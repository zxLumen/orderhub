"""Best-effort parser for the JD PC order list page.

JD changes its markup often. Two layouts are supported:

* 2026 CSS Modules layout: class names are hashed per release
  (``orderCard-b731ca``, ``orderTime-4e55a3``, ...), so elements are matched by
  class *prefix* and never by the full hashed name.
* Legacy layout: ``order-item`` / ``data-orderid`` blocks.

When neither matches, the collector dumps the HTML to disk; only this file
should need updating after a redesign.
"""

from __future__ import annotations

import re
from typing import Any

from .html_dom import Node, first_text, parse_html

ORDER_ID_TEXT = re.compile(r"订单号\s*[:：]?\s*([A-Za-z0-9-]{6,})")
TRACKING_TEXT = re.compile(r"(?:运单号|快递单号)\s*[:：]?\s*([A-Za-z0-9-]{8,24})")
MONEY = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
TIME = re.compile(r"(20\d{2}[-/]\d{2}[-/]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)")
ORDER_TIME_NEAR = re.compile(
    r"(?:订单号|下单时间|提交时间)[^\n]{0,40}?(20\d{2}[-/]\d{2}[-/]\d{2}"
    r"(?:\s+\d{2}:\d{2}(?::\d{2})?)?)"
)

# Block-level class prefixes across JD layouts (hash suffix is ignored).
CARD_PREFIXES = (
    "orderCard",
    "orderItem",
    "order-item",
    "order-tb-item",
    "order-list-item",
)
LEGACY_CARD_CLASSES = {"order-item", "order-item-wrap", "order-tb-item", "order-list-item"}

STATUS_MAP = [
    ("cancelled", ("已取消", "取消")),
    ("refunded", ("退款", "退货")),
    ("after_sales", ("售后", "换货", "维修")),
    ("completed", ("已完成", "已签收", "交易完成", "已收货")),
    ("shipped", ("已发货", "运输中", "派送中", "待收货", "等待收货", "已出库", "商品出库", "出库")),
    ("processing", ("待发货", "处理中", "备货", "正在出库", "等待发货", "付款成功", "已付款")),
    ("pending_payment", ("待付款", "等待付款", "未付款")),
]


# ---------------------------------------------------------------------------
# Class matching
# ---------------------------------------------------------------------------
def _has_prefix(node: Node, prefix: str) -> bool:
    """True when the node has ``prefix`` or ``prefix-<hash>`` in its class list."""
    return any(cls == prefix or cls.startswith(prefix + "-") for cls in node.classes)


def _find_prefix(node: Node, *prefixes: str, tag: str | None = None) -> Node | None:
    for child in node.iter():
        if child is node:
            continue
        if tag and child.tag != tag:
            continue
        if any(_has_prefix(child, prefix) for prefix in prefixes):
            return child
    return None


def _find_all_prefix(node: Node, *prefixes: str) -> list[Node]:
    return [
        child
        for child in node.iter()
        if child is not node and any(_has_prefix(child, prefix) for prefix in prefixes)
    ]


def _text_of(node: Node | None) -> str:
    if node is None:
        return ""
    return node.text().strip()


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------
def _status(text: str) -> tuple[str, str]:
    for status, words in STATUS_MAP:
        if any(word in text for word in words):
            return status, text.strip()[:40]
    return "unknown", text.strip()[:40]


def _class_text(node: Node, *class_names: str) -> str:
    for name in class_names:
        found = node.find(class_=name)
        if found:
            value = found.text().strip()
            if value:
                return value
    return ""


def _money(text: str) -> float | None:
    match = MONEY.search(text)
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", ""))
    except ValueError:
        return None


def _order_time(block: Node, text: str) -> str | None:
    # 2026 layout: dedicated orderTime element holding the timestamp.
    hashed = _find_prefix(block, "orderTime")
    if hashed is not None:
        match = TIME.search(hashed.text())
        if match:
            return match.group(1).replace("/", "-")

    # Legacy layout: prefer known class names; promo banners also carry dates.
    for class_name in ("dealtime", "order-time", "order-date", "time"):
        value = _class_text(block, class_name)
        match = TIME.search(value)
        if match:
            return match.group(1).replace("/", "-")
    match = ORDER_TIME_NEAR.search(text)
    if match:
        return match.group(1).replace("/", "-")
    match = TIME.search(text)
    return match.group(1).replace("/", "-") if match else None


def _candidate_blocks(doc: Node) -> list[Node]:
    blocks: list[Node] = []
    for node in doc.iter():
        if node is doc:
            continue
        has_order_attr = any(
            key in node.attrs for key in ("data-orderid", "data-order-id", "orderid")
        )
        has_order_class = bool(LEGACY_CARD_CLASSES & node.classes) or any(
            _has_prefix(node, prefix) for prefix in CARD_PREFIXES
        )
        is_table_row = node.tag in {"tbody", "tr"} and "tr" in node.classes
        if has_order_attr or has_order_class or is_table_row:
            blocks.append(node)

    # Keep the outermost block for each order; JD often wraps the same order in
    # a larger card and also tags a nested node.
    block_ids = {id(node) for node in blocks}
    outer: list[Node] = []
    for node in blocks:
        parent = node.parent
        nested = False
        while parent is not None:
            if id(parent) in block_ids:
                nested = True
                break
            parent = parent.parent
        if not nested:
            outer.append(node)
    return outer


def _order_id(node: Node) -> str:
    for key in ("data-orderid", "data-order-id", "orderid", "data-id"):
        value = (node.attrs.get(key) or "").strip()
        if value and re.fullmatch(r"[A-Za-z0-9-]{6,}", value):
            return value

    # 2026 layout: <div class="orderMetas-xxx"><span>订单号</span>
    #               <div class="metaValue-xxx" title="3596...">3596...</div>
    metas = _find_prefix(node, "orderMetas")
    if metas is not None:
        for child in metas.iter():
            value = (child.attrs.get("title") or "").strip()
            if value and re.fullmatch(r"[A-Za-z0-9-]{6,}", value):
                return value
        match = ORDER_ID_TEXT.search(metas.text())
        if match:
            return match.group(1)

    match = ORDER_ID_TEXT.search(node.text())
    return match.group(1) if match else ""


def _product_titles(node: Node) -> list[str]:
    titles: list[str] = []
    for item in _find_all_prefix(node, "productTitle"):
        value = (item.attrs.get("title") or item.text()).strip()
        if value and value not in titles:
            titles.append(value)
    return titles


def _title_and_url(node: Node) -> tuple[str, str]:
    titles = _product_titles(node)

    links = [
        link
        for link in node.find_all("a")
        if "item.jd.com" in (link.attrs.get("href") or "")
        or "item.m.jd.com" in (link.attrs.get("href") or "")
    ]
    url = ""
    if links:
        url = links[0].attrs.get("href") or ""
        if url.startswith("//"):
            url = "https:" + url

    if titles:
        return " / ".join(titles), url

    if links:
        links.sort(key=lambda link: len(link.text()), reverse=True)
        title = links[0].text().strip()
        if title:
            return title, url

    title = first_text(
        node,
        ("div", "p-name"),
        ("span", "p-name"),
        ("div", "item-name"),
        ("span", "item-name"),
        ("div", "goods-name"),
        ("div", "product-name"),
    )
    return title, url


def _amount(node: Node, text: str) -> float | None:
    # 2026 layout splits the price into integer/decimal spans.
    integer = _find_prefix(node, "priceInteger")
    if integer is not None:
        decimal = _find_prefix(node, "priceDecimal")
        raw = integer.text().strip().replace(",", "")
        if decimal is not None:
            raw += decimal.text().strip()
        try:
            return float(raw)
        except ValueError:
            pass

    value = _money(_class_text(node, "amount", "order-total", "total-price"))
    if value is not None:
        return value
    return _money(text)


def _shop(node: Node) -> str:
    hashed = _find_prefix(node, "shopNames")
    if hashed is not None:
        value = (hashed.attrs.get("title") or hashed.text()).strip()
        if value:
            return value
    return _class_text(node, "shop-name", "store-name", "vender-name", "shop")


def _status_text(node: Node) -> str:
    hashed = _find_prefix(node, "statusText")
    if hashed is not None:
        value = hashed.text().strip()
        if value:
            return value
    return _class_text(node, "status", "order-status")


def _logistics(node: Node) -> str:
    hashed = _find_prefix(node, "detailText")
    return _text_of(hashed)


def _image_url(node: Node) -> str:
    """First real product thumbnail; skips lazy-load placeholders and icons.

    JD serves product images from ``img*.360buyimg.com`` on ``/n0/`` paths,
    while status/toolbar icons live under ``/img/jfs/`` and are ignored.
    """
    anchor = _find_prefix(node, "goodImg")
    roots = [anchor, node] if anchor is not None else [node]
    for root in roots:
        for img in root.find_all("img"):
            for key in ("data-src", "data-lazy-img", "src"):
                value = (img.attrs.get(key) or "").strip()
                if not value or value.startswith("data:"):
                    continue
                if "360buyimg.com" not in value:
                    continue
                if "/n0/" not in value and "/n1/" not in value and "s500x500" not in value:
                    continue
                return "https:" + value if value.startswith("//") else value
    return ""


def _quantity(node: Node) -> str:
    return _text_of(_find_prefix(node, "countNum"))


def parse_jd_orders(html: str) -> list[dict[str, Any]]:
    doc = parse_html(html)
    orders: list[dict[str, Any]] = []
    seen: set[str] = set()

    for block in _candidate_blocks(doc):
        order_id = _order_id(block)
        title, url = _title_and_url(block)
        text = block.text()
        if not order_id and not title:
            continue
        if order_id and order_id in seen:
            continue
        if order_id:
            seen.add(order_id)

        status_text = _status_text(block)
        status, status_norm = _status(status_text or text)
        tracking_match = TRACKING_TEXT.search(text)
        orders.append(
            {
                "platform": "jd",
                "platform_order_id": order_id or None,
                "title": title[:300],
                "shop": _shop(block)[:120],
                "amount": _amount(block, text),
                "status": status,
                "status_text": status_norm or status_text[:40],
                "order_time": _order_time(block, text),
                "tracking_no": tracking_match.group(1) if tracking_match else None,
                "url": "",
                "product_url": url,
                "image_url": _image_url(block),
                "logistics_text": _logistics(block)[:500],
                "raw": {
                    "order_id": order_id,
                    "logistics": _logistics(block)[:500],
                    "quantity": _quantity(block),
                    "text": text[:2000],
                },
            }
        )
    return orders


# ---------------------------------------------------------------------------
# JD list API (pcorder_center_list)
# ---------------------------------------------------------------------------
def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _absolute_url(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return "https:" + text if text.startswith("//") else text


def order_from_api(raw: dict[str, Any]) -> dict[str, Any]:
    """Map one ``pcorder_center_list`` order object to the OrderHub shape.

    Everything the API returns is kept in ``raw`` so future features can read
    new fields from the database without collecting again.
    """
    wares = [ware for ware in (raw.get("wareList") or []) if isinstance(ware, dict)]
    titles: list[str] = []
    for ware in wares:
        name = str(ware.get("name") or "").strip()
        if name and name not in titles:
            titles.append(name)

    status_text = str((raw.get("statusInfo") or {}).get("orderStateName") or "").strip()
    status = _status(status_text)[0] if status_text else "unknown"

    pay = raw.get("itemPayInfo") or {}
    amount = _as_float(pay.get("shouldPay"))
    if amount is None:
        amount = _as_float(pay.get("fistPrice"))
    if amount is None:
        amount = _as_float(pay.get("totalPrice"))

    detail = raw.get("itemDetail") or {}
    shop = raw.get("shopInfo") or {}

    return {
        "platform": "jd",
        "platform_order_id": str(raw.get("orderId") or ""),
        "title": " / ".join(titles)[:300],
        "shop": str(shop.get("shopName") or "")[:120],
        "amount": amount,
        "status": status,
        "status_text": status_text[:40],
        "order_time": raw.get("dateSubmit"),
        "tracking_no": None,
        "url": _absolute_url(detail.get("itemUrl")),
        "product_url": _absolute_url(wares[0].get("wareUrl")) if wares else "",
        "image_url": _absolute_url(wares[0].get("imgPath")) if wares else "",
        "logistics_text": "",
        "raw": raw,
    }
