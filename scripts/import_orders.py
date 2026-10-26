"""Import an order export (CSV or JSON) into OrderHub.

Examples:
    python scripts/import_orders.py taobao.csv --platform taobao
    python scripts/import_orders.py pipl.json
    python scripts/import_orders.py orders.csv --db data/orderhub.db

CSV headers can be English or common Chinese names. Unknown columns are kept in
raw_json so nothing is silently lost.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import db  # noqa: E402

ALIASES = {
    "platform": ("platform", "平台", "渠道"),
    "platform_order_id": ("platform_order_id", "order_id", "订单号", "订单编号", "交易订单号"),
    "title": ("title", "商品", "商品名称", "商品标题", "宝贝名称"),
    "shop": ("shop", "店铺", "店铺名称", "商家"),
    "amount": ("amount", "金额", "实付", "实付金额", "付款金额", "总金额"),
    "status": ("status", "状态", "订单状态"),
    "status_text": ("status_text", "状态说明"),
    "order_time": ("order_time", "下单时间", "创建时间", "交易时间", "时间"),
    "tracking_no": ("tracking_no", "运单号", "快递单号", "物流单号"),
    "url": ("url", "链接", "订单链接"),
}


def _pick(row: dict[str, Any], key: str) -> Any:
    for alias in ALIASES[key]:
        if alias in row and row[alias] not in (None, ""):
            return row[alias]
    # Case-insensitive fallback
    lowered = {str(k).strip().lower(): v for k, v in row.items()}
    for alias in ALIASES[key]:
        if alias.lower() in lowered and lowered[alias.lower()] not in (None, ""):
            return lowered[alias.lower()]
    return None


def _amount(value: Any) -> float | None:
    if value in (None, ""):
        return None
    text = str(value).replace(",", "").replace("¥", "").replace("￥", "").strip()
    match = re.search(r"-?\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def _normalize(row: dict[str, Any], default_platform: str | None) -> dict[str, Any]:
    return {
        "platform": str(_pick(row, "platform") or default_platform or "unknown").strip().lower(),
        "platform_order_id": str(_pick(row, "platform_order_id") or "").strip() or None,
        "title": str(_pick(row, "title") or "").strip(),
        "shop": str(_pick(row, "shop") or "").strip(),
        "amount": _amount(_pick(row, "amount")),
        "status": str(_pick(row, "status") or "unknown").strip(),
        "status_text": str(_pick(row, "status_text") or "").strip(),
        "order_time": _pick(row, "order_time"),
        "tracking_no": str(_pick(row, "tracking_no") or "").strip() or None,
        "url": str(_pick(row, "url") or "").strip() or None,
        "raw": row,
    }


def load(path: Path) -> list[dict[str, Any]]:
    if path.suffix.lower() in {".json", ".jsonl"}:
        if path.suffix.lower() == ".jsonl":
            return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data = data.get("orders") or data.get("items") or data.get("data") or []
        return [item for item in data if isinstance(item, dict)]
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description="Import orders into OrderHub")
    parser.add_argument("input", type=Path)
    parser.add_argument("--platform", help="default platform when the file has no platform column")
    parser.add_argument("--db", type=Path, help="database path (default: ORDERHUB_DATA_DIR/orderhub.db)")
    args = parser.parse_args()

    db.init_db(args.db)
    rows = load(args.input)
    inserted = 0
    with db.connect(args.db) as conn:
        for row in rows:
            normalized = _normalize(row, args.platform)
            if not normalized["title"] and not normalized["platform_order_id"]:
                continue
            db.upsert_order(conn, **normalized)
            inserted += 1
        conn.commit()
    print(f"imported {inserted} orders from {args.input}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
