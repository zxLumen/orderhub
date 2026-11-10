"""Free tracking lookup via the public Kuaidi100 web endpoint.

The paid API needs a customer/key pair that OrderHub does not have. The public
web query used by kuaidi100.com itself returns the same trace without a key, so
non-JD parcels can still be tracked. It is an undocumented endpoint: treat it
as best effort, keep a cache, rate-limit it, and always fall back silently.
"""

from __future__ import annotations

import json
import logging
import random
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .. import config
from .kuaidi100 import STATE_MAP

log = logging.getLogger(__name__)

URL = "https://www.kuaidi100.com/query"

USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148"
)

# Our carrier code -> Kuaidi100 type
TYPE_MAP = {
    "sf": "shunfeng",
    "jd": "jd",
    "yto": "yuantong",
    "zto": "zhongtong",
    "sto": "shentong",
    "yunda": "yunda",
    "ems": "ems",
    "jtexpress": "jtexpress",
    "debang": "debang",
    "youzhengguonei": "youzhengguonei",
    "cainiao": "cainiao",
}

TYPE_LABEL = {
    "shunfeng": "顺丰速运",
    "jd": "京东物流",
    "yuantong": "圆通速递",
    "zhongtong": "中通快递",
    "shentong": "申通快递",
    "yunda": "韵达速递",
    "ems": "中国邮政EMS",
    "jtexpress": "极兔速递",
    "debang": "德邦快递",
    "youzhengguonei": "邮政快递包裹",
    "cainiao": "菜鸟",
}

NAME_TO_CODE = {
    "顺丰": "sf",
    "顺丰速运": "sf",
    "京东": "jd",
    "京东物流": "jd",
    "京东快递": "jd",
    "圆通": "yto",
    "圆通速递": "yto",
    "中通": "zto",
    "中通快递": "zto",
    "申通": "sto",
    "申通快递": "sto",
    "韵达": "yunda",
    "韵达速递": "yunda",
    "邮政": "ems",
    "ems": "ems",
    "中国邮政ems": "ems",
    "极兔": "jtexpress",
    "极兔速递": "jtexpress",
    "德邦": "debang",
    "德邦快递": "debang",
}

CODE_BY_TYPE = {value: key for key, value in TYPE_MAP.items()}

_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_CACHE_TTL = 600
_LOCK = threading.Lock()
_LAST_CALL = 0.0
_MIN_INTERVAL = 0.55


def _sleep() -> None:
    global _LAST_CALL
    with _LOCK:
        now = time.monotonic()
        wait = _MIN_INTERVAL - (now - _LAST_CALL)
        if wait > 0:
            time.sleep(wait)
        _LAST_CALL = time.monotonic()


def normalize_carrier(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered in TYPE_MAP:
        return lowered
    if lowered in NAME_TO_CODE:
        return NAME_TO_CODE[lowered]
    for name, code in NAME_TO_CODE.items():
        if name and name in lowered:
            return code
    return ""


def guess_types(tracking_no: str, carrier: str = "") -> list[str]:
    number = (tracking_no or "").strip().upper()
    code = normalize_carrier(carrier)
    types: list[str] = []
    if code in TYPE_MAP:
        types.append(TYPE_MAP[code])

    prefix_order = [
        (re.compile(r"^SF\d"), "shunfeng"),
        (re.compile(r"^JD[A-Z0-9]"), "jd"),
        (re.compile(r"^YT\d"), "yuantong"),
        (re.compile(r"^ZTO"), "zhongtong"),
        (re.compile(r"^STO"), "shentong"),
        (re.compile(r"^YD\d"), "yunda"),
        (re.compile(r"^JT\d"), "jtexpress"),
        (re.compile(r"^DPK"), "debang"),
        (re.compile(r"^[A-Z]{2}\d{9}[A-Z]{2}$"), "ems"),
    ]
    for pattern, value in prefix_order:
        if pattern.match(number) and value not in types:
            types.append(value)

    if not types:
        # Numeric waybills are ambiguous. Try the four carriers that cover most
        # of them; stop as soon as one returns a real trace.
        types = ["zhongtong", "yuantong", "yunda", "shentong"]
    return types


def _query_once(tracking_no: str, kuaidi_type: str) -> dict[str, Any]:
    key = f"{kuaidi_type}:{tracking_no.upper()}"
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < _CACHE_TTL:
        return cached[1]

    params = {
        "type": kuaidi_type,
        "postid": tracking_no,
        "temp": f"{random.random():.6f}",
    }
    phone = getattr(config, "KUAIDI100_PHONE", "")
    if phone:
        params["phone"] = phone
    request = urllib.request.Request(
        URL + "?" + urllib.parse.urlencode(params),
        headers={
            "User-Agent": USER_AGENT,
            "Referer": "https://www.kuaidi100.com/",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        },
    )
    _sleep()
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        result = {"ok": False, "error": str(exc), "type": kuaidi_type}
        _CACHE[key] = (time.monotonic(), result)
        return result

    message = str(payload.get("message") or "")
    events = payload.get("data") if isinstance(payload.get("data"), list) else []
    if str(payload.get("status")) != "200" or message != "ok" or not events:
        result = {
            "ok": False,
            "error": message or "empty",
            "type": kuaidi_type,
            "not_found": True,
        }
        _CACHE[key] = (time.monotonic(), result)
        return result

    first_context = str((events[0] or {}).get("context") or "")
    if "查无结果" in first_context or "暂无轨迹" in first_context:
        result = {"ok": False, "error": "查无结果", "type": kuaidi_type, "not_found": True}
        _CACHE[key] = (time.monotonic(), result)
        return result

    state = str(payload.get("state") or "")
    parsed_events = [
        {
            "time": item.get("time") or item.get("ftime") or "",
            "context": item.get("context") or "",
            "status": item.get("status") or "",
        }
        for item in events
        if isinstance(item, dict)
    ]
    result = {
        "ok": True,
        "type": kuaidi_type,
        "carrier_name": TYPE_LABEL.get(kuaidi_type, kuaidi_type),
        "state": STATE_MAP.get(state, "unknown"),
        "raw_state": state,
        "status_text": parsed_events[0]["context"][:80] if parsed_events else "",
        "events": parsed_events,
    }
    _CACHE[key] = (time.monotonic(), result)
    return result


def query(tracking_no: str, carrier: str = "") -> dict[str, Any]:
    """Try likely carriers until one returns a trace. Never raises."""
    tracking_no = (tracking_no or "").strip()
    if not tracking_no:
        return {"ok": False, "error": "missing_tracking_no"}

    last_error = "not_found"
    for kuaidi_type in guess_types(tracking_no, carrier):
        result = _query_once(tracking_no, kuaidi_type)
        if result.get("ok"):
            return result
        last_error = str(result.get("error") or last_error)
    return {"ok": False, "error": last_error}
