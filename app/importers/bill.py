"""Alipay / WeChat Pay bill -> normalized orders.

Bills are the only cross-platform source that needs no login and no scraper:
Taobao, Pinduoduo, Xianyu, Zhuanzhuan, Douyin and Meituan payments all show up
in one of the two wallets. Titles are less rich than a platform export, but the
order id, merchant, amount and time are exact.
"""

from __future__ import annotations

import re
from typing import Any

from .. import platforms
from .files import Table

MONEY = re.compile(r"[-+]?[0-9][0-9,]*(?:\.[0-9]{1,2})?")


def _get(row: dict[str, str], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value:
            return value.strip()
    return ""


def _money(value: str) -> float | None:
    text = (value or "").replace("¥", "").replace("￥", "").replace("元", "").replace(",", "")
    match = MONEY.search(text)
    if not match:
        return None
    try:
        return abs(float(match.group(0)))
    except ValueError:
        return None


def _status(text: str) -> tuple[str, str]:
    value = (text or "").strip()
    if not value:
        return "unknown", ""
    if "退款" in value:
        return "refunded", value
    if any(word in value for word in ("关闭", "取消", "已失效")):
        return "cancelled", value
    if any(word in value for word in ("成功", "已完成", "已收货", "已签收", "交易完成")):
        return "completed", value
    if any(word in value for word in ("发货", "运输", "派送", "待收货", "确认收货")):
        return "shipped", value
    if any(word in value for word in ("待付款", "等待付款", "未付款")):
        return "pending_payment", value
    if any(word in value for word in ("付款", "已支付", "处理中", "待发货")):
        return "processing", value
    return "unknown", value


SHOPPING_CATEGORIES = (
    "购物",
    "数码",
    "服饰",
    "日用",
    "食品",
    "家居",
    "美容",
    "母婴",
    "运动",
    "图书",
    "宠物",
    "汽车",
    "医疗",
    "餐饮",
    "外卖",
    "文娱",
)

NON_ORDER_TYPES = (
    "转账",
    "红包",
    "群收款",
    "零钱通",
    "理财",
    "信用卡还款",
    "余额宝",
    "充值",
    "提现",
    "保险",
    "基金",
)


def detect(headers: list[str]) -> str:
    keys = set(headers)
    if {"交易分类", "收/支", "交易订单号"} & keys and "交易时间" in keys:
        if "商家订单号" in keys or "交易订单号" in keys:
            return "alipay"
    if "交易单号" in keys and "商户单号" in keys:
        return "wechat"
    if "交易时间" in keys and "收/支" in keys:
        return "wechat" if "支付方式" in keys else "alipay"
    return ""


def _alipay_platform(row: dict[str, str]) -> str:
    text = " ".join(
        [
            _get(row, "交易对方"),
            _get(row, "商品说明"),
            _get(row, "商品名称"),
            _get(row, "交易分类"),
            _get(row, "备注"),
        ]
    )
    return platforms.infer(text)


def _wechat_platform(row: dict[str, str]) -> str:
    text = " ".join(
        [
            _get(row, "交易对方"),
            _get(row, "商品"),
            _get(row, "商品说明"),
            _get(row, "交易类型"),
            _get(row, "备注"),
        ]
    )
    return platforms.infer(text)


def to_orders(table: Table, *, kind: str, include_unknown: bool = False) -> tuple[list[dict[str, Any]], dict[str, int]]:
    orders: list[dict[str, Any]] = []
    stats = {"rows": 0, "skipped_income": 0, "skipped_type": 0, "skipped_unknown": 0, "skipped_invalid": 0}

    for row in table.rows:
        stats["rows"] += 1
        direction = _get(row, "收/支", "收支")
        if direction and direction not in {"支出", "支"}:
            stats["skipped_income"] += 1
            continue

        trade_type = _get(row, "交易类型", "类型", "交易分类")
        if any(word in trade_type for word in NON_ORDER_TYPES):
            stats["skipped_type"] += 1
            continue

        platform = _alipay_platform(row) if kind == "alipay" else _wechat_platform(row)
        category = _get(row, "交易分类")
        if platform == "other" and not include_unknown:
            if not any(word in category for word in SHOPPING_CATEGORIES):
                stats["skipped_unknown"] += 1
                continue

        title = _get(row, "商品说明", "商品名称", "商品", "交易内容")
        shop = _get(row, "交易对方", "商户名称", "对方")
        if not title and not shop:
            stats["skipped_invalid"] += 1
            continue

        amount = _money(_get(row, "金额", "金额(元)", "订单金额"))
        order_time = _get(row, "交易时间", "创建时间", "付款时间")
        status, status_text = _status(_get(row, "交易状态", "当前状态", "状态"))
        order_id = _get(row, "商家订单号", "商户单号", "交易订单号", "交易单号")
        if not order_id:
            # The wallet trade number is stable for one payment; it is a valid
            # dedupe key even when the marketplace order id is absent.
            order_id = _get(row, "交易订单号", "交易单号")

        orders.append(
            {
                "platform": platform,
                "platform_order_id": order_id,
                "title": title or trade_type or shop,
                "shop": shop,
                "amount": amount,
                "status": status,
                "status_text": status_text,
                "order_time": order_time,
                "source": kind,
                "raw": {
                    "_source": kind,
                    "_category": category or trade_type,
                    "row": row,
                },
            }
        )

    return orders, stats
