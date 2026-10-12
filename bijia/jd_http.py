"""用手机版商品页读取标题、店铺、库存和价格。搜索或券后价被拦住时抛出明确错误。"""

from __future__ import annotations

import http.cookiejar
import json
import random
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

from bijia.jd_browser import JdBlocked, SearchCard
from bijia.models import Offer

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)


class JdLoginRequired(RuntimeError):
    pass


class JdHttp:
    def __init__(self, cookie_file: Path | None = None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        if cookie_file and cookie_file.exists():
            _load_playwright_cookies(cookie_file, self.jar)
        self.login_required = False
        self.search_blocked = False
        self.slow = False

    def open_offer(self, url: str, hint: SearchCard | None = None) -> Offer | None:
        sku = _sku_from_url(url)
        if not sku:
            return None
        html, final = self._get(f"https://item.m.jd.com/product/{sku}.html", referer="https://m.jd.com/")
        _raise_for_gate(html, final, allow_item=True)
        offer = parse_mobile_item(html, sku, _canonical_url(url, sku))
        if hint and not offer.shop:
            offer.shop = hint.shop
        if hint and not offer.sales:
            offer.sales = hint.sales
        return offer

    def search(self, keyword: str) -> list[SearchCard]:
        if self.login_required:
            raise JdLoginRequired("京东要求登录后才能搜索")
        if self.search_blocked:
            raise JdBlocked("搜索已被频控，本轮其余搜索先跳过；请改用 --pick links 或稍后再试")
        html, final = self._get(
            f"https://so.m.jd.com/ware/search.action?keyword={quote(keyword)}",
            referer="https://m.jd.com/",
        )
        try:
            _raise_for_gate(html, final, allow_item=False)
        except JdLoginRequired:
            self.login_required = True
            raise
        except JdBlocked as first:
            if "访问频繁" in str(first) or "无法搜索" in str(first):
                self.search_blocked = True
                raise
            time.sleep(45)
            html, final = self._get(
                f"https://so.m.jd.com/ware/search.action?keyword={quote(keyword)}",
                referer="https://m.jd.com/",
            )
            try:
                _raise_for_gate(html, final, allow_item=False)
            except JdLoginRequired:
                self.login_required = True
                raise
            except JdBlocked as second:
                self.search_blocked = True
                if "访问频繁" in str(second) or "无法搜索" in str(second):
                    raise
                raise JdBlocked(
                    "搜索连续被京东拦住，本轮其余搜索先跳过；有京东链接的行请改用 --pick links"
                ) from None
        cards: list[SearchCard] = []
        seen: set[str] = set()
        for sku, title in _search_hits(html):
            if sku in seen:
                continue
            seen.add(sku)
            cards.append(
                SearchCard(
                    sku=sku,
                    url=f"https://item.jd.com/{sku}.html",
                    title=_decode_js(title),
                    text=_decode_js(title),
                    shop="",
                    sales=0,
                )
            )
        return cards

    def _get(self, url: str, referer: str) -> tuple[str, str]:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": MOBILE_UA,
                "Accept": "text/html,application/xhtml+xml",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer,
            },
        )
        try:
            with self.opener.open(request, timeout=20) as response:
                final = response.geturl()
                html = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code in {302, 403, 429, 503}:
                raise JdBlocked(f"京东返回 {exc.code}") from exc
            raise
        except urllib.error.URLError as exc:
            raise JdBlocked(f"网络打不开京东：{exc.reason}") from exc
        time.sleep(random.uniform(4.0, 7.0) if self.slow else random.uniform(2.0, 3.5))
        return html, final


def parse_mobile_item(html: str, sku: str, url: str) -> Offer:
    title = _decode_js(_first(html, r'"skuName":"(.*?)"') or "")
    brand = _decode_js(_first(html, r'"brandName":"(.*?)"') or "")
    shop = _decode_js(_first(html, r'"shopName":"(.*?)"') or "")
    if brand and title and brand not in title:
        title = f"{brand} {title}".strip()
    stock_raw = _first(html, r'"StockState":(\d+)')
    in_stock = True
    if stock_raw is not None:
        in_stock = int(stock_raw) in {33, 36, 39, 40}
    if any(word in html for word in ("该地区暂时无货", "该地区无货", "该商品已下柜", "当前地址无货")):
        in_stock = False
    price_text = _first(html, r'"priceFloor"\s*:\s*\{[^{}]{0,160}"price":"([^"]+)"')
    jd_text = _first(html, r'"jdPrice":"([^"]+)"')
    coupon = _number(price_text)
    jd_price = _number(jd_text)
    source = ""
    if coupon is None and jd_price is None and price_text and "?" in price_text:
        source = "需要登录"
    elif coupon is not None:
        nearby = html
        pos = html.find(f'"price":"{price_text}"')
        if pos >= 0:
            nearby = html[max(0, pos - 120) : pos + 160]
        source = "券后价" if "到手" in nearby else "页面售价"
    region = _region(html)
    approvals = " ".join(re.findall(r"国药准字\s*[A-Za-z]\s*[A-Za-z0-9]{6,}", html)[:3])
    page_text = " ".join(part for part in (title, shop, brand, region, approvals) if part)
    return Offer(
        sku=sku,
        url=url,
        title=title,
        shop=shop,
        coupon_price=coupon if source == "券后价" else None,
        jd_price=jd_price if jd_price is not None else (coupon if source != "券后价" else None),
        in_stock=in_stock,
        region=region,
        page_text=page_text,
        price_source=source,
    )


def _search_hits(html: str) -> list[tuple[str, str]]:
    hits = re.findall(r'"wareId":"(\d+)".{0,300}?"wname":"(.*?)"', html)
    if hits:
        return [(sku, title) for sku, title in hits]
    skus = re.findall(r"item\.m\.jd\.com/product/(\d+)\.html", html)
    return [(sku, "") for sku in skus]


def _raise_for_gate(html: str, final: str, allow_item: bool) -> None:
    blob = f"{final}\n{html[:800]}"
    if (
        "访问频繁" in blob
        or "无法搜索" in blob
        or "搜索太频繁" in blob
        or ("搜索" in blob and "稍后再试" in blob)
    ):
        raise JdBlocked(
            "京东搜索频控：访问频繁导致无法搜索，请稍后再试。"
            "本轮其余搜索先跳过；有京东链接的行请改用 --pick links"
        )
    if "risk_handler" in blob or "验证一下" in blob or "pc-frequent-pro" in final:
        raise JdBlocked("京东弹出了验证或频控，等待后重试")
    if "人数过多" in html:
        raise JdBlocked("京东提示当前人数过多，等待后重试")
    if "plogin.m.jd.com" in final or ("京东登录注册" in html and "skuName" not in html):
        raise JdLoginRequired("京东要求登录后才能继续")
    if allow_item and "skuName" not in html and "暂时无法展示" in html:
        raise JdBlocked("商品页暂时打不开")


def _region(html: str) -> str:
    raw = _first(html, r'"wqAddrDefault":"([^"]+)"')
    if not raw:
        return ""
    parts = raw.split("|")
    if len(parts) < 3:
        return ""
    return parts[2].replace("_", "")[:40]


def _number(text: str | None) -> float | None:
    if not text or not re.fullmatch(r"\d+(?:\.\d+)?", text):
        return None
    value = float(text)
    if value <= 0:
        return None
    return value


def _first(html: str, pattern: str) -> str | None:
    match = re.search(pattern, html)
    return match.group(1) if match else None


def _decode_js(text: str) -> str:
    if "\\" not in text:
        return text
    try:
        return json.loads(f'"{text}"')
    except json.JSONDecodeError:
        return text


def _sku_from_url(url: str) -> str:
    match = re.search(r"(?:item\.jd\.com|item\.jingdonghealth\.cn|item\.m\.jd\.com/product)/(\d+)", url or "")
    return match.group(1) if match else ""


def _canonical_url(url: str, sku: str) -> str:
    if "jingdonghealth" in (url or ""):
        return f"https://item.jingdonghealth.cn/{sku}.html"
    return f"https://item.jd.com/{sku}.html"


def _load_playwright_cookies(path: Path, jar: http.cookiejar.CookieJar) -> None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for item in payload.get("cookies") or []:
        domain = item.get("domain") or ""
        if "jd.com" not in domain:
            continue
        cookie = http.cookiejar.Cookie(
            0,
            item.get("name") or "",
            item.get("value") or "",
            None,
            False,
            domain,
            True,
            domain.startswith("."),
            item.get("path") or "/",
            True,
            item.get("secure") or False,
            item.get("expires") or None,
            False,
            None,
            None,
            {},
        )
        jar.set_cookie(cookie)
