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
            return {"last_order_updated": "", "last_run_id": 0, "last_alert_id": 0, "last_schedule": 0}

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

        order_chunks = [order_rows[i : i + 50] for i in range(0, len(order_rows), 50)] or [[]]
        for index, chunk_orders in enumerate(order_chunks):
            first = index == 0
            last = index == len(order_chunks) - 1
            payload: dict[str, Any] = {
                "orders": chunk_orders,
                "runs": run_rows if first else [],
                "alerts": alert_rows if first else [],
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
