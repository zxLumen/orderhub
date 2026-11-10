"""Generic platform order-export importer.

Every marketplace names its columns differently ("实付款" / "订单金额" /
"金额(元)"), so instead of one adapter per platform this maps a wide alias list
onto the six fields OrderHub needs. Works for CSV, XLSX and JSON, including the
ZIP that "个人信息副本" usually arrives in.
"""

from __future__ import annotations

import re
from typing import Any

from .. import platforms
from .bill import _money, _status
from .files import Table

ALIASES: dict[str, tuple[str, ...]] = {
    "order_id": (
        "订单号", "订单编号", "主订单编号", "订单ID", "订单id", "交易订单号", "交易单号",
        "商家订单号", "订单编码", "子订单号", "订单流水号", "平台订单号",
    ),
    "title": (
        "商品名称", "商品", "商品标题", "宝贝标题", "商品信息", "交易内容", "商品说明",
        "产品名称", "名称", "商品名称(简)", "订单标题", "套餐名称",
    ),
    "shop": (
        "店铺名称", "店铺", "卖家名称", "卖家", "商家名称", "商户名称", "交易对方",
        "供应商", "商家", "店铺名",
    ),
    "amount": (
        "实付款", "实付金额", "金额", "订单金额", "应付款金额", "支付金额", "金额(元)",
        "商品金额", "总金额", "实际支付", "实付",
    ),
    "time": (
        "订单时间", "下单时间", "创建时间", "交易时间", "付款时间", "成交时间",
        "订单创建时间", "支付时间", "日期",
    ),
    "status": ("订单状态", "交易状态", "状态", "订单状态说明", "售后状态"),
    "tracking": ("运单号", "快递单号", "物流单号", "快递编号", "物流编号", "货运单号"),
    "carrier": ("快递公司", "物流公司", "承运商", "快递", "配送方式"),
    "url": ("订单链接", "订单详情", "链接", "详情链接", "订单url", "订单URL"),
    "image": ("商品图片", "图片", "主图", "图片链接", "商品主图", "商品图片链接"),
    "logistics_text": ("物流信息", "物流状态", "物流描述", "最新物流", "物流详情"),
    "platform": ("平台", "渠道", "来源平台", "商城"),
}

PLATFORM_WORDS = (
    ("京东", "jd"),
    ("淘宝", "taobao"),
    ("天猫", "taobao"),
    ("拼多多", "pdd"),
    ("抖音", "douyin"),
    ("闲鱼", "xianyu"),
    ("转转", "zhuanzhuan"),
    ("美团", "meituan"),
    ("饿了么", "eleme"),
    ("唯品会", "vip"),
    ("小红书", "xiaohongshu"),
)


def _pick(row: dict[str, str], field: str) -> str:
    for key in ALIASES[field]:
        value = row.get(key)
        if value:
            return str(value).strip()
    return ""


def _clean_id(value: str) -> str:
    text = (value or "").strip().strip("'\"")
    if re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def _infer_from_filename(filename: str) -> str:
    lowered = (filename or "").lower()
    for word, key in PLATFORM_WORDS:
        if word in filename:
            return key
    for key, item in platforms.PLATFORMS.items():
        if key in lowered:
            return key
    return ""


def to_orders(
    table: Table,
    *,
    platform: str = "",
    filename: str = "",
    source: str = "export",
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders: list[dict[str, Any]] = []
    stats = {"rows": 0, "skipped_invalid": 0}
    filename_platform = platforms.normalize(platform) if platform else _infer_from_filename(filename)

    for row in table.rows:
        stats["rows"] += 1
        text = " ".join(
            [
                _pick(row, "shop"),
                _pick(row, "title"),
                _pick(row, "platform"),
                table.name,
            ]
        )
        row_platform = _pick(row, "platform")
        if row_platform:
            guessed = platforms.infer(row_platform) if platforms.normalize(row_platform) == "other" else platforms.normalize(row_platform)
        else:
            guessed = platforms.infer(text, default="")
        chosen = guessed or filename_platform or "other"

        order_id = _clean_id(_pick(row, "order_id"))
        title = _pick(row, "title")
        shop = _pick(row, "shop")
        if not order_id and not title:
            stats["skipped_invalid"] += 1
            continue

        status, status_text = _status(_pick(row, "status"))
        orders.append(
            {
                "platform": chosen,
                "platform_order_id": order_id,
                "title": title,
                "shop": shop,
                "amount": _money(_pick(row, "amount")),
                "status": status,
                "status_text": status_text,
                "order_time": _pick(row, "time"),
                "tracking_no": _pick(row, "tracking") or None,
                "logistics_text": _pick(row, "logistics_text") or None,
                "url": _pick(row, "url") or None,
                "product_url": _pick(row, "url") or None,
                "image_url": _pick(row, "image") or None,
                "source": source,
                "raw": {"_source": source, "_table": table.name, "row": row},
            }
        )

    return orders, stats
