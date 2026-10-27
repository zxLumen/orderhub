"""Tracking-number extraction shared by JD parsing and backfills."""

from __future__ import annotations

import re

TRACKING_LABEL = re.compile(
    r"(?:运单号|快递单号|物流单号)(?:为|是|[:：])?\s*([A-Za-z0-9-]{8,24})",
    re.I,
)
TRACKING_PREFIX = re.compile(
    r"\b(?:SF|JD|YT|ZTO|STO|YD|JT|DPK|EMS)[A-Z0-9]{8,20}\b",
    re.I,
)
EMS = re.compile(r"\b[A-Z]{2}\d{9}[A-Z]{2}\b", re.I)

CARRIERS = [
    ("sf", "顺丰速运", ("顺丰",)),
    ("jd", "京东物流", ("京东",)),
    ("yto", "圆通速递", ("圆通",)),
    ("zto", "中通快递", ("中通",)),
    ("sto", "申通快递", ("申通",)),
    ("yunda", "韵达速递", ("韵达",)),
    ("ems", "中国邮政EMS", ("邮政", "ems")),
    ("jtexpress", "极兔速递", ("极兔",)),
    ("debang", "德邦快递", ("德邦",)),
]


def _clean(value: str) -> str:
    return re.sub(r"[\s-]", "", value).upper()


def _carrier(text: str, number: str) -> tuple[str, str]:
    upper = number.upper()
    if upper.startswith("SF"):
        return "sf", "顺丰速运"
    if upper.startswith("JD"):
        return "jd", "京东物流"
    if upper.startswith("YT"):
        return "yto", "圆通速递"
    if "ZTO" in upper:
        return "zto", "中通快递"
    if upper.startswith("STO"):
        return "sto", "申通快递"
    if upper.startswith("YD"):
        return "yunda", "韵达速递"
    if upper.startswith("JT"):
        return "jtexpress", "极兔速递"
    if re.fullmatch(r"[A-Z]{2}\d{9}[A-Z]{2}", upper):
        return "ems", "中国邮政EMS"
    for code, name, words in CARRIERS:
        if any(word in text.lower() for word in words):
            return code, name
    return "", ""


def extract(text: str) -> tuple[str, str, str]:
    """Return (tracking_no, carrier_code, carrier_name) from a logistics line."""
    text = text or ""
    candidates: list[str] = []
    for match in TRACKING_LABEL.finditer(text):
        candidates.append(_clean(match.group(1)))
    for match in TRACKING_PREFIX.finditer(text):
        candidates.append(_clean(match.group(0)))
    for match in EMS.finditer(text):
        candidates.append(_clean(match.group(0)))

    # Only accept a bare number when the text explicitly talks about a waybill.
    if not candidates and ("运单" in text or "快递单" in text or "物流单" in text):
        for match in re.finditer(r"(?<!\d)(\d{12,20})(?!\d)", text):
            value = match.group(1)
            if len(value) == 11 and value.startswith("1"):
                continue
            candidates.append(value)
            break

    for candidate in candidates:
        if 8 <= len(candidate) <= 24:
            code, name = _carrier(text, candidate)
            return candidate, code, name
    return "", "", ""
