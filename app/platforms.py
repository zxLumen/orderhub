"""Platform registry.

OrderHub started with JD only. Every other marketplace differs wildly in how
much data it exposes, so each platform declares *which channels* can deliver
real orders instead of promising a scraper that does not exist:

* ``bill``   – the platform can be reconstructed from Alipay / WeChat bills.
* ``export`` – the platform offers a personal-data copy / order export file.
* ``sms``    – shipping SMS carries tracking numbers for this platform.
* ``agent``  – the Windows Chrome agent can collect the order list.
* ``manual`` – manual add / paste is available.

The web UI reads this registry through ``GET /api/platforms`` so the coverage
matrix shown to the user stays honest.
"""

from __future__ import annotations

from typing import Any

PLATFORMS: dict[str, dict[str, Any]] = {
    "jd": {
        "label": "京东",
        "order_url": "https://order.jd.com/center/list.action",
        "login_url": "https://passport.jd.com/new/login.aspx",
        "channels": ["agent", "export", "sms", "bill", "manual"],
        "import_hint": "京东 App → 我的 → 客户服务 → 个人信息副本；或继续用 Windows 采集 agent。",
        "keywords": ("京东", "京东商城", "京东自营", "京东物流", "京东健康", "京东超市"),
    },
    "taobao": {
        "label": "淘宝/天猫",
        "order_url": "https://buyertrade.taobao.com/trade/itemlist/list_bought_items.htm",
        "login_url": "https://login.taobao.com/",
        "channels": ["export", "sms", "bill", "manual"],
        "import_hint": "淘宝 App → 我的淘宝 → 设置 → 隐私 → 个人信息副本（订单）；或用支付宝账单导入。",
        "keywords": ("淘宝", "天猫", "阿里", "淘宝网", "天猫超市", "天猫国际", "淘工厂"),
    },
    "pdd": {
        "label": "拼多多",
        "order_url": "https://mobile.yangkeduo.com/orders.html",
        "login_url": "https://mobile.yangkeduo.com/login.html",
        "channels": ["export", "sms", "bill", "manual"],
        "import_hint": "拼多多 App → 个人中心 → 设置 → 隐私 → 个人信息副本；微信支付账单也能补到大部分订单。",
        "keywords": ("拼多多", "寻梦", "上海寻梦", "多多买菜", "拼小圈", "PDD"),
    },
    "douyin": {
        "label": "抖音",
        "order_url": "https://www.douyin.com/",
        "login_url": "https://www.douyin.com/",
        "channels": ["export", "sms", "bill", "manual"],
        "import_hint": "抖音 App → 我 → 抖音商城 → 订单 → 客服/设置 → 个人信息副本；支付记录在支付宝/微信账单里。",
        "keywords": ("抖音", "抖店", "抖音商城", "字节", "微播视界", "火山引擎", "抖音电商"),
    },
    "xianyu": {
        "label": "闲鱼",
        "order_url": "https://www.goofish.com/",
        "login_url": "https://www.goofish.com/",
        "channels": ["export", "sms", "bill", "manual"],
        "import_hint": "闲鱼 App → 我的 → 设置 → 隐私 → 个人信息副本；买入订单也可用支付宝账单补。",
        "keywords": ("闲鱼", "goofish", "闲鱼小铺"),
    },
    "zhuanzhuan": {
        "label": "转转",
        "order_url": "https://www.zhuanzhuan.com/",
        "login_url": "https://www.zhuanzhuan.com/",
        "channels": ["export", "sms", "bill", "manual"],
        "import_hint": "转转 App → 我的 → 设置 → 隐私 → 个人信息副本；支付记录在支付宝/微信账单里。",
        "keywords": ("转转", "转转二手", "zhuanzhuan", "采货侠"),
    },
    "meituan": {
        "label": "美团",
        "order_url": "https://www.meituan.com/",
        "login_url": "https://passport.meituan.com/",
        "channels": ["sms", "bill", "manual"],
        "import_hint": "外卖/买菜订单从支付宝或微信账单导入，配送状态靠短信。",
        "keywords": ("美团", "美团外卖", "美团买菜", "小象超市", "三快"),
    },
    "eleme": {
        "label": "饿了么",
        "order_url": "https://www.ele.me/",
        "login_url": "https://login.taobao.com/",
        "channels": ["sms", "bill", "manual"],
        "import_hint": "饿了么订单从支付宝账单导入，配送状态靠短信。",
        "keywords": ("饿了么", "饿了么星选", "ele.me"),
    },
    "vip": {
        "label": "唯品会",
        "order_url": "https://order.vip.com/",
        "login_url": "https://passport.vip.com/",
        "channels": ["bill", "sms", "manual"],
        "import_hint": "唯品会支持订单导出；支付宝/微信账单也能补。",
        "keywords": ("唯品会", "vip.com"),
    },
    "xiaohongshu": {
        "label": "小红书",
        "order_url": "https://www.xiaohongshu.com/",
        "login_url": "https://www.xiaohongshu.com/",
        "channels": ["bill", "sms", "manual"],
        "import_hint": "小红书订单从支付宝/微信账单导入。",
        "keywords": ("小红书", "行吟信息", "薯店"),
    },
    "other": {
        "label": "其他",
        "order_url": "",
        "login_url": "",
        "channels": ["bill", "manual"],
        "import_hint": "未知商家按「其他」归档，仍可搜索和导出。",
        "keywords": (),
    },
}

# Aliases accepted by importers / API / older rows.
ALIASES = {
    "tmall": "taobao",
    "taobao/tmall": "taobao",
    "pinduoduo": "pdd",
    "yangkeduo": "pdd",
    "douyin_shop": "douyin",
    "goofish": "xianyu",
    "zhuanzhuan2": "zhuanzhuan",
    "vip.com": "vip",
    "red": "xiaohongshu",
    "xhs": "xiaohongshu",
    "unknown": "other",
    "": "other",
}

# Brand order matters: 闲鱼/转转 are marketplaces whose payment rows also
# mention 支付宝/淘宝, and 抖音 payments go through 字节/微播视界.
_INFER_ORDER = (
    "xianyu",
    "zhuanzhuan",
    "pdd",
    "douyin",
    "taobao",
    "jd",
    "meituan",
    "eleme",
    "vip",
    "xiaohongshu",
)


def normalize(platform: str | None) -> str:
    value = (platform or "").strip().lower()
    value = ALIASES.get(value, value)
    return value if value in PLATFORMS else "other"


def label(platform: str | None) -> str:
    return PLATFORMS[normalize(platform)]["label"]


def infer(text: str, *, default: str = "other") -> str:
    """Guess the platform from merchant / product / SMS text."""
    lowered = (text or "").lower()
    if not lowered:
        return default
    for key in _INFER_ORDER:
        if any(word.lower() in lowered for word in PLATFORMS[key]["keywords"]):
            return key
    return default


def channels(platform: str) -> list[str]:
    return list(PLATFORMS[normalize(platform)]["channels"])


def registry() -> list[dict[str, Any]]:
    """Serializable registry without internal keyword lists."""
    result = []
    for key, item in PLATFORMS.items():
        result.append(
            {
                "id": key,
                "label": item["label"],
                "order_url": item["order_url"],
                "login_url": item["login_url"],
                "channels": list(item["channels"]),
                "import_hint": item["import_hint"],
            }
        )
    return result
