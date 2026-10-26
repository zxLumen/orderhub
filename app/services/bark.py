"""Minimal Bark push client (iOS)."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request

from .. import config

log = logging.getLogger(__name__)


def send(title: str, body: str, *, url: str | None = None, group: str = "OrderHub") -> bool:
    if not config.BARK_URL:
        log.info("Bark not configured; skip push: %s | %s", title, body)
        return False
    payload: dict[str, str] = {
        "title": title[:80],
        "body": body[:500],
        "group": group,
        "sound": "default",
    }
    if url:
        payload["url"] = url
    request = urllib.request.Request(
        config.BARK_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
        return True
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Bark push failed: %s", exc)
        return False
