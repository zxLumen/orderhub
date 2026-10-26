"""Best-effort parser for the JD PC order list page.

JD changes its markup often. The parser tries several layouts, keeps the raw
HTML on disk when it finds nothing, and is covered by a fixture test. If JD
redesigns the page, only this file needs updating.
"""

from __future__ import annotations

import re
from typing import Any

from .html_dom import Node, first_text, parse_html

ORDER_ID_TEXT = re.compile(r"订单号\s*[:：]?\s*([0-9]{6,})")
TRACKING_TEXT = re.compile(r"(?:运单号|快递单号)\s*[:：]?\s*([A-Za-z0-9-]{8,24})")
MONEY = re.compile(r"[¥￥]\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)")
TIME = re.compile(r"(20\d{2}-\d{2}-\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)")

STATUS_MAP = [
    ("cancelled", ("已取消", "取消")),
    ("refunded", ("退款", "退货")),
    ("after_sales", ("售后", "换货", "维修")),
    ("completed", ("已完成", "已签收", "交易完成")),
    ("shipped", ("已发货", "运输中", "派送中", "待收货", "已出库")),
    ("processing", ("待发货", "处理中", "备货", "正在出库")),
    ("pending_payment", ("待付款", "等待付款", "未付款")),
]


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


def _candidate_blocks(doc: Node) -> list[Node]:
    blocks: list[Node] = []
    for node in doc.iter():
        if node is doc:
            continue
        has_order_attr = any(
            key in node.attrs for key in ("data-orderid", "data-order-id", "orderid")
        )
        has_order_class = bool(
            {"order-item", "order-item-wrap", "order-tb-item", "order-list-item"}
            & node.classes
        )
        is_table_row = node.tag in {"tbody", "tr"} and "tr" in node.classes
        if has_order_attr or has_order_class or is_table_row:
            blocks.append(node)

    # Keep the outermost block for each order; JD often wraps data-orderid in a
    # larger card and also adds it to a nested node.
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
    text = node.text()
    match = ORDER_ID_TEXT.search(text)
    return match.group(1) if match else ""


def _title_and_url(node: Node) -> tuple[str, str]:
    links = [
        link
        for link in node.find_all("a")
        if "item.jd.com" in (link.attrs.get("href") or "")
        or "item.m.jd.com" in (link.attrs.get("href") or "")
    ]
    if links:
        links.sort(key=lambda link: len(link.text()), reverse=True)
        title = links[0].text().strip()
        url = links[0].attrs.get("href") or ""
        if url.startswith("//"):
            url = "https:" + url
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
    return title, ""


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

        shop = _class_text(
            block,
            "shop-name",
            "store-name",
            "vender-name",
            "shop",
        )
        status_text = _class_text(block, "status", "order-status")
        status, status_norm = _status(status_text or text)
        amount = _money(_class_text(block, "amount", "order-total", "total-price"))
        if amount is None:
            amount = _money(text)
        tracking_match = TRACKING_TEXT.search(text)
        time_match = TIME.search(text)
        orders.append(
            {
                "platform": "jd",
                "platform_order_id": order_id or None,
                "title": title[:300],
                "shop": shop[:120],
                "amount": amount,
                "status": status,
                "status_text": status_norm or status_text[:40],
                "order_time": time_match.group(1) if time_match else None,
                "tracking_no": tracking_match.group(1) if tracking_match else None,
                "url": url,
                "raw": {"order_id": order_id, "text": text[:2000]},
            }
        )
    return orders
