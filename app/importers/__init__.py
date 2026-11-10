"""Upload orchestrator: bytes + filename -> normalized orders."""

from __future__ import annotations

from typing import Any

from . import bill, files, generic


def parse_upload(
    data: bytes,
    filename: str = "",
    *,
    platform: str = "",
    include_unknown: bool = False,
) -> dict[str, Any]:
    tables, error = files.load_tables(data, filename)
    if error:
        return {"ok": False, "error": error, "orders": [], "stats": {}}

    orders: list[dict[str, Any]] = []
    stats = {
        "tables": len(tables),
        "rows": 0,
        "skipped_income": 0,
        "skipped_type": 0,
        "skipped_unknown": 0,
        "skipped_invalid": 0,
    }
    sources: list[str] = []

    for table in tables:
        kind = bill.detect(table.headers)
        if kind:
            parsed, table_stats = bill.to_orders(
                table, kind=kind, include_unknown=include_unknown
            )
            sources.append(kind)
            source = kind
        else:
            parsed, table_stats = generic.to_orders(
                table, platform=platform, filename=filename, source="export"
            )
            sources.append("export")
            source = "export"
        for item in parsed:
            item.setdefault("source", source)
        orders.extend(parsed)
        for key, value in table_stats.items():
            stats[key] = stats.get(key, 0) + value

    if not orders:
        return {
            "ok": False,
            "error": "没有可导入的订单行",
            "orders": [],
            "stats": stats,
            "sources": sources,
        }

    # Dedupe inside one upload: exporting twice in the same file should not
    # create duplicate synthetic ids before they hit the DB.
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for item in orders:
        key = (item["platform"], item.get("platform_order_id") or "")
        if key[1] and key in seen:
            continue
        if key[1]:
            seen.add(key)
        unique.append(item)

    return {
        "ok": True,
        "orders": unique,
        "stats": stats,
        "sources": sorted(set(sources)),
        "tables": [table.name for table in tables],
    }
