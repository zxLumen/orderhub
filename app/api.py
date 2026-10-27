"""Dependency-free HTTP API + static PWA server."""

from __future__ import annotations

import gzip
import hmac
import json
import logging
import mimetypes
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from . import auth, config, db
from .collectors.jd import normalize_mode
from .db import session, utc_now
from .services import notifier, webpush
from .services.ingest import ingest_capture, ingest_sms, poll_parcels

log = logging.getLogger(__name__)

VERSION = "0.1.0"

_LOGIN_LOCK = threading.Lock()
_LOGIN_ATTEMPTS: dict[str, list[float]] = {}


def _login_rate_limited(address: str) -> bool:
    now = time.time()
    window = max(60, config.LOGIN_RATE_WINDOW)
    with _LOGIN_LOCK:
        attempts = [stamp for stamp in _LOGIN_ATTEMPTS.get(address, []) if now - stamp < window]
        if len(attempts) >= max(1, config.LOGIN_RATE_LIMIT):
            _LOGIN_ATTEMPTS[address] = attempts
            return True
        attempts.append(now)
        _LOGIN_ATTEMPTS[address] = attempts
        return False


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _public_config() -> dict[str, Any]:
    return {
        "version": VERSION,
        "auth_required": bool(config.TOKEN or config.PASSWORD_HASH),
        "login_required": bool(config.PASSWORD_HASH),
        "kuaidi100_configured": bool(config.KUAIDI100_CUSTOMER and config.KUAIDI100_KEY),
        "webpush_configured": webpush.configured(),
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

    def _session_value(self) -> str:
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return ""
        morsel = cookie.get(auth.COOKIE_NAME)
        return morsel.value if morsel else ""

    def _bearer_token(self) -> str:
        header = self.headers.get("Authorization", "")
        token = ""
        if header.lower().startswith("bearer "):
            token = header[7:].strip()
        if not token:
            token = self.headers.get("X-OrderHub-Token", "").strip()
        return token

    def _session_authenticated(self) -> bool:
        return bool(
            config.PASSWORD_HASH
            and config.SESSION_SECRET
            and auth.verify_session(config.SESSION_SECRET, self._session_value())
        )

    def _authenticated(self) -> bool:
        if not config.TOKEN and not config.PASSWORD_HASH:
            return True
        token = self._bearer_token()
        if config.TOKEN and token and hmac.compare_digest(token, config.TOKEN):
            return True
        return self._session_authenticated()

    def _check_auth(self, path: str) -> str:
        if not config.TOKEN and not config.PASSWORD_HASH:
            return "none"
        if path in {"/api/health", "/api/config", "/api/session", "/api/login", "/api/logout"}:
            return "none"
        token = self._bearer_token()
        if config.TOKEN and token and hmac.compare_digest(token, config.TOKEN):
            return "token"
        if self._session_authenticated():
            return "session"
        raise ApiError(HTTPStatus.UNAUTHORIZED, "unauthorized")

    def _set_session_cookie(self, value: str, max_age: int) -> None:
        secure = config.COOKIE_SECURE or self.headers.get("X-Forwarded-Proto", "").lower() == "https"
        parts = [
            f"{auth.COOKIE_NAME}={value}",
            "Path=/",
            "HttpOnly",
            "SameSite=Lax",
            f"Max-Age={max_age}",
        ]
        if secure:
            parts.append("Secure")
        self.send_header("Set-Cookie", "; ".join(parts))

    def _redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _csrf_ok(self) -> bool:
        origin = self.headers.get("Origin", "")
        if not origin:
            return True
        parsed = urllib.parse.urlparse(origin)
        host = self.headers.get("Host", "")
        return parsed.netloc == host

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
        if self.headers.get("Content-Encoding", "").lower() == "gzip":
            try:
                raw = gzip.decompress(raw)
            except OSError:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid gzip body")
            if len(raw) > max(config.MAX_BODY_BYTES * 8, 8 * 1024 * 1024):
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body too large")
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
                return
            if config.PASSWORD_HASH and not self._session_authenticated():
                public = (
                    path in {"/login", "/login.html", "/login.js", "/styles.css"}
                    or path.startswith("/icons/")
                    or path in {"/manifest.webmanifest", "/sw.js", "/app.js"}
                )
                if not public:
                    self._redirect("/login?next=" + urllib.parse.quote(path or "/"))
                    return
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
            if path == "/api/login":
                self._handle_login()
                return
            if path == "/api/logout":
                self.send_response(HTTPStatus.OK)
                self._set_session_cookie("", 0)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            mode = self._check_auth(path)
            if mode == "session" and not self._csrf_ok():
                raise ApiError(HTTPStatus.FORBIDDEN, "origin mismatch")
            payload = self._read_json()
            self._post_api(path, payload, urllib.parse.parse_qs(parsed.query))
        except ApiError as exc:
            self._error(exc.status, exc.message)
        except Exception:  # pragma: no cover
            log.exception("POST failed: %s", path)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "internal error")

    def _handle_login(self) -> None:
        if not config.PASSWORD_HASH:
            raise ApiError(HTTPStatus.BAD_REQUEST, "password login not configured")
        if not config.SESSION_SECRET:
            raise ApiError(HTTPStatus.INTERNAL_SERVER_ERROR, "session secret missing")
        if _login_rate_limited(self.client_address[0]):
            raise ApiError(HTTPStatus.TOO_MANY_REQUESTS, "rate_limited")
        payload = self._read_json()
        password = str(payload.get("password") or "")
        if not auth.verify_password(password, config.PASSWORD_HASH):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "invalid_password")
        session_value = auth.create_session(config.SESSION_SECRET, config.SESSION_TTL_SECONDS)
        self.send_response(HTTPStatus.OK)
        self._set_session_cookie(session_value, config.SESSION_TTL_SECONDS)
        body = b'{"ok":true}'
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # ------------------------------------------------------------------
    # API
    # ------------------------------------------------------------------
    def _get_api(self, path: str, query: dict[str, list[str]]) -> None:
        if path == "/api/health":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "api_version": 1,
                    "version": VERSION,
                    "time": utc_now(),
                    "auth_required": bool(config.TOKEN or config.PASSWORD_HASH),
                    "login_required": bool(config.PASSWORD_HASH),
                },
            )
            return
        if path == "/api/session":
            self._json(
                HTTPStatus.OK,
                {
                    "ok": True,
                    "authenticated": self._authenticated(),
                    "login_required": bool(config.PASSWORD_HASH),
                },
            )
            return
        if path == "/api/config":
            self._json(HTTPStatus.OK, {"ok": True, **_public_config()})
            return
        if path == "/api/push/public-key":
            self._json(
                HTTPStatus.OK,
                {"ok": True, "enabled": webpush.configured(), "public_key": webpush.public_key()},
            )
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
            elif path == "/api/collector/status":
                worker = db.get_worker(conn)
                online = False
                if worker and worker.get("last_seen"):
                    try:
                        from datetime import datetime, timezone

                        seen = datetime.fromisoformat(worker["last_seen"].replace("Z", "+00:00"))
                        online = (datetime.now(timezone.utc) - seen).total_seconds() < 90
                    except ValueError:
                        pass
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "online": online,
                        "worker": worker,
                        "commands": db.list_commands(conn, limit=10),
                        "runs": db.stats(conn)["collector_runs"],
                    },
                )
            elif path == "/api/collector/commands/next":
                worker = _str(query, "worker") or "windows"
                command = db.claim_command(conn, worker)
                self._json(HTTPStatus.OK, {"ok": True, "command": command})
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
            with session() as conn:
                command_id = db.enqueue_command(conn, platform="jd", mode=mode)
            self._json(
                HTTPStatus.ACCEPTED,
                {"ok": True, "queued": True, "command_id": command_id, "mode": mode},
            )
            return

        if path == "/api/push/subscribe":
            subscription = payload.get("subscription") if isinstance(payload.get("subscription"), dict) else payload
            endpoint = str(subscription.get("endpoint") or "").strip()
            keys = subscription.get("keys") if isinstance(subscription.get("keys"), dict) else {}
            p256dh = str(keys.get("p256dh") or "").strip()
            auth_key = str(keys.get("auth") or "").strip()
            if not endpoint or not p256dh or not auth_key:
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid subscription")
            with session() as conn:
                db.upsert_push_subscription(
                    conn,
                    endpoint=endpoint,
                    p256dh=p256dh,
                    auth=auth_key,
                    user_agent=self.headers.get("User-Agent", ""),
                )
            self._json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/push/unsubscribe":
            endpoint = str(payload.get("endpoint") or "").strip()
            if not endpoint:
                raise ApiError(HTTPStatus.BAD_REQUEST, "endpoint required")
            with session() as conn:
                db.delete_push_subscription(conn, endpoint)
            self._json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/push/test":
            if not webpush.configured():
                raise ApiError(HTTPStatus.BAD_REQUEST, "webpush_not_configured")
            with session() as conn:
                result = webpush.send_all(
                    conn,
                    title="OrderHub 测试通知",
                    body="如果你看到这条，Web Push 已经通了。",
                )
            self._json(HTTPStatus.OK, {"ok": True, "result": result})
            return

        if path == "/api/collector/heartbeat":
            with session() as conn:
                db.heartbeat_worker(
                    conn,
                    name=str(payload.get("name") or "windows"),
                    version=str(payload.get("version") or ""),
                    chrome_ok=bool(payload.get("chrome_ok")),
                    message=str(payload.get("message") or ""),
                )
            self._json(HTTPStatus.OK, {"ok": True})
            return

        if path == "/api/collector/ingest":
            with session() as conn:
                result = _ingest_remote(conn, payload)
            self._json(HTTPStatus.OK, {"ok": True, **result})
            return

        if path.startswith("/api/collector/commands/") and path.endswith("/finish"):
            try:
                command_id = int(path.rsplit("/", 2)[-2])
            except (ValueError, IndexError):
                raise ApiError(HTTPStatus.BAD_REQUEST, "invalid command id")
            with session() as conn:
                db.finish_command(
                    conn,
                    command_id,
                    status=str(payload.get("status") or "done"),
                    message=str(payload.get("message") or ""),
                )
            self._json(HTTPStatus.OK, {"ok": True})
            return

        raise ApiError(HTTPStatus.NOT_FOUND, "not found")

    # ------------------------------------------------------------------
    # Static PWA
    # ------------------------------------------------------------------
    def _serve_static(self, path: str) -> None:
        web_dir = config.WEB_DIR.resolve()
        if path in {"", "/"}:
            target = web_dir / "index.html"
        elif path in {"/login", "/login.html"}:
            target = web_dir / "login.html"
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


def _ingest_remote(conn: Any, payload: dict[str, Any]) -> dict[str, Any]:
    orders = payload.get("orders") or []
    runs = payload.get("runs") or []
    alerts = payload.get("alerts") or []
    worker = payload.get("worker") if isinstance(payload.get("worker"), dict) else None

    order_count = 0
    for raw_order in orders:
        if not isinstance(raw_order, dict):
            continue
        item = dict(raw_order)
        if "raw" not in item and item.get("raw_json"):
            try:
                item["raw"] = json.loads(item["raw_json"])
            except (TypeError, json.JSONDecodeError):
                pass
        _insert_order(conn, item)
        order_count += 1

    run_count = 0
    for run in runs:
        if not isinstance(run, dict):
            continue
        db.insert_collector_run(
            conn,
            platform=str(run.get("platform") or "jd"),
            started_at=run.get("started_at"),
            finished_at=run.get("finished_at"),
            status=str(run.get("status") or "unknown"),
            items=int(run.get("items") or 0),
            captcha=bool(run.get("captcha")),
            message=str(run.get("message") or ""),
        )
        run_count += 1

    alert_count = 0
    for alert in alerts:
        if not isinstance(alert, dict):
            continue
        notifier.notify(
            conn,
            kind=str(alert.get("kind") or "collector"),
            title=str(alert.get("title") or "采集提醒"),
            body=str(alert.get("body") or ""),
        )
        alert_count += 1

    if worker:
        db.heartbeat_worker(
            conn,
            name=str(worker.get("name") or "windows"),
            version=str(worker.get("version") or ""),
            chrome_ok=bool(worker.get("chrome_ok")),
            message=str(worker.get("message") or ""),
        )

    command_id = payload.get("command_id")
    if command_id:
        try:
            db.finish_command(
                conn,
                int(command_id),
                status=str(payload.get("status") or "done"),
                message=str(payload.get("message") or ""),
            )
        except (TypeError, ValueError):
            pass

    return {
        "orders": order_count,
        "runs": run_count,
        "alerts": alert_count,
    }


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
