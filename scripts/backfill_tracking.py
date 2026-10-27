"""Backfill tracking numbers from existing logistics text.

Run once after upgrading:
    python scripts/backfill_tracking.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402
from app.services import tracking  # noqa: E402


def main() -> int:
    db.init_db()
    updated = 0
    with db.session() as conn:
        rows = conn.execute(
            "SELECT * FROM orders WHERE (tracking_no IS NULL OR tracking_no='') "
            "AND logistics_text IS NOT NULL AND logistics_text != ''"
        ).fetchall()
        for row in rows:
            number, _code, _name = tracking.extract(row["logistics_text"])
            if not number:
                continue
            db.upsert_order(
                conn,
                platform=row["platform"],
                platform_order_id=row["platform_order_id"],
                title=row["title"] or "",
                shop=row["shop"] or "",
                amount=row["amount"],
                status=row["status"] or "unknown",
                status_text=row["status_text"] or "",
                order_time=row["order_time"],
                tracking_no=number,
                url=row["url"],
                product_url=row["product_url"],
                image_url=row["image_url"],
                logistics_text=row["logistics_text"],
            )
            db.add_parcel_event(
                conn,
                tracking_no=number,
                event_time=row["updated_at"],
                context=row["logistics_text"][:1000],
                status="",
                source="jd_text",
            )
            updated += 1
        conn.commit()
    print(f"backfilled {updated} orders")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
