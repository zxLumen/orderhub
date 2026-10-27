"""In-app alert + browser Web Push dispatcher."""

from __future__ import annotations

import logging
import threading
from typing import Any

from ..db import add_alert, session
from . import webpush

log = logging.getLogger(__name__)


def _push_worker(title: str, body: str, url: str | None) -> None:
    try:
        with session() as conn:
            result = webpush.send_all(conn, title=title, body=body, url=url)
            if result.get("error"):
                log.warning("web push result: %s", result)
    except Exception:
        log.exception("web push dispatch failed")


def notify(
    conn: Any,
    *,
    kind: str,
    title: str,
    body: str = "",
    url: str | None = None,
    push: bool = True,
) -> int:
    alert_id = add_alert(conn, kind=kind, title=title, body=body)
    if push and webpush.configured():
        threading.Thread(
            target=_push_worker,
            args=(title, body, url),
            daemon=True,
            name="orderhub-webpush",
        ).start()
    return alert_id
