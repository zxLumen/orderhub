"""Windows-side collector agent.

Runs JD collection locally (Chrome + Playwright + local SQLite staging) and
ships orders / run records / alerts to the server. The local database is only a
durable outbox: the server remains the single source of truth for the web UI.

    python -m app.collector_agent
    python -m app.collector_agent --once
"""

from __future__ import annotations

import argparse
import gzip
import json
import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, db
from .collectors.jd import collect_jd, normalize_mode

log = logging.getLogger(__name__)

VERSION = "0.1.0"
STATE_FILE = "collector-sync.json"
CHROME_MODE_FILE = Path(config.BASE_DIR) / "data" / "chrome-mode.txt"


class Agent:
    def __init__(self) -> None:
        self.server = config.SERVER_URL
        self.token = config.COLLECTOR_TOKEN
        self.worker = os.environ.get("ORDERHUB_WORKER_NAME") or socket.gethostname()
        self.state_path = config.DATA_DIR / STATE_FILE
        self._stop = threading.Event()
        self._last_heartbeat = 0.0
        self._last_sync = 0.0
        self._last_schedule = 0.0
        self.state = self._load_state()

    # ------------------------------------------------------------------
    # HTTP
    # ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "User-Agent": f"OrderHub-Agent/{VERSION}",
        }

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.server:
            raise RuntimeError("ORDERHUB_SERVER_URL is not configured")
        if not self.token:
            raise RuntimeError("ORDERHUB_COLLECTOR_TOKEN is not configured")
        url = self.server.rstrip("/") + path
        data = None
        headers = self._headers()
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            data = gzip.compress(body, 6)
            headers["Content-Encoding"] = "gzip"
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"server HTTP {exc.code}: {detail}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise RuntimeError(f"server unreachable: {exc}") from exc
        if not raw:
            return {"ok": True}
        try:
            result = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            raise RuntimeError("server returned non-JSON")
        if result.get("ok") is False:
            raise RuntimeError(result.get("error") or "server error")
        return result

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    def _load_state(self) -> dict[str, Any]:
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "last_order_updated": "",
                "last_order_id": 0,
                "last_run_id": 0,
                "last_alert_id": 0,
                "last_event_id": 0,
                "last_schedule": 0,
            }

    def _save_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ------------------------------------------------------------------
    # Sync
    # ------------------------------------------------------------------
    def _chrome_ok(self) -> bool:
        try:
            with urllib.request.urlopen(config.JD_CDP_URL.rstrip("/") + "/json/version", timeout=3) as response:
                return response.status == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Login watchdog
    # ------------------------------------------------------------------
    def _chrome_mode(self) -> str:
        try:
            value = CHROME_MODE_FILE.read_text(encoding="utf-8-sig").strip().lower()
        except OSError:
            return ""
        return value.lstrip("\ufeff")

    def _chrome_pages(self) -> list[dict[str, Any]]:
        try:
            with urllib.request.urlopen(
                config.JD_CDP_URL.rstrip("/") + "/json/list", timeout=3
            ) as response:
                data = json.loads(response.read().decode("utf-8", "replace"))
        except Exception:
            return []
        return [page for page in data if isinstance(page, dict) and page.get("type") == "page"]

    @staticmethod
    def _order_page_open(pages: list[dict[str, Any]]) -> bool:
        """True when a logged-in JD order page is open.

        The pre-login passport URL embeds the order URL in ``ReturnUrl``, and the
        order URL briefly loads before redirecting, so require both the order
        host and the logged-in page title.
        """
        for page in pages:
            if page.get("type") not in (None, "page"):
                continue
            url = str(page.get("url") or "")
            title = str(page.get("title") or "")
            if "order.jd.com" not in url:
                continue
            if "passport.jd.com" in url or "login.jd.com" in url:
                continue
            if "订单列表" in title:
                return True
        return False

    def _switch_chrome(self, mode: str) -> bool:
        script = Path(config.BASE_DIR) / "scripts" / "chrome_ctl.ps1"
        if not script.exists():
            log.error("chrome_ctl.ps1 not found: %s", script)
            return False
        log.info("switching chrome to %s", mode)
        try:
            subprocess.Popen(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-WindowStyle",
                    "Hidden",
                    "-File",
                    str(script),
                    "-Mode",
                    mode,
                ],
                cwd=str(config.BASE_DIR),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            log.error("chrome switch failed: %s", exc)
            return False
        for _ in range(60):
            time.sleep(0.5)
            if self._chrome_ok() and self._chrome_mode() == mode:
                return True
        log.warning("chrome did not switch to %s in time", mode)
        return False

    def _open_login_window(self) -> None:
        """Show a visible Chrome at the JD order page, unless one is already up."""
        if not config.JD_AUTO_LOGIN:
            return
        if not self._chrome_ok():
            # The watchdog restarts Chrome on its own; retry on the next tick.
            return
        if self._chrome_mode() == "visible":
            return
        log.info("JD login required; opening a visible Chrome window")
        self._switch_chrome("visible")

    def _ensure_headless(self) -> None:
        if self._chrome_mode() != "visible":
            return
        log.info("JD login restored; switching back to headless")
        self._switch_chrome("headless")

    def _login_watch(self) -> None:
        """Keep prompting for a login until JD accepts the session again."""
        if not config.JD_AUTO_LOGIN or not self._chrome_ok():
            return
        if self._chrome_mode() == "visible":
            if self._order_page_open(self._chrome_pages()):
                log.info("JD login detected in the visible window")
                self.state["login_required"] = False
                self._save_state()
                if self._switch_chrome("headless"):
                    self.collect("recent")
            return
        if self.state.get("login_required"):
            self._open_login_window()

    def heartbeat(self) -> None:
        try:
            self._request(
                "POST",
                "/api/collector/heartbeat",
                {
                    "name": self.worker,
                    "version": VERSION,
                    "chrome_ok": self._chrome_ok(),
                    "message": "agent alive",
                },
            )
            self._last_heartbeat = time.time()
        except Exception as exc:
            log.warning("heartbeat failed: %s", exc)

    def sync(self, *, command_id: int | None = None, status: str = "ok", message: str = "") -> None:
        """Push local staging rows to the server, then advance the cursor.

        Large full-history runs are chunked; the command is finished only on the
        last chunk so a partial upload doesn't mark the job done prematurely.
        """
        with db.session() as conn:
            order_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM orders WHERE "
                    "(COALESCE(updated_at, created_at, '') > ?) OR "
                    "(COALESCE(updated_at, created_at, '') = ? AND id > ?) "
                    "ORDER BY COALESCE(updated_at, created_at, '') ASC, id ASC",
                    (
                        self.state.get("last_order_updated") or "",
                        self.state.get("last_order_updated") or "",
                        int(self.state.get("last_order_id") or 0),
                    ),
                ).fetchall()
            ]
            run_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM collector_runs WHERE id > ? ORDER BY id ASC",
                    (int(self.state.get("last_run_id") or 0),),
                ).fetchall()
            ]
            alert_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM alerts WHERE id > ? ORDER BY id ASC",
                    (int(self.state.get("last_alert_id") or 0),),
                ).fetchall()
            ]
            shipment_rows = [
                dict(row)
                for row in conn.execute("SELECT * FROM shipments").fetchall()
            ]
            event_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM parcel_events WHERE id > ? ORDER BY id ASC",
                    (int(self.state.get("last_event_id") or 0),),
                ).fetchall()
            ]
            link_rows = [
                dict(row)
                for row in conn.execute(
                    "SELECT os.tracking_no, os.package_index, os.source, "
                    "o.platform, o.platform_order_id "
                    "FROM order_shipments os JOIN orders o ON o.id=os.order_id"
                ).fetchall()
            ]

        order_chunks = [order_rows[i : i + 50] for i in range(0, len(order_rows), 50)] or [[]]
        for index, chunk_orders in enumerate(order_chunks):
            first = index == 0
            last = index == len(order_chunks) - 1
            payload: dict[str, Any] = {
                "orders": chunk_orders,
                "runs": run_rows if first else [],
                "alerts": alert_rows if first else [],
                "shipments": shipment_rows if first else [],
                "events": event_rows if first else [],
                "links": link_rows if first else [],
                "worker": {
                    "name": self.worker,
                    "version": VERSION,
                    "chrome_ok": self._chrome_ok(),
                    "message": "syncing",
                },
            }
            if command_id is not None and last:
                payload["command_id"] = command_id
                payload["status"] = status
                payload["message"] = message

            self._request("POST", "/api/collector/ingest", payload)

            if chunk_orders:
                self.state["last_order_updated"] = (
                    chunk_orders[-1].get("updated_at") or chunk_orders[-1].get("created_at") or ""
                )
                self.state["last_order_id"] = chunk_orders[-1].get("id") or 0
            if first and run_rows:
                self.state["last_run_id"] = run_rows[-1]["id"]
            if first and alert_rows:
                self.state["last_alert_id"] = alert_rows[-1]["id"]
            if first and event_rows:
                self.state["last_event_id"] = event_rows[-1]["id"]
            self._save_state()

        self._last_sync = time.time()
        log.info(
            "synced orders=%s runs=%s alerts=%s",
            len(order_rows),
            len(run_rows),
            len(alert_rows),
        )

    # ------------------------------------------------------------------
    # Collection
    # ------------------------------------------------------------------
    def collect(self, mode: str = "recent", *, command_id: int | None = None) -> dict[str, Any]:
        mode = normalize_mode(mode)
        log.info("starting JD collection mode=%s command=%s", mode, command_id)
        result = collect_jd(mode)
        status = "ok" if result.get("ok") else str(result.get("error") or "error")
        message = json.dumps(result, ensure_ascii=False)[:500]
        try:
            # Sync even on captcha/login: runs and alerts are useful to the web UI.
            self.sync(command_id=command_id, status=status, message=message)
        except Exception as exc:
            log.error("sync failed: %s", exc)
            if command_id is not None:
                try:
                    self._request(
                        "POST",
                        f"/api/collector/commands/{command_id}/finish",
                        {"status": "error", "message": str(exc)[:500]},
                    )
                except Exception:
                    pass
        if result.get("ok"):
            if self.state.get("login_required"):
                self.state["login_required"] = False
                self._save_state()
            self._ensure_headless()
        elif result.get("error") in {"login_required", "captcha"}:
            self.state["login_required"] = True
            self._save_state()
            self._open_login_window()
        return result

    def maybe_scheduled(self) -> None:
        now = time.time()
        interval = max(1800, config.JD_INTERVAL_SECONDS)
        last = max(float(self.state.get("last_schedule") or 0), self._last_schedule)
        if now - last < interval:
            return
        self._last_schedule = now
        self.state["last_schedule"] = now
        self._save_state()
        self.collect("recent")

    def poll_command(self) -> bool:
        try:
            data = self._request(
                "GET",
                "/api/collector/commands/next?worker=" + urllib.parse.quote(self.worker),
            )
        except Exception as exc:
            log.warning("command poll failed: %s", exc)
            return False
        command = data.get("command")
        if not command:
            return False
        self.collect(str(command.get("mode") or "recent"), command_id=int(command["id"]))
        return True

    # ------------------------------------------------------------------
    # Loop
    # ------------------------------------------------------------------
    def run(self, *, once: bool = False) -> None:
        if not self.server:
            raise SystemExit("ORDERHUB_SERVER_URL is required for the collector agent")
        if not self.token:
            raise SystemExit("ORDERHUB_COLLECTOR_TOKEN is required for the collector agent")
        db.init_db()
        self.heartbeat()
        while not self._stop.is_set():
            try:
                if once:
                    self.maybe_scheduled()
                    self.poll_command()
                    self.sync()
                    return
                self.maybe_scheduled()
                self.poll_command()
                self._login_watch()
                if time.time() - self._last_heartbeat > 30:
                    self.heartbeat()
                if time.time() - self._last_sync > 60:
                    try:
                        self.sync()
                    except Exception as exc:
                        log.warning("periodic sync failed: %s", exc)
                self._stop.wait(15)
            except KeyboardInterrupt:
                break
            except Exception:
                log.exception("agent loop failed")
                self._stop.wait(10)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--sync-only", action="store_true", help="upload local staging rows without collecting")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    agent = Agent()
    if args.sync_only:
        if not agent.server or not agent.token:
            raise SystemExit("ORDERHUB_SERVER_URL and ORDERHUB_COLLECTOR_TOKEN are required")
        db.init_db()
        agent.heartbeat()
        agent.sync()
        return 0
    agent.run(once=args.once)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
