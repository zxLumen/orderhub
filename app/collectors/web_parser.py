"""Parsers for the browser-side platform collectors.

The web order pages of Taobao, Pinduoduo, Douyin, Xianyu and Zhuanzhuan are
single-page apps. Instead of hard-coding one API per platform (they change
often), the collector sniffs the JSON the page itself loads and this module
pulls order-shaped objects out of it. A text fallback handles server-rendered
pages, and Taobao gets a dedicated parser because its ``mainOrders`` payload is
stable and much richer.

Every function here is pure Python and testable without a browser.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import platforms
from ..db import normalize_time

# ---------------------------------------------------------------------------
# Field aliases
# ---------------------------------------------------------------------------
ID_KEYS: dict[str, tuple[str, ...]] = {
    "taobao": ("orderId", "bizOrderId", "tid"),
    "pdd": ("orderSn", "order_sn", "orderId", "order_id"),
    "douyin": ("orderId", "order_id", "orderSn", "bizOrderId"),
    "xianyu": ("bizOrderId", "orderId", "order_id"),
    "zhuanzhuan": ("orderId", "orderNo", "orderSn", "order_id"),
}

TITLE_KEYS = (
    "itemTitle", "goodsName", "productName", "productTitle", "itemName",
    "subject", "title", "goodsTitle", "wareName", "spuName", "item_title",
)
AMOUNT_KEYS = (
    "payAmount", "actualFee", "orderAmount", "payMoney", "totalPrice",
    "payment", "price", "amount", "orderPrice", "realPrice",
)
TIME_KEYS = (
    "createTime", "createdTime", "orderTime", "create_time", "gmtCreate",
    "payTime", "createDate", "orderCreateTime", "submitTime",
)
STATUS_KEYS = (
    "orderStatus", "statusDesc", "orderStatusDesc", "statusText",
    "statusName", "orderStatusName", "status",
)
SHOP_KEYS = (
    "shopName", "mallName", "sellerName", "storeName", "merchantName",
    "sellerNick", "shop_name",
)
TRACKING_KEYS = (
    "trackingNo", "expressNo", "waybillNo", "logisticsNo", "mailNo",
    "tracking_no", "express_no",
)
IMAGE_KEYS = ("image", "itemPic", "picUrl", "imageUrl", "mainImage", "img", "thumb")
URL_KEYS = ("detailUrl", "orderUrl", "url", "itemUrl")

MONEY = re.compile(r"[-+]?[0-9][0-9,]*(?:\.[0-9]{1,2})?")
ORDER_ID_LABEL = re.compile(
    r"(?:订单号|订单编号|订单\s*ID)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9-]{7,39})",
    re.I,
)
DATE = re.compile(r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?:\s*\d{1,2}:\d{2}(?::\d{2})?)?)")
INDEX_NONE = object()


def _get(node: dict[str, Any], keys: Iterable[str]) -> Any:
    for key in keys:
        if key in node and node[key] not in (None, "", [], {}):
            return node[key]
    return INDEX_NONE


def _text(value: Any, *, depth: int = 0) -> str:
    if value is None or depth > 2:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        for key in TITLE_KEYS + ("name", "text", "value", "desc"):
            if key in value:
                found = _text(value[key], depth=depth + 1)
                if found:
                    return found
        return ""
    if isinstance(value, list):
        for item in value:
            found = _text(item, depth=depth + 1)
            if found:
                return found
    return ""


def _amount(value: Any, platform: str, key: str = "") -> float | None:
    if value is None or isinstance(value, (list, tuple)):
        return None
    if isinstance(value, dict):
        for name in ("value", "amount", "cent", "price", "payAmount"):
            if name in value:
                return _amount(value[name], platform, name)
        return None
    text = str(value).replace("¥", "").replace("￥", "").replace("元", "").replace(",", "")
    match = MONEY.search(text)
    if not match:
        return None
    try:
        number = abs(float(match.group(0)))
    except ValueError:
        return None
    if number <= 0 or number > 100_000_000:
        return None
    # PDD / Douyin JSON APIs report amounts in cents. Only convert when there
    # is no decimal point; a string like "29.90" is already yuan.
    cents_platform = platform in {"pdd", "douyin"} or "cent" in key.lower()
    if cents_platform and "." not in match.group(0) and number >= 100:
        number = number / 100
    return round(number, 2)


def _time(value: Any) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        try:
            stamp = float(value)
        except (TypeError, ValueError):
            return None
        if stamp > 10_000_000_000:  # milliseconds
            stamp /= 1000
        if stamp < 1_000_000_000:  # not a plausible timestamp
            return None
        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")
    text = str(value).strip()
    if not text:
        return None
    return normalize_time(text.replace("/", "-"))


def map_status(value: Any) -> tuple[str, str]:
    text = str(value or "").strip()
    if not text:
        return "unknown", ""
    if "退款" in text or "售后" in text and "关闭" in text:
        return "refunded", text
    if any(word in text for word in ("已关闭", "交易关闭", "已取消", "取消")):
        return "cancelled", text
    if any(word in text for word in ("已完成", "已收货", "已签收", "交易成功", "已送达", "完成")):
        return "completed", text
    if any(word in text for word in ("待收货", "已发货", "运输中", "派送", "待取件", "已出库")):
        return "shipped", text
    if any(word in text for word in ("待付款", "等待付款", "未付款")):
        return "pending_payment", text
    if any(word in text for word in ("待发货", "已付款", "处理中", "备货", "待处理")):
        return "processing", text
    if "售后" in text:
        return "after_sales", text
    return "unknown", text


# ---------------------------------------------------------------------------
# Taobao dedicated parser
# ---------------------------------------------------------------------------
def _find_key(node: Any, key: str, depth: int = 0) -> Any:
    if depth > 6:
        return None
    if isinstance(node, dict):
        if isinstance(node.get(key), list):
            return node[key]
        for value in node.values():
            found = _find_key(value, key, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_key(item, key, depth + 1)
            if found is not None:
                return found
    return None


def parse_taobao_payload(payload: Any) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    main = _find_key(payload, "mainOrders")
    if not isinstance(main, list):
        return orders

    for raw in main:
        if not isinstance(raw, dict):
            continue
        order_id = str(raw.get("orderId") or raw.get("bizOrderId") or "")
        if not order_id:
            continue
        status_info = raw.get("statusInfo") if isinstance(raw.get("statusInfo"), dict) else {}
        pay_info = raw.get("payInfo") if isinstance(raw.get("payInfo"), dict) else {}
        order_info = raw.get("orderInfo") if isinstance(raw.get("orderInfo"), dict) else {}
        seller = raw.get("seller") if isinstance(raw.get("seller"), dict) else {}
        sub_orders = raw.get("subOrders") if isinstance(raw.get("subOrders"), list) else []
        item_info: dict[str, Any] = {}
        if sub_orders and isinstance(sub_orders[0], dict):
            item_info = sub_orders[0].get("itemInfo") if isinstance(sub_orders[0].get("itemInfo"), dict) else {}
        status_text = str(status_info.get("text") or status_info.get("status") or "")
        status, status_text = map_status(status_text)
        orders.append(
            {
                "platform": "taobao",
                "platform_order_id": order_id,
                "title": str(item_info.get("title") or ""),
                "shop": str(seller.get("shopName") or seller.get("nick") or ""),
                "amount": _amount(pay_info.get("actualFee"), "taobao", "actualFee"),
                "status": status,
                "status_text": status_text,
                "order_time": _time(order_info.get("createTime") or raw.get("createTime")),
                "url": str(order_info.get("detailUrl") or item_info.get("itemUrl") or "") or None,
                "product_url": str(item_info.get("itemUrl") or "") or None,
                "image_url": str(item_info.get("pic") or "") or None,
                "source": "agent",
                "raw": {"_source": "web", "_platform": "taobao", "order": raw},
            }
        )
    return orders


# ---------------------------------------------------------------------------
# Generic JSON walker
# ---------------------------------------------------------------------------
def _candidate(node: dict[str, Any], platform: str) -> dict[str, Any] | None:
    id_value = _get(node, ID_KEYS.get(platform, ()))
    if id_value is INDEX_NONE:
        return None
    order_id = str(id_value).strip()
    if not (6 <= len(order_id) <= 40):
        return None

    title = _text(_get(node, TITLE_KEYS))
    if not title:
        for key in ("goods", "items", "itemList", "product", "productInfo", "sku", "item", "orderItem", "ware"):
            if key in node:
                title = _text(node[key])
                if title:
                    break
    amount_raw = _get(node, AMOUNT_KEYS)
    amount = _amount(amount_raw, platform) if amount_raw is not INDEX_NONE else None
    if amount is None:
        for key in ("payInfo", "priceInfo", "paymentInfo", "amountInfo", "pay"):
            value = node.get(key)
            if isinstance(value, dict):
                amount = _amount(value, platform)
                if amount is not None:
                    break
    order_time = _time(_get(node, TIME_KEYS)) if _get(node, TIME_KEYS) is not INDEX_NONE else None

    # Confidence gate: a nested item id plus one price is not an order.
    if not title and amount is None:
        return None
    if not title and not order_time:
        return None

    status_raw = _get(node, STATUS_KEYS)
    status, status_text = map_status(status_raw if status_raw is not INDEX_NONE else "")
    shop = _text(_get(node, SHOP_KEYS))
    tracking = _text(_get(node, TRACKING_KEYS))
    image = _text(_get(node, IMAGE_KEYS))
    url = _text(_get(node, URL_KEYS))
    logistics = _text(
        _get(node, ("logisticsText", "logisticsInfo", "logistics", "traceInfo", "expressInfo"))
    )

    return {
        "platform": platform,
        "platform_order_id": order_id,
        "title": title or f"订单 {order_id}",
        "shop": shop,
        "amount": amount,
        "status": status,
        "status_text": status_text,
        "order_time": order_time,
        "tracking_no": tracking or None,
        "logistics_text": logistics or None,
        "url": url or None,
        "product_url": url or None,
        "image_url": image or None,
        "source": "agent",
        "raw": {"_source": "web", "_platform": platform, "order": node},
    }


def _walk(node: Any, platform: str, found: list[dict[str, Any]], depth: int = 0) -> None:
    if depth > 9:
        return
    if isinstance(node, dict):
        order = _candidate(node, platform)
        if order:
            found.append(order)
        for value in node.values():
            _walk(value, platform, found, depth + 1)
    elif isinstance(node, list):
        for item in node:
            _walk(item, platform, found, depth + 1)


def extract_orders(payload: Any, platform: str) -> list[dict[str, Any]]:
    platform = platforms.normalize(platform)
    if platform == "taobao":
        dedicated = parse_taobao_payload(payload)
        if dedicated:
            return _dedupe(dedicated)

    found: list[dict[str, Any]] = []
    _walk(payload, platform, found)
    return _dedupe(found)


def _dedupe(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for order in orders:
        key = str(order.get("platform_order_id") or "")
        if not key:
            continue
        current = merged.get(key)
        if current is None:
            merged[key] = order
            continue
        for field in ("title", "shop", "amount", "status_text", "order_time", "tracking_no", "image_url", "url"):
            if not current.get(field) and order.get(field):
                current[field] = order[field]
        if current.get("status") in (None, "", "unknown") and order.get("status") not in (None, "", "unknown"):
            current["status"] = order["status"]
    return list(merged.values())


# ---------------------------------------------------------------------------
# Text fallback (server-rendered pages / no JSON captured)
# ---------------------------------------------------------------------------
def parse_order_text(text: str, platform: str) -> list[dict[str, Any]]:
    """Last-resort parser. Requires an explicit "订单号" label per block."""
    text = text or ""
    matches = list(ORDER_ID_LABEL.finditer(text))
    orders: list[dict[str, Any]] = []
    for index, match in enumerate(matches):
        start = max(0, match.start() - 200)
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 500)
        block = text[start:end]
        order_id = match.group(1).strip()
        if not (6 <= len(order_id) <= 40):
            continue
        amount_match = re.search(r"(?:实付款|实付|金额|合计|¥|￥)\s*[:：]?\s*[¥￥]?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", block)
        amount = _amount(amount_match.group(1), platform, "price") if amount_match else None
        time_match = DATE.search(block)
        order_time = _time(time_match.group(1)) if time_match else None
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        title = ""
        for line in lines:
            if 4 <= len(line) <= 60 and not any(
                word in line
                for word in ("订单号", "订单", "退款", "查看", "物流", "评价", "实付", "金额", "合计", "¥", "￥")
            ):
                title = line
                break
        if not title and amount is None:
            continue
        status_text = ""
        for line in lines:
            if any(word in line for word in ("待付款", "待发货", "待收货", "已完成", "已取消", "退款", "运输中", "已签收")):
                status_text = line[:40]
                break
        status, status_text = map_status(status_text)
        shop = next((line for line in lines if any(word in line for word in ("旗舰店", "专营店", "专卖店", "店")) and len(line) <= 40), "")
        orders.append(
            {
                "platform": platforms.normalize(platform),
                "platform_order_id": order_id,
                "title": title or f"订单 {order_id}",
                "shop": shop,
                "amount": amount,
                "status": status,
                "status_text": status_text,
                "order_time": order_time,
                "source": "agent",
                "raw": {"_source": "web_text", "_platform": platform, "block": block[:1000]},
            }
        )
    return _dedupe(orders)
