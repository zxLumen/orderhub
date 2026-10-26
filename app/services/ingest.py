"""Ingestion pipeline: SMS, captures, and parcel tracking updates."""

from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Any

from .. import config
from ..db import (
    add_alert,
    add_parcel_event,
    list_shipments,
    record_capture,
    record_sms,
    session,
    upsert_shipment,
)
from ..parsers.sms import parse_sms
from . import bark, kuaidi100

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "unknown": "未知",
    "accepted": "已接单",
    "in_transit": "运输中",
    "out_for_delivery": "派送中",
    "ready_for_pickup": "待取件",
    "delivering": "配送中",
    "delivered": "已签收/送达",
    "returned": "已退回",
    "refunded": "已退款",
}

PUSH_STATUSES = {"out_for_delivery", "ready_for_pickup", "delivered", "returned"}


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status or "未知")


def _link_order(
    conn: sqlite3.Connection,
    *,
    tracking_no: str,
    platform: str,
    order_id_text: str,
) -> int | None:
    row = None
    if order_id_text:
        row = conn.execute(
            "SELECT id, platform FROM orders WHERE platform=? AND platform_order_id=?",
            (platform or "", order_id_text),
        ).fetchone()
    if row is None:
        row = conn.execute(
            "SELECT id, platform FROM orders WHERE tracking_no=? ORDER BY id DESC LIMIT 1",
            (tracking_no,),
        ).fetchone()
    if row is None:
        return None
    order_id = int(row["id"])
    conn.execute(
        "UPDATE shipments SET order_id=?, platform=? WHERE tracking_no=?",
        (order_id, row["platform"] or platform, tracking_no.upper()),
    )
    conn.execute(
        "UPDATE orders SET tracking_no=COALESCE(tracking_no, ?) WHERE id=?",
        (tracking_no.upper(), order_id),
    )
    return order_id


def _notify(conn: sqlite3.Connection, *, kind: str, title: str, body: str, url: str | None = None) -> None:
    add_alert(conn, kind=kind, title=title, body=body)
    if not config.BARK_URL:
        return
    # Never keep a SQLite write transaction open while talking to the network.
    threading.Thread(
        target=bark.send,
        args=(title, body),
        kwargs={"url": url},
        daemon=True,
        name="orderhub-bark",
    ).start()


def ingest_sms(
    conn: sqlite3.Connection,
    *,
    text: str,
    sender: str = "",
    received_at: str | None = None,
) -> dict[str, Any]:
    parsed = parse_sms(text, sender)
    event_id, created = record_sms(
        conn, text=text, sender=sender, received_at=received_at, parsed=parsed.as_dict()
    )
    result: dict[str, Any] = {
        "event_id": event_id,
        "created": created,
        "parsed": parsed.as_dict(),
        "alerts": [],
    }
    if not created or parsed.ignored:
        return result

    occurred_at = received_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
    for tracking_no in parsed.tracking_numbers or ([parsed.tracking_no] if parsed.tracking_no else []):
        _, changed = upsert_shipment(
            conn,
            tracking_no=tracking_no,
            carrier=parsed.carrier,
            carrier_name=parsed.carrier_name,
            platform=parsed.platform,
            status=parsed.status,
            status_text=status_label(parsed.status),
            pickup_code=parsed.pickup_code,
            location=parsed.location,
            last_event_at=occurred_at,
        )
        add_parcel_event(
            conn,
            tracking_no=tracking_no,
            event_time=occurred_at,
            context=text[:1000],
            status=parsed.status,
            source="sms",
        )
        _link_order(
            conn,
            tracking_no=tracking_no,
            platform=parsed.platform,
            order_id_text=parsed.order_id,
        )

        if changed and parsed.status in PUSH_STATUSES:
            title = f"{status_label(parsed.status)} · {parsed.carrier_name or '包裹'}"
            body_parts = [f"运单 {tracking_no}"]
            if parsed.pickup_code:
                body_parts.append(f"取件码 {parsed.pickup_code}")
            if parsed.location:
                body_parts.append(parsed.location)
            body = " · ".join(body_parts)
            _notify(conn, kind=f"parcel_{parsed.status}", title=title, body=body)
            result["alerts"].append({"title": title, "body": body})
        elif parsed.status == "in_transit" and changed:
            # First scan of a new parcel: a quiet alert is still useful.
            _notify(
                conn,
                kind="parcel_in_transit",
                title="新包裹已发出",
                body=f"{parsed.carrier_name or '包裹'} {tracking_no}",
            )

    # Food delivery has no tracking number; keep the status event and push the
    # short-lived, high-value states.
    if not parsed.tracking_numbers and parsed.platform in {"meituan", "eleme"}:
        if parsed.status in {"accepted", "delivering", "out_for_delivery", "delivered"}:
            title = f"{'外卖' if parsed.platform == 'meituan' else '订单'} · {status_label(parsed.status)}"
            body = text[:160]
            _notify(conn, kind=f"food_{parsed.status}", title=title, body=body)
            result["alerts"].append({"title": title, "body": body})

    return result


def ingest_capture(
    conn: sqlite3.Connection,
    *,
    platform: str,
    text: str,
    image_path: str = "",
    captured_at: str | None = None,
) -> dict[str, Any]:
    capture_id, created = record_capture(
        conn,
        platform=platform,
        text=text,
        image_path=image_path,
        captured_at=captured_at,
    )
    result: dict[str, Any] = {"capture_id": capture_id, "created": created, "parsed": None}
    if not created:
        return result

    # Captures often include the order detail page: reuse the SMS heuristics to
    # pull a tracking number / status out of the OCR text.
    parsed = parse_sms(text, platform)
    result["parsed"] = parsed.as_dict()
    if parsed.tracking_numbers:
        occurred_at = captured_at or datetime.now(timezone.utc).isoformat(timespec="seconds")
        for tracking_no in parsed.tracking_numbers:
            _, changed = upsert_shipment(
                conn,
                tracking_no=tracking_no,
                carrier=parsed.carrier,
                carrier_name=parsed.carrier_name,
                platform=platform,
                status=parsed.status,
                status_text=status_label(parsed.status),
                pickup_code=parsed.pickup_code,
                location=parsed.location,
                last_event_at=occurred_at,
            )
            add_parcel_event(
                conn,
                tracking_no=tracking_no,
                event_time=occurred_at,
                context=text[:1000],
                status=parsed.status,
                source="capture",
            )
            if changed:
                _notify(
                    conn,
                    kind="capture_parcel",
                    title="抓屏补到运单",
                    body=f"{tracking_no} · {status_label(parsed.status)}",
                )
    return result


def poll_parcels(conn: sqlite3.Connection, *, limit: int = 20) -> dict[str, Any]:
    """Refresh active shipments via Kuaidi100. Safe to call when unconfigured."""
    if not kuaidi100.configured():
        return {"ok": False, "error": "not_configured", "checked": 0, "updated": 0}

    candidates = [
        shipment
        for shipment in list_shipments(conn, active_only=True, limit=max(limit * 3, 60))
        if shipment.get("carrier") in kuaidi100.COM_MAP
    ][:limit]

    checked = 0
    updated = 0
    errors: list[str] = []
    for shipment in candidates:
        tracking_no = shipment["tracking_no"]
        phone = config.KUAIDI100_PHONE
        result = kuaidi100.query(shipment.get("carrier") or "", tracking_no, phone=phone)
        checked += 1
        if not result.get("ok"):
            errors.append(f"{tracking_no}: {result.get('error')}")
            continue
        new_status = result.get("state") or "unknown"
        old_status = shipment.get("status") or "unknown"
        changed = new_status != old_status
        _, status_changed = upsert_shipment(
            conn,
            tracking_no=tracking_no,
            carrier=shipment.get("carrier") or "",
            carrier_name=shipment.get("carrier_name") or "",
            platform=shipment.get("platform") or "",
            status=new_status,
            status_text=status_label(new_status),
            last_event_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        added = 0
        for event in result.get("events") or []:
            if add_parcel_event(
                conn,
                tracking_no=tracking_no,
                event_time=event.get("time"),
                context=event.get("context") or "",
                status=new_status,
                source="kuaidi100",
            ):
                added += 1
        if status_changed and new_status in PUSH_STATUSES:
            body_parts = [f"运单 {tracking_no}"]
            if shipment.get("pickup_code"):
                body_parts.append(f"取件码 {shipment['pickup_code']}")
            _notify(
                conn,
                kind=f"parcel_{new_status}",
                title=f"{status_label(new_status)} · {shipment.get('carrier_name') or '包裹'}",
                body=" · ".join(body_parts),
            )
        if changed or added:
            updated += 1
    return {"ok": True, "checked": checked, "updated": updated, "errors": errors[:5]}


def poll_loop() -> None:
    """Background loop used by the scheduler."""
    try:
        with session() as conn:
            result = poll_parcels(conn)
            if result.get("checked"):
                log.info("Parcel poll: %s", result)
    except Exception:  # pragma: no cover - defensive, keep the loop alive
        log.exception("Parcel poll failed")
