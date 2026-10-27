"""Parser for JD's pcdetail_traceMultiPackage response."""

from __future__ import annotations

import re
from typing import Any

STATUS_BY_CODE = {
    "0001": "processing",
    "0002": "processing",
    "0004": "in_transit",
    "0005": "in_transit",
    "0006": "out_for_delivery",
    "0007": "delivered",
    "0008": "delivered",
    "0009": "returned",
}

STATUS_WORDS = [
    ("delivered", ("已签收", "签收", "已送达", "已完成")),
    ("returned", ("退回", "退签", "拒收", "退货")),
    ("out_for_delivery", ("派送中", "派件", "配送中")),
    ("ready_for_pickup", ("待取件", "已到驿站", "快递柜", "取件码")),
    ("in_transit", ("运输中", "已揽收", "已发出", "到达", "离开", "分拣", "发往")),
    ("processing", ("仓库", "打包", "拣货", "出库", "已下单", "处理中")),
]

PICKUP = re.compile(r"取件码\s*[:：]?\s*([0-9A-Za-z-]{2,12})")


def _status(code: str, name: str, content: str) -> str:
    if code in STATUS_BY_CODE:
        return STATUS_BY_CODE[code]
    text = f"{name} {content}"
    for status, words in STATUS_WORDS:
        if any(word in text for word in words):
            return status
    return "unknown"


def parse_jd_trace(payload: dict[str, Any], order_id: str = "") -> list[dict[str, Any]]:
    """Return one shipment-shaped dict per package in the trace response."""
    packages: list[dict[str, Any]] = []
    for package in payload.get("multiPackageTrackInfoList") or []:
        if not isinstance(package, dict):
            continue
        group = package.get("trackGroupInfo") or {}
        carriage = group.get("carriageInfo") or package.get("carriageInfo") or {}
        tracking_no = str(carriage.get("carriageId") or "").strip().upper()
        if not tracking_no:
            continue
        events = []
        for event in group.get("orderTrackShowList") or []:
            if not isinstance(event, dict):
                continue
            content = str(event.get("Content") or "").strip()
            code = str(event.get("Status") or "").strip()
            name = str(event.get("StatusName") or "").strip()
            events.append(
                {
                    "time": event.get("CreationTime"),
                    "context": content,
                    "status": _status(code, name, content),
                    "status_text": name,
                }
            )
        events.sort(key=lambda item: str(item.get("time") or ""))
        latest = events[-1] if events else {}
        status = latest.get("status") or "unknown"
        status_text = latest.get("status_text") or ""
        if not status_text and latest.get("context"):
            status_text = str(latest["context"])[:40]
        pickup_match = PICKUP.search(latest.get("context") or "")
        packages.append(
            {
                "tracking_no": tracking_no,
                "carrier": str(carriage.get("carrier") or ""),
                "carrier_name": str(carriage.get("carrier") or ""),
                "status": status,
                "status_text": status_text,
                "pickup_code": pickup_match.group(1) if pickup_match else "",
                "last_event_at": latest.get("time"),
                "events": events,
                "process": payload.get("processTimeInfo") or {},
                "courier": payload.get("courierInfo") or {},
                "order_id": order_id,
                "raw": package,
            }
        )
    return packages
