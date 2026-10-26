"""Kuaidi100 tracking query client.

Docs: https://api.kuaidi100.com/ (query endpoint)
Requires KUAIDI100_CUSTOMER and KUAIDI100_KEY in .env.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import config

log = logging.getLogger(__name__)

STATE_MAP = {
    "0": "in_transit",
    "1": "in_transit",
    "2": "unknown",
    "3": "delivered",
    "4": "returned",
    "5": "out_for_delivery",
    "6": "returned",
    "7": "in_transit",
    "8": "in_transit",
    "9": "in_transit",
    "10": "out_for_delivery",
}

# Our carrier codes -> Kuaidi100 company codes
COM_MAP = {
    "sf": "shunfeng",
    "jd": "jd",
    "yto": "yuantong",
    "zto": "zhongtong",
    "sto": "shentong",
    "yunda": "yunda",
    "ems": "ems",
    "jtexpress": "jtexpress",
    "debang": "debang",
}


def configured() -> bool:
    return bool(config.KUAIDI100_CUSTOMER and config.KUAIDI100_KEY)


def _sign(param: str) -> str:
    raw = param + config.KUAIDI100_KEY + config.KUAIDI100_CUSTOMER
    return hashlib.md5(raw.encode("utf-8")).hexdigest().upper()


def query(carrier: str, tracking_no: str, *, phone: str = "") -> dict[str, Any]:
    """Query one parcel. Returns {ok, state, status, events, error}."""
    if not configured():
        return {"ok": False, "error": "not_configured"}
    com = COM_MAP.get((carrier or "").lower())
    if not com:
        return {"ok": False, "error": "unsupported_carrier"}

    param_obj: dict[str, str] = {"com": com, "num": tracking_no, "resultv2": "1"}
    if phone:
        param_obj["phone"] = phone
    param = json.dumps(param_obj, ensure_ascii=False, separators=(",", ":"))
    body = urllib.parse.urlencode(
        {
            "customer": config.KUAIDI100_CUSTOMER,
            "sign": _sign(param),
            "param": param,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://poll.kuaidi100.com/poll/query.do",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        log.warning("Kuaidi100 query failed for %s: %s", tracking_no, exc)
        return {"ok": False, "error": str(exc)}

    if str(payload.get("status")) != "200" and payload.get("message"):
        return {"ok": False, "error": payload.get("message")}

    state = str(payload.get("state", ""))
    events = []
    for item in payload.get("data") or []:
        if not isinstance(item, dict):
            continue
        events.append(
            {
                "time": item.get("time") or item.get("ftime") or "",
                "context": item.get("context") or "",
                "status": item.get("status") or "",
            }
        )
    return {
        "ok": True,
        "state": STATE_MAP.get(state, "unknown"),
        "raw_state": state,
        "status_text": payload.get("message") or "",
        "events": events,
    }
