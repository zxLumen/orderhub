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

import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable

from .. import platforms
from ..db import normalize_time
from .html_dom import Node, parse_html

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
    orders = _parse_taobao_main(payload)
    if orders:
        return orders
    return _parse_taobao_graph(payload)


def _parse_taobao_main(payload: Any) -> list[dict[str, Any]]:
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
    if platform == "pdd":
        dedicated = parse_pdd_payload(payload)
        if dedicated:
            return _dedupe(dedicated)

    found: list[dict[str, Any]] = []
    _walk(payload, platform, found)
    return _dedupe(found)


# ---------------------------------------------------------------------------
# Pinduoduo dedicated parser
# ---------------------------------------------------------------------------
PDD_STATUS = {
    4: ("completed", "交易成功"),
    6: ("refunded", "退款成功"),
}
EMBEDDED_MARKERS: dict[str, tuple[str, ...]] = {
    "pdd": ("window.rawData=",),
}


def _balanced_json(text: str, start: int) -> Any | None:
    """Parse a JSON object starting at ``start`` with string-aware braces."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
    return None


def extract_embedded_payloads(html: str, platform: str) -> list[Any]:
    """Pull JSON the page embeds in the HTML instead of fetching over XHR.

    Pinduoduo renders its order list from ``window.rawData`` in the document, so
    the response sniffer never sees it.
    """
    payloads: list[Any] = []
    for marker in EMBEDDED_MARKERS.get(platform, ()):
        start = 0
        while True:
            index = html.find(marker, start)
            if index < 0:
                break
            begin = index + len(marker)
            while begin < len(html) and html[begin] in " \t\r\n":
                begin += 1
            if begin < len(html) and html[begin] == "{":
                payload = _balanced_json(html, begin)
                if payload is not None:
                    payloads.append(payload)
            start = max(begin, index + len(marker))
    return payloads


def _maybe(node: dict[str, Any], keys: Iterable[str]) -> Any:
    value = _get(node, keys)
    return None if value is INDEX_NONE else value


def _pick(node: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = node.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _web_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "https:" + text if text.startswith("//") else text


# ---------------------------------------------------------------------------
# Taobao graph payload (mtop.taobao.order.queryboughtlistv2)
# ---------------------------------------------------------------------------
def _taobao_graph(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        return data["data"]
    return data if isinstance(data, dict) else None


def _taobao_entry(graph: dict[str, Any], prefix: str, order_id: str) -> dict[str, Any]:
    exact = f"{prefix}_{order_id}"
    value = graph.get(exact)
    if isinstance(value, dict):
        return value.get("fields") or {}
    for key, candidate in graph.items():
        if key.startswith(exact) and isinstance(candidate, dict):
            return candidate.get("fields") or {}
    return {}


def _parse_taobao_graph(payload: Any) -> list[dict[str, Any]]:
    """New Taobao order list: a graph of shopInfo/orderItemInfo/orderPayment nodes."""
    graph = _taobao_graph(payload)
    if not graph:
        return []
    shops = [
        (key, value)
        for key, value in graph.items()
        if key.startswith("shopInfo") and isinstance(value, dict)
    ]
    orders: list[dict[str, Any]] = []
    for key, value in shops:
        fields = value.get("fields") or {}
        order_id = str(fields.get("orderId") or key.split("_", 1)[-1]).strip()
        if not order_id:
            continue

        item_fields = _taobao_entry(graph, "orderItemInfo", order_id)
        item = item_fields.get("item") if isinstance(item_fields.get("item"), dict) else {}

        payment = _taobao_entry(graph, "orderPayment", order_id)
        actual = payment.get("actualFee")
        actual_amount = actual.get("value") if isinstance(actual, dict) else actual

        status_text = str(fields.get("tradeTitle") or fields.get("orderStatus") or "").strip()
        status, status_text = map_status(status_text)

        orders.append(
            {
                "platform": "taobao",
                "platform_order_id": order_id,
                "title": str(item.get("title") or "")[:300],
                "shop": str(fields.get("shopName") or fields.get("sellerName") or "")[:120],
                "amount": _amount(actual_amount, "taobao", "actualFee"),
                "status": status,
                "status_text": status_text[:40],
                "order_time": _time(fields.get("createTime")),
                "tracking_no": None,
                "url": _web_url(fields.get("orderDetailUrl")) or None,
                "product_url": _web_url(item.get("itemUrl")) or None,
                "image_url": _web_url(item.get("pic")) or None,
                "source": "agent",
                "raw": {
                    "_source": "web",
                    "_platform": "taobao",
                    "order": {"shop": fields, "item": item, "payment": payment},
                },
            }
        )
    return orders


def parse_pdd_payload(payload: Any) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    raw_orders = _find_key(payload, "orders")
    if not isinstance(raw_orders, list):
        return orders

    for raw in raw_orders:
        if not isinstance(raw, dict):
            continue
        order_id = str(
            _pick(raw, "orderSn", "order_sn", "orderId", "order_id") or ""
        ).strip()
        if not order_id:
            continue

        goods = [
            item
            for item in (raw.get("orderGoods") or raw.get("order_goods") or [])
            if isinstance(item, dict)
        ]
        titles: list[str] = []
        for item in goods:
            name = str(item.get("goodsName") or item.get("goods_name") or "").strip()
            if name and name not in titles:
                titles.append(name)
        title = " / ".join(titles) if titles else _text(_maybe(raw, TITLE_KEYS))

        mall = raw.get("mall") if isinstance(raw.get("mall"), dict) else {}
        shop = (
            str(mall.get("mallName") or mall.get("mall_name") or "").strip()
            or _text(_maybe(raw, SHOP_KEYS))
        )

        prompt = str(
            raw.get("orderStatusPrompt")
            or raw.get("order_status_prompt")
            or raw.get("statusDesc")
            or ""
        ).strip()
        status, status_text = map_status(prompt)
        if status == "unknown":
            generic_status = _maybe(raw, STATUS_KEYS)
            if isinstance(generic_status, str) and generic_status.strip():
                status, status_text = map_status(generic_status)
            if status == "unknown":
                try:
                    code = int(raw.get("status") or 0)
                except (TypeError, ValueError):
                    code = 0
                status, status_text = PDD_STATUS.get(code, ("unknown", prompt))

        amount = None
        for key in (
            "orderAmount",
            "order_amount",
            "displayAmount",
            "display_amount",
            "payAmount",
            "pay_amount",
            "actualFee",
        ):
            if raw.get(key) is not None:
                amount = _amount(raw[key], "pdd", key)
                if amount is not None:
                    break
        if amount is None:
            amount = _amount(_maybe(raw, AMOUNT_KEYS), "pdd")

        link = str(_pick(raw, "orderLinkUrl", "order_link_url") or "").strip()
        if not link:
            link = _text(_maybe(raw, URL_KEYS))
        if link and not link.startswith("http"):
            link = "https://mobile.yangkeduo.com/" + link.lstrip("/")

        image = str(
            (goods[0].get("thumbUrl") or goods[0].get("thumb_url") if goods else "") or ""
        ).strip()
        if not image:
            image = _text(_maybe(raw, IMAGE_KEYS))

        tracking = str(
            _pick(raw, "trackingNumber", "tracking_number") or ""
        ).strip() or _text(_maybe(raw, TRACKING_KEYS))

        orders.append(
            {
                "platform": "pdd",
                "platform_order_id": order_id,
                "title": title[:300],
                "shop": shop[:120],
                "amount": amount,
                "status": status,
                "status_text": status_text[:40],
                "order_time": _time(
                    _pick(raw, "orderTime", "order_time") or _maybe(raw, TIME_KEYS)
                ),
                "tracking_no": tracking or None,
                "url": link or None,
                "product_url": None,
                "image_url": image or None,
                "source": "agent",
                "raw": {"_source": "web", "_platform": "pdd", "order": raw},
            }
        )
    return orders


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


def parse_html_orders(html: str, platform: str) -> list[dict[str, Any]]:
    """Extract orders from platform-rendered HTML (no JSON available)."""
    platform = platforms.normalize(platform)
    if platform == "pdd":
        found: list[dict[str, Any]] = []
        for payload in extract_embedded_payloads(html, platform):
            found.extend(extract_orders(payload, platform))
        return _dedupe(found)
    if platform == "xianyu":
        return parse_xianyu_html(html)
    return []


# ---------------------------------------------------------------------------
# Xianyu server-rendered order list
# ---------------------------------------------------------------------------
XIANYU_STATUS_WORDS = (
    "交易成功",
    "交易关闭",
    "等待",
    "待付款",
    "待发货",
    "待收货",
    "退款",
    "已发货",
    "已完成",
)


def _has_class_prefix(node: Node, prefix: str) -> bool:
    return any(
        cls == prefix or cls.startswith(prefix + "-") or cls.startswith(prefix + "--")
        for cls in node.classes
    )


def _find_prefix(node: Node, prefix: str) -> Node | None:
    for child in node.iter():
        if child is node:
            continue
        if _has_class_prefix(child, prefix):
            return child
    return None


def parse_xianyu_html(html: str) -> list[dict[str, Any]]:
    """Parse ``goofish.com/bought`` cards.

    Each card is server-rendered: a seller, a status, a product link carrying
    ``orderId``, a thumbnail and a price. The list has no "订单号" text, so the
    generic text fallback cannot see it.
    """
    doc = parse_html(html or "")
    orders: list[dict[str, Any]] = []
    seen: set[str] = set()

    for anchor in doc.find_all("a"):
        href = str(anchor.attrs.get("href") or "").strip()
        match = re.search(r"order-detail\?orderId=(\d+)", href)
        if not match:
            continue
        order_id = match.group(1)
        if order_id in seen:
            continue
        seen.add(order_id)

        card: Node = anchor
        for _ in range(7):
            if card.parent is None:
                break
            card = card.parent
            text = card.text()
            if "¥" in text and any(word in text for word in XIANYU_STATUS_WORDS):
                break

        title_node = _find_prefix(anchor, "name")
        title = title_node.text().strip() if title_node else anchor.text().strip()

        seller = ""
        for node in card.iter():
            if node is card:
                continue
            if _has_class_prefix(node, "name"):
                value = node.text().strip()
                if value and value != title:
                    seller = value
                    break

        price_node = _find_prefix(card, "price")
        amount = _amount(price_node.text(), "xianyu", "price") if price_node else None

        status_node = _find_prefix(card, "status")
        status_text = status_node.text().strip() if status_node else ""
        status, status_text = map_status(status_text)

        image = ""
        picture = _find_prefix(card, "picture")
        if picture is not None:
            for img in picture.find_all("img"):
                value = str(img.attrs.get("src") or "").strip()
                if value:
                    image = "https:" + value if value.startswith("//") else value
                    break

        orders.append(
            {
                "platform": "xianyu",
                "platform_order_id": order_id,
                "title": title[:300],
                "shop": seller[:120],
                "amount": amount,
                "status": status,
                "status_text": status_text[:40],
                "order_time": None,
                "tracking_no": None,
                "url": href,
                "product_url": None,
                "image_url": image or None,
                "source": "agent",
                "raw": {
                    "_source": "web",
                    "_platform": "xianyu",
                    "text": card.text()[:1500],
                },
            }
        )
    return orders


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
