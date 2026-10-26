"""Dependency-free HTTP API + static PWA server."""

from __future__ import annotations

import hmac
import json
import logging
import mimetypes
import sys
import threading
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import config, db
from .collectors.jd import collect_jd_async, normalize_mode
from .db import session, utc_now
from .services.ingest import ingest_capture, ingest_sms, poll_parcels

log = logging.getLogger(__name__)

VERSION = "0.1.0"


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _public_config() -> dict[str, Any]:
    return {
        "version": VERSION,
        "auth_required": bool(config.TOKEN),
        "kuaidi100_configured": bool(config.KUAIDI100_CUSTOMER and config.KUAIDI100_KEY),
        "bark_configured": bool(config.BARK_URL),
        "jd_collector_enabled": config.ENABLE_JD_COLLECTOR,
        "scheduler_enabled": config.ENABLE_SCHEDULER,
        "jd_cdp_url": config.JD_CDP_URL if config.ENABLE_JD_COLLECTOR else "",
        "parcel_poll_seconds": config.PARCEL_POLL_SECONDS,
        "jd_interval_seconds": config.JD_INTERVAL_SECONDS,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = f"OrderHub/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------
    # Plumbing
    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args: Any) -> None:
        log.info("%s - %s", self.address_string(), fmt % args)

    def _send(
        self,
        status: int,
        body: bytes,
        content_type: str,
        *,
        cache: str = "no-store",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https://*.360buyimg.com; "
            "style-src 'self' 'unsafe-inline'; script-src 'self'; "
            "connect-src 'self'; manifest-src 'self'; worker-src 'self'",
        )
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._json(status, {"ok": False, "error": message})

    def _check_auth(self, path: str) -> None:
        if not config.TOKEN:
            return
        if path in {"/api/health", "/api/config"}:
            return
        header = self.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            token = self.headers.get("X-OrderHub-Token", "").strip()
        if token and hmac.compare_digest(token, config.TOKEN):
            return
        raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized")

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid content length")
        if length > config.MAX_BODY_BYTES:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            value = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise ApiError(HTTPStatus.BAD_REQUEST, "invalid json")
        if not isinstance(value, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "json object expected")
        return value

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------
    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Allow", "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            if path.startswith("/api/"):
                self._check_auth(path)
                self._get_api(path, urllib.parse.parse_qs(parsed.query))
            else:
                self._serve_static(path)
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except Exception:  # pragma: no cover
            log.exception("GET failed: %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def do_POST(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = urllib.parse.unquote(parsed.path)
        try:
            self._check_auth(path)
            payload = self._read_json()
            self._post_api(path, payload, urllib.parse.parse_qs(parsed.query))
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except Exception:  # pragma: no cover
            log.exception("POST failed: %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def _get_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "version": VERSION,
                    "time": utc_now(),
                    "auth_required": bool(config.TOKEN),
                },
            )
            return
        if path == "/api/config":
            self._json(HTTPStatus.OK, {"ok": True, **_public_config()})
            return

        with session() as conn:
            if path == "/api/orders":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "items": db.list_orders(
                            conn,
                            limit=_int(query, "limit", 50),
                            offset=_int(query, "offset", 0),
                            platform=_str(query, "platform"),
                            status=_str(query, "status"),
                        ),
                    },
                )
            elif path == "/api/shipments":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "items": db.list_shipments(
                            conn,
                            active_only=_bool(query, "active"),
                            limit=_int(query, "limit", 100),
                        ),
                    },
                )
            elif path.startswith("/api/shipments/"):
                tracking_no = path.rsplit("/", 1)[-1]
                shipment = db.get_shipment(conn, tracking_no)
                if shipment is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "shipment not found")
                self._json(HTTPStatus.OK, {"ok": True, "item": shipment})
            elif path == "/api/sms":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "items": db.list_sms(
                            conn,
                            limit=_int(query, "limit", 50),
                            include_ignored=_bool(query, "include_ignored"),
                        ),
                    },
                )
            elif path == "/api/captures":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "items": db.list_captures(conn, limit=_int(query, "limit", 50))},
                )
            elif path == "/api/alerts":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "items": db.list_alerts(conn, limit=_int(query, "limit", 50))},
                )
            elif path == "/api/search":
                text = _str(query, "q") or ""
                if not text.strip():
                    self._json(HTTPStatus.OK, {"ok": True, "items": []})
                    return
                results = db.search(
                    conn,
                    text,
                    limit=_int(query, "limit", 50),
                    platform=_str(query, "platform"),
                )
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "query": text, "items": _hydrate_search(conn, results)},
                )
            elif path == "/api/stats":
                self._json(HTTPStatus.OK, {"ok": True, **_public_config(), **db.stats(conn)})
            elif path == "/api/export":
                self._json(
                    HTTPStatus.OK,
                    {"ok": True, "exported_at": utc_now(), "data": db.export_all(conn)},
                )
            else:
                raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    def _post_api(self, path: str, payload: dict[str, Any], query: dict[str, list[str]]) -> None:
        if path == "/api/sms":
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ApiError(HTTPStatus.BAD_REQUEST, "text is required")
            with session() as conn:
                result = ingest_sms(
                    conn,
                    text=text,
                    sender=str(payload.get("sender") or ""),
                    received_at=payload.get("received_at"),
                )
                conn.commit()
            self._json(HTTPStatus.OK, {"ok": True, **result})
            return

        if path == "/api/captures":
            text = str(payload.get("text") or "").strip()
            if not text:
                raise ApiError(HTTPStatus.BAD_REQUEST, "text is required")
            platform = str(payload.get("platform") or "douyin")
            with session() as conn:
                result = ingest_capture(
                    conn,
                    platform=platform,
                    text=text,
                    image_path=str(payload.get("image_path") or ""),
                    captured_at=payload.get("captured_at"),
                )
                conn.commit()
            self._json(HTTPStatus.OK, {"ok": True, **result})
            return

        if path == "/api/orders":
            with session() as conn:
                order_id = _insert_order(conn, payload)
                conn.commit()
            self._json(HTTPStatus.OK, {"ok": True, "id": order_id})
            return

        if path == "/api/orders/import":
            items = payload.get("orders")
            if not isinstance(items, list):
                raise ApiError(HTTPStatus.BAD_REQUEST, "orders[] is required")
            inserted = 0
            with session() as conn:
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    _insert_order(conn, item)
                    inserted += 1
                conn.commit()
            self._json(HTTPStatus.OK, {"ok": True, "inserted": inserted})
            return

        if path == "/api/alerts/read":
            alert_id = payload.get("id")
            with session() as conn:
                if alert_id:
                    conn.execute("UPDATE alerts SET read=1 WHERE id=?", (int(alert_id),))
                else:
                    conn.execute("UPDATE alerts SET read=1")
                conn.commit()
            self._json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/parcels/poll":
            threading.Thread(target=_poll_thread, daemon=True).start()
            self._json(HTTPStatus.ACCEPTED, {"ok": True, "started": True})
            return

        if path == "/api/collectors/jd/run":
            mode = normalize_mode(payload.get("mode"))
            threading.Thread(target=collect_jd_async, args=(mode,), daemon=True).start()
            self._json(HTTPStatus.ACCEPTED, {"ok": True, "started": True, "mode": mode})
            return

        raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    # ------------------------------------------------------------------
    # Static PWA
    # ------------------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        web_dir = config.WEB_DIR.resolve()
        if path in {"", "/"}:
            target = web_dir / "index.html"
        else:
            relative = path.lstrip("/")
            target = (web_dir / relative).resolve()
            if web_dir not in target.parents and target != web_dir:
                raise ApiError(HTTPStatus.FORBIDDEN, "forbidden")
            if not target.exists() or target.is_dir():
                # SPA fallback: unknown non-file routes render the app shell.
                target = web_dir / "index.html"
        if not target.exists():
            raise ApiError(HTTPStatus.NOT_FOUND, "not found")
        content_type = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".webmanifest":
            content_type = "application/manifest+json"
        if target.suffix in {".js", ".mjs"}:
            content_type = "text/javascript; charset=utf-8"
        if target.suffix == ".css":
            content_type = "text/css; charset=utf-8"
        if target.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        if target.suffix == ".svg":
            content_type = "image/svg+xml"
        cache = "public, max-age=3600" if target.suffix in {".png", ".svg", ".ico"} else "no-cache"
        self._send(HTTPStatus.OK, target.read_bytes(), content_type, cache=cache)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
def _str(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key)
    return values[0].strip() if values and values[0].strip() else None


def _int(query: dict[str, list[str]], key: str, default: int) -> int:
    try:
        return int((query.get(key) or [str(default)])[0])
    except (TypeError, ValueError):
        return default


def _bool(query: dict[str, list[str]], key: str) -> bool:
    return ((query.get(key) or [""])[0] or "").lower() in {"1", "true", "yes", "on"}


def _insert_order(conn: Any, payload: dict[str, Any]) -> int:
    platform = str(payload.get("platform") or "").strip()
    if not platform:
        raise ApiError(HTTPStatus.BAD_REQUEST, "platform is required")
    amount = payload.get("amount")
    try:
        amount_value = float(amount) if amount not in (None, "") else None
    except (TypeError, ValueError):
        amount_value = None
    return db.upsert_order(
        conn,
        platform=platform,
        platform_order_id=str(payload.get("platform_order_id") or "").strip() or None,
        title=str(payload.get("title") or ""),
        shop=str(payload.get("shop") or ""),
        amount=amount_value,
        status=str(payload.get("status") or "unknown"),
        status_text=str(payload.get("status_text") or ""),
        order_time=payload.get("order_time"),
        tracking_no=payload.get("tracking_no") or None,
        url=payload.get("url") or None,
        product_url=payload.get("product_url") or None,
        image_url=payload.get("image_url") or None,
        logistics_text=payload.get("logistics_text") or None,
        raw=payload.get("raw"),
    )


def _hydrate_search(conn: Any, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    hydrated: list[dict[str, Any]] = []
    for result in results:
        item = dict(result)
        if result.get("doc_type") == "order":
            row = conn.execute("SELECT * FROM orders WHERE id=?", (int(result["doc_id"]),)).fetchone()
            if row:
                item["order"] = dict(row)
        elif result.get("doc_type") == "capture":
            row = conn.execute(
                "SELECT * FROM captures WHERE id=?", (int(result["doc_id"]),)
            ).fetchone()
            if row:
                item["capture"] = dict(row)
        elif result.get("doc_type") == "sms":
            row = conn.execute(
                "SELECT * FROM sms_events WHERE id=?", (int(result["doc_id"]),)
            ).fetchone()
            if row:
                item["sms"] = dict(row)
        hydrated.append(item)
    return hydrated


def _poll_thread() -> None:
    try:
        with session() as conn:
            result = poll_parcels(conn)
            log.info("Manual parcel poll: %s", result)
    except Exception:  # pragma: no cover
        log.exception("Manual parcel poll failed")


class Server(ThreadingHTTPServer):
    """Threaded HTTP server that ignores client disconnects.

    Browsers and local health probes close keep-alive sockets without a proper
    shutdown; socketserver would print a full traceback for each one.
    """

    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:  # pragma: no cover
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            log.debug("client disconnected: %s", client_address)
            return
        super().handle_error(request, client_address)


def serve() -> None:
    db.init_db()
    loopback = {"127.0.0.1", "localhost", "::1"}
    if config.HOST not in loopback and not config.TOKEN:
        raise SystemExit(
            "ORDERHUB_TOKEN is required when ORDERHUB_HOST is not loopback. "
            "Set a long random token in .env, or bind to 127.0.0.1."
        )
    if not config.TOKEN:
        log.warning(
            "ORDERHUB_TOKEN is empty; this is only safe for loopback access. "
            "Set a token before using Tailscale Serve or LAN access."
        )
    httpd = Server((config.HOST, config.PORT), Handler)
    log.info("OrderHub listening on http://%s:%s", config.HOST, config.PORT)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
