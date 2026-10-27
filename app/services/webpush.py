"""Browser Web Push (VAPID) sender.

Optional: requires pywebpush + VAPID keys in /etc/orderhub.env. When missing,
OrderHub keeps working with the in-app notification center.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sqlite3
from typing import Any

from .. import config, db

log = logging.getLogger(__name__)


def configured() -> bool:
    return bool(
        config.VAPID_PUBLIC_KEY
        and config.VAPID_PRIVATE_KEY
        and importlib.util.find_spec("pywebpush") is not None
    )


def public_key() -> str:
    return config.VAPID_PUBLIC_KEY if configured() else ""


def _send_one(subscription: dict[str, Any], title: str, body: str, url: str | None) -> str:
    try:
        from pywebpush import WebPushException, webpush
    except ImportError:
        return "unavailable"
    payload = json.dumps(
        {"title": title, "body": body, "url": url or "/"},
        ensure_ascii=False,
    )
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=payload,
            vapid_private_key=config.VAPID_PRIVATE_KEY,
            vapid_claims={"sub": config.VAPID_SUBJECT},
        )
        return "ok"
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in {404, 410}:
            return "gone"
        log.warning("web push failed: %s", exc)
        return "error"
    except Exception as exc:  # pragma: no cover - network/openssl
        log.warning("web push failed: %s", exc)
        return "error"


def send_all(
    conn: sqlite3.Connection, *, title: str, body: str, url: str | None = None
) -> dict[str, int]:
    if not configured():
        return {"ok": 0, "gone": 0, "error": 0, "unavailable": 1}
    counts = {"ok": 0, "gone": 0, "error": 0, "unavailable": 0}
    for subscription in db.list_push_subscriptions(conn):
        result = _send_one(subscription, title, body, url)
        counts[result] = counts.get(result, 0) + 1
        if result == "gone":
            db.delete_push_subscription(conn, subscription["endpoint"])
    return counts
