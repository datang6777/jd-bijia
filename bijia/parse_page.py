"""从京东页面文本和接口 JSON 里取券后价、店铺、库存。"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class PageFacts:
    title: str = ""
    shop: str = ""
    coupon_price: float | None = None
    jd_price: float | None = None
    in_stock: bool = True
    region: str = ""
    sales: int = 0
    price_source: str = ""


_COUPON_PATTERNS = (
    # 京东页面是「¥148到手价」，数字在标签前面。
    r"[¥￥](?P<n>\d+(?:\.\d{1,2})?)(?:到手价|券后价|券后)",
    r"(?:到手价|券后价|券后)[¥￥:：]?(?P<n>\d+(?:\.\d{1,2})?)",
)
_JD_PATTERNS = (
    r"(?:京东价|日常价|原价)[¥￥:：]?(?P<n>\d+(?:\.\d{1,2})?)",
)
_OUT_OF_STOCK = (
    "该地区暂时无货",
    "该地区无货",
    "当前地址无货",
    "当前地址暂时无货",
    "该商品已下柜",
    "该地区不支持销售",
    "不支持配送",
    "暂时缺货",
    "无货",
    "到货通知",
    "已抢光",
)
_IN_STOCK_BUTTONS = ("加入购物车", "立即购买", "领券购买", "去购买")


def parse_sales(text: str) -> int:
    if not text:
        return 0
    match = re.search(r"已售\s*([0-9]+(?:\.[0-9]+)?)\s*(万)?", text)
    if not match:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(万)?\+?\s*(?:人付款|条评价|人已买|人买过)", text)
    if not match:
        return 0
    number = float(match.group(1))
    if match.group(2):
        number *= 10000
    return int(number)


def absorb_json_text(text: str, facts: PageFacts) -> None:
    if not text or len(text) > 2_000_000:
        return
    compact = text.replace(" ", "")
    coupon = _json_number(
        compact,
        (
            r'"finalPrice":\{[^}]{0,240}"price":"?(\d+(?:\.\d{1,2})?)"?',
            r'"purchasePrice":"?(\d+(?:\.\d{1,2})?)"?',
            r'"guidePrice":"?(\d+(?:\.\d{1,2})?)"?',
            r'"couponPrice":"?(\d+(?:\.\d{1,2})?)"?',
        ),
    )
    if coupon and facts.coupon_price is None:
        facts.coupon_price = coupon
        facts.price_source = "接口券后价"
    jd_price = _json_number(
        compact,
        (
            r'"jdPrice":"?(\d+(?:\.\d{1,2})?)"?',
            r'"opPrice":"?(\d+(?:\.\d{1,2})?)"?',
            r'"originalPrice":"?(\d+(?:\.\d{1,2})?)"?',
        ),
    )
    if jd_price and (facts.jd_price is None or facts.jd_price < jd_price):
        facts.jd_price = jd_price
    shop = re.search(r'"(?:shopName|venderName)":"([^"\\]{2,40})"', text)
    if shop and not facts.shop:
        facts.shop = shop.group(1)
    stock = re.search(r'"stockState"\s*:\s*(\d+)', compact)
    if stock:
        state = int(stock.group(1))
        if state in {34, 0}:
            facts.in_stock = False
        elif state in {33, 36, 39, 40}:
            facts.in_stock = True
    if re.search(r'"areaLimit"\s*:\s*true', compact) or re.search(r'"isPurchase"\s*:\s*false', compact):
        facts.in_stock = False


def apply_visible_text(text: str, buttons: list[str], facts: PageFacts) -> None:
    compact = re.sub(r"\s+", "", text or "")
    if facts.coupon_price is None:
        for pattern in _COUPON_PATTERNS:
            match = re.search(pattern, compact)
            if match:
                facts.coupon_price = float(match.group("n"))
                facts.price_source = facts.price_source or "页面券后价"
                break
    if facts.jd_price is None:
        for pattern in _JD_PATTERNS:
            match = re.search(pattern, compact)
            if match:
                facts.jd_price = float(match.group("n"))
                break
    if facts.coupon_price is None and facts.jd_price is None:
        yen = re.search(r"[¥￥](\d+(?:\.\d{1,2})?)", compact)
        if yen:
            facts.jd_price = float(yen.group(1))
            facts.price_source = facts.price_source or "页面标价"
    button_text = " ".join(buttons or [])
    head = compact[:800]
    if any(word in button_text for word in ("到货通知", "看相似", "无货")) and not any(
        word in button_text for word in _IN_STOCK_BUTTONS
    ):
        facts.in_stock = False
    if any(word in head for word in _OUT_OF_STOCK[:8]):
        facts.in_stock = False
    if "无货" in head and not any(word in button_text for word in _IN_STOCK_BUTTONS):
        facts.in_stock = False


def is_risk_page(url: str, title: str, text: str) -> bool:
    blob = f"{url}\n{title}\n{(text or '')[:600]}"
    markers = ("risk_handler", "cfe.m.jd.com", "验证一下", "安全验证", "滑动验证", "京东验证")
    return any(marker in blob for marker in markers)


def _json_number(text: str, patterns: tuple[str, ...]) -> float | None:
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            try:
                return float(match.group(1))
            except ValueError:
                continue
    return None


def loads_maybe(text: str):
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
