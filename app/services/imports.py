"""Persist normalized imported orders and report what changed."""

from __future__ import annotations

import sqlite3
from typing import Any

from .. import db


def import_orders(
    conn: sqlite3.Connection,
    orders: list[dict[str, Any]],
    *,
    source: str = "",
) -> dict[str, Any]:
    created = 0
    updated = 0
    skipped = 0
    by_platform: dict[str, int] = {}
    ids: list[int] = []

    for item in orders:
        platform = str(item.get("platform") or "other").strip()
        order_id = str(item.get("platform_order_id") or "").strip()
        if order_id:
            exists = conn.execute(
                "SELECT id FROM orders WHERE platform=? AND platform_order_id=?",
                (platform, order_id),
            ).fetchone()
        else:
            exists = None

        try:
            row_id = db.upsert_order(
                conn,
                platform=platform,
                platform_order_id=order_id or None,
                title=str(item.get("title") or ""),
                shop=str(item.get("shop") or ""),
                amount=item.get("amount"),
                status=str(item.get("status") or "unknown"),
                status_text=str(item.get("status_text") or ""),
                order_time=item.get("order_time"),
                tracking_no=item.get("tracking_no") or None,
                url=item.get("url") or None,
                product_url=item.get("product_url") or None,
                image_url=item.get("image_url") or None,
                logistics_text=item.get("logistics_text") or None,
                source=str(item.get("source") or source or ""),
                raw=item.get("raw"),
            )
        except (sqlite3.Error, ValueError, TypeError):
            skipped += 1
            continue

        if exists:
            updated += 1
        else:
            created += 1
        by_platform[platform] = by_platform.get(platform, 0) + 1
        if len(ids) < 20:
            ids.append(row_id)

    return {
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "imported": created + updated,
        "by_platform": by_platform,
        "sample_ids": ids,
    }
