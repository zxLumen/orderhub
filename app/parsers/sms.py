"""Heuristic parser for Chinese delivery / order SMS messages.

Carriers and platforms change wording constantly, so this parser is deliberately
forgiving: it extracts what it can, stores the raw text, and never raises on
unexpected input. Every field is optional; the raw message remains searchable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Carriers
# ---------------------------------------------------------------------------

CARRIER_NAMES = {
    "sf": "顺丰速运",
    "jd": "京东物流",
    "yto": "圆通速递",
    "zto": "中通快递",
    "sto": "申通快递",
    "yunda": "韵达速递",
    "ems": "中国邮政EMS",
    "jtexpress": "极兔速递",
    "debang": "德邦快递",
    "cainiao": "菜鸟",
    "unknown": "未知承运商",
}

CARRIER_KEYWORDS = [
    ("sf", ("顺丰", "丰巢", "sf")),
    ("jd", ("京东物流", "京东快递", "京东")),
    ("yto", ("圆通", "yto")),
    ("zto", ("中通", "zto")),
    ("sto", ("申通", "sto")),
    ("yunda", ("韵达", "yunda")),
    ("ems", ("邮政", "ems", "中国邮政")),
    ("jtexpress", ("极兔", "j&t", "jtexpress")),
    ("debang", ("德邦", "debang")),
    ("cainiao", ("菜鸟", "驿站", "裹裹")),
]

PREFIX_CARRIER = [
    (re.compile(r"^SF\d{10,20}$", re.I), "sf"),
    (re.compile(r"^JD[A-Z0-9]{8,20}$", re.I), "jd"),
    (re.compile(r"^YT\d{10,20}$", re.I), "yto"),
    (re.compile(r"^ZTO\d{8,20}$", re.I), "zto"),
    (re.compile(r"^STO\d{8,20}$", re.I), "sto"),
    (re.compile(r"^YD\d{10,20}$", re.I), "yunda"),
    (re.compile(r"^JT\d{10,20}$", re.I), "jtexpress"),
    (re.compile(r"^DPK[A-Z0-9]{8,20}$", re.I), "debang"),
    (re.compile(r"^[A-Z]{2}\d{9}[A-Z]{2}$", re.I), "ems"),
]

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

TRACKING_CONTEXT = re.compile(
    r"(?:运单号|快递单号|物流单号|包裹号|快递编号|(?<!订)单号)\s*[:：]?\s*"
    r"([A-Za-z0-9][A-Za-z0-9-]{7,23})",
    re.I,
)
TRACKING_PREFIX = re.compile(
    r"\b(?:SF|JD|YT|ZTO|STO|YD|JT|DPK|EMS)\s?[A-Z0-9]{8,20}\b",
    re.I,
)
EMS_PATTERN = re.compile(r"\b[A-Z]{2}\d{9}[A-Z]{2}\b", re.I)
NUMERIC_TRACKING = re.compile(r"(?<!\d)(\d{12,20})(?!\d)")
ORDER_ID = re.compile(
    r"(?:订单号|订单编号|订单\s*ID)\s*[:：]?\s*([A-Za-z0-9][A-Za-z0-9-]{5,29})",
    re.I,
)
PICKUP_CODE = re.compile(
    r"(?:取件码|取货码|提货码|取餐码|取件凭证|开柜码|取件密码)\s*[:：]?\s*"
    r"([0-9A-Za-z]{1,4}(?:[-—][0-9A-Za-z]{1,6}){1,3}|[0-9A-Za-z]{3,10})",
    re.I,
)
LOCATION = re.compile(
    r"(?:已到|到达|存放至|存至|放至|放置于|投递至|在)\s*"
    r"([^，。；;！!\n]{2,50}?(?:驿站|快递柜|代收点|服务中心|超市|门卫|物业|柜))",
)
BRACKET_LOCATION = re.compile(r"【([^】]{2,30}(?:驿站|快递柜|代收点|服务中心|超市))】")

# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

STATUS_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("refunded", re.compile(r"退款成功|已退款|退款到账|原路退回")),
    ("returned", re.compile(r"退回|退签|拒收|超时未取|逾期未取|无人接收|退回商家")),
    ("delivered", re.compile(r"已签收|签收成功|本人签收|已送达|已投递|已完成|已取件")),
    ("ready_for_pickup", re.compile(r"待取件|已到驿站|已到达驿站|存放至|已放入|存入|已放至|取件码|待取")),
    ("out_for_delivery", re.compile(r"派送中|派件中|正在派送|派件员|外出派送|即将送达")),
    ("delivering", re.compile(r"配送中|骑手已取餐|正在配送|骑手正在|已取餐|配送员")),
    ("accepted", re.compile(r"已接单|商家已接单|已确认订单|商家已确认|制作中")),
    ("in_transit", re.compile(r"运输中|已发出|已发货|已揽收|已收件|已交寄|中转|发往|离开|到达分拨")),
]

PLATFORM_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("jd", ("京东", "京东商城")),
    ("taobao", ("淘宝", "天猫")),
    ("douyin", ("抖音", "抖店", "抖音商城")),
    ("meituan", ("美团", "美团外卖", "美团买菜", "小象超市")),
    ("pdd", ("拼多多", "多多买菜")),
    ("vip", ("唯品会",)),
    ("eleme", ("饿了么",)),
]

PLATFORM_PREFIX = re.compile(r"^\s*【([^】]{1,16})】")
BAD_TRACKING_CONTEXT = re.compile(
    r"订单|流水|交易|电话|手机|金额|元|取件码|验证码|校验码|日期|时间"
)
LOGISTICS_WORDS = re.compile(
    r"快递|包裹|运单|取件|派送|派件|签收|发货|配送|物流|驿站|快递柜|订单|外卖|骑手|退款"
)
OTP_WORDS = re.compile(r"验证码|校验码|动态码|登录|注册|身份验证|安全码")


@dataclass
class ParsedSMS:
    ignored: bool = False
    platform: str = ""
    status: str = "unknown"
    tracking_no: str = ""
    tracking_numbers: list[str] = field(default_factory=list)
    carrier: str = ""
    carrier_name: str = ""
    pickup_code: str = ""
    location: str = ""
    order_id: str = ""
    is_logistics: bool = False

    def as_dict(self) -> dict:
        return {
            "ignored": self.ignored,
            "platform": self.platform,
            "status": self.status,
            "tracking_no": self.tracking_no,
            "tracking_numbers": self.tracking_numbers,
            "carrier": self.carrier,
            "carrier_name": self.carrier_name,
            "pickup_code": self.pickup_code,
            "location": self.location,
            "order_id": self.order_id,
            "is_logistics": self.is_logistics,
        }


def _clean_tracking(value: str) -> str:
    return re.sub(r"[\s\-]", "", value).upper()


def _valid_tracking(value: str) -> bool:
    if not (8 <= len(value) <= 24):
        return False
    # A phone number is 11 digits and starts with 1; don't treat it as a parcel.
    if value.isdigit() and len(value) == 11 and value.startswith("1"):
        return False
    # Long all-digit IDs are often order numbers; only accept with prefix or context.
    if value.isdigit() and len(value) > 20:
        return False
    return True


def _detect_carrier(text: str, numbers: list[str]) -> str:
    for number in numbers:
        for pattern, carrier in PREFIX_CARRIER:
            if pattern.match(number):
                return carrier
    lowered = text.lower()
    for carrier, keywords in CARRIER_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            return carrier
    return ""


def _match_platform(value: str) -> str:
    lowered = (value or "").lower()
    # "京东物流/京东快递" are carriers, not proof of a JD order.
    if ("京东物流" in lowered or "京东快递" in lowered) and "京东商城" not in lowered:
        return ""
    for platform, keywords in PLATFORM_KEYWORDS:
        if any(keyword.lower() in lowered for keyword in keywords):
            return platform
    return ""


def _detect_platform(text: str, sender: str = "") -> str:
    """Only explicit brand signals count.

    A parcel can be delivered to a "菜鸟驿站" while belonging to a JD order, so
    location words and carrier names must never decide the platform. The sender
    and the leading 【brand】 tag are reliable; everything else stays unknown and
    is filled later by tracking-number/order-number linking.
    """
    from_sender = _match_platform(sender)
    if from_sender:
        return from_sender
    prefix = PLATFORM_PREFIX.match(text or "")
    if prefix:
        from_prefix = _match_platform(prefix.group(1))
        if from_prefix:
            return from_prefix
    return _match_platform((text or "")[:24])


def _detect_status(text: str) -> str:
    for status, pattern in STATUS_PATTERNS:
        if pattern.search(text):
            return status
    return "unknown"


def _extract_tracking_numbers(text: str) -> list[str]:
    candidates: dict[str, float] = {}

    def add(value: str, confidence: float) -> None:
        value = _clean_tracking(value)
        if _valid_tracking(value):
            candidates[value] = max(candidates.get(value, 0.0), confidence)

    # Explicit labels are the strongest signal.
    for match in TRACKING_CONTEXT.finditer(text):
        add(match.group(1), 1.0)

    # Prefixed carrier numbers are very reliable.
    for match in TRACKING_PREFIX.finditer(text):
        add(match.group(0), 1.0)

    # EMS style: EA123456789CN
    for match in EMS_PATTERN.finditer(text):
        add(match.group(0), 0.95)

    # Bare numbers are only accepted with a nearby parcel/carrier signal. Never
    # guess from the first long number in the message: order IDs and payment
    # references live in the same SMS and are indistinguishable without context.
    for match in NUMERIC_TRACKING.finditer(text):
        candidate = match.group(1)
        start = max(0, match.start() - 18)
        end = min(len(text), match.end() + 18)
        context = text[start:end]
        if BAD_TRACKING_CONTEXT.search(context):
            continue
        has_carrier = any(
            keyword.lower() in context.lower()
            for _, keywords in CARRIER_KEYWORDS
            for keyword in keywords
        )
        has_parcel_word = bool(re.search(r"快递|包裹|运单|物流|派送|签收", context))
        if has_carrier or has_parcel_word:
            add(candidate, 0.75)

    return [
        value
        for value, _ in sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    ]


def parse_sms(text: str, sender: str = "") -> ParsedSMS:
    text = (text or "").strip()
    if not text:
        return ParsedSMS(ignored=True)

    numbers = _extract_tracking_numbers(text)
    carrier = _detect_carrier(text, numbers)
    platform = _detect_platform(text, sender)
    status = _detect_status(text)
    is_logistics = bool(numbers or status != "unknown" or LOGISTICS_WORDS.search(text))

    # OTP / marketing messages should never enter the parcel pipeline.
    if OTP_WORDS.search(text) and not numbers and status == "unknown":
        return ParsedSMS(ignored=True, platform=platform, is_logistics=False)

    if not is_logistics:
        return ParsedSMS(ignored=True, platform=platform, is_logistics=False)

    pickup_match = PICKUP_CODE.search(text)
    pickup_code = pickup_match.group(1) if pickup_match else ""
    if pickup_code and any(word in pickup_code for word in ("验证", "登录")):
        pickup_code = ""

    location = ""
    loc_match = LOCATION.search(text) or BRACKET_LOCATION.search(text)
    if loc_match:
        location = loc_match.group(1).strip()

    order_match = ORDER_ID.search(text)
    order_id = order_match.group(1) if order_match else ""

    tracking_no = numbers[0] if numbers else ""
    return ParsedSMS(
        ignored=False,
        platform=platform,
        status=status,
        tracking_no=tracking_no,
        tracking_numbers=numbers,
        carrier=carrier,
        carrier_name=CARRIER_NAMES.get(carrier, ""),
        pickup_code=pickup_code,
        location=location,
        order_id=order_id,
        is_logistics=True,
    )
