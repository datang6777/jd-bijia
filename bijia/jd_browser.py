"""用本机 Chrome 打开京东。遇到验证页就抛出，由上层等待后重试。"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

from bijia.models import Offer
from bijia.parse_page import PageFacts, absorb_json_text, apply_visible_text, is_risk_page, parse_sales

CARD_JS = """
() => {
  const anchors = [...document.querySelectorAll('a[href*="item.jd.com"], a[href*="item.jingdonghealth.cn"]')];
  const seen = new Set();
  const items = [];
  for (const anchor of anchors) {
    const href = anchor.href || "";
    const matched = href.match(/(\\d+)\\.html/);
    if (!matched) continue;
    const sku = matched[1];
    if (seen.has(sku)) continue;
    let node = anchor;
    let text = "";
    for (let i = 0; i < 7 && node; i += 1) {
      const current = (node.innerText || "").trim();
      if (current.length > 30 && current.length < 900) {
        text = current;
        break;
      }
      node = node.parentElement;
    }
    if (!text) text = (anchor.innerText || "").trim();
    if (!text || text.length < 4) continue;
    seen.add(sku);
    const host = href.includes("jingdonghealth") ? "https://item.jingdonghealth.cn" : "https://item.jd.com";
    items.push({
      sku,
      url: host + "/" + sku + ".html",
      title: (anchor.innerText || "").trim().slice(0, 180),
      text: text.slice(0, 700),
    });
    if (items.length >= 40) break;
  }
  return {
    url: location.href,
    title: document.title || "",
    text: (document.body && document.body.innerText || "").slice(0, 1500),
    items,
  };
}
"""

PAGE_JS = """
() => {
  const titleNode = document.querySelector(".sku-name, .itemInfo-wrap .sku-name, [class*='sku-name'], .product-intro .name");
  const shopNode = document.querySelector(
    "#popbox .name a, .shopName, .J-hove-wrap .name a, [class*='shopName'], .name-goodshop a, a[href*='mall.jd.com']"
  );
  const areaNode = document.querySelector(".ui-area-text, [class*='area-text'], .address-text, .stock-address");
  const buttons = [...document.querySelectorAll("button, a.btn, .btn, [class*='btn']")]
    .map((node) => (node.innerText || "").trim())
    .filter((text) => text && text.length <= 16)
    .slice(0, 40);
  const specNodes = [...document.querySelectorAll(".dd .item.selected, .sku-spec, [class*='specification'], .p-choose .dd")]
    .map((node) => (node.innerText || "").trim())
    .filter(Boolean)
    .slice(0, 8);
  return {
    url: location.href,
    title: document.title || "",
    skuTitle: titleNode ? titleNode.innerText.trim() : "",
    shop: shopNode ? shopNode.innerText.trim() : "",
    region: areaNode ? areaNode.innerText.trim() : "",
    buttons,
    spec: specNodes.join(" "),
    text: (document.body && document.body.innerText || "").slice(0, 6000),
  };
}
"""


class JdBlocked(RuntimeError):
    pass


@dataclass
class SearchCard:
    sku: str
    url: str
    title: str
    text: str
    shop: str
    sales: int
    price: float | None = None
    self_operated: bool = False


class JdBrowser:
    def __init__(self, profile_dir: Path, headless: bool = True):
        self.profile_dir = profile_dir
        self.headless = headless
        self._playwright = None
        self._context = None
        self._page = None

    def __enter__(self) -> "JdBrowser":
        from playwright.sync_api import sync_playwright

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = sync_playwright().start()
        launch_args = ["--disable-blink-features=AutomationControlled"]
        try:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=self.headless,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1400, "height": 900},
                args=launch_args,
            )
        except Exception:
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=self.headless,
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1400, "height": 900},
                args=launch_args,
            )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()
        self._page.set_default_timeout(25000)
        self._facts: PageFacts | None = None

        def on_response(response) -> None:
            facts = self._facts
            if facts is None:
                return
            url = response.url
            if not any(token in url for token in ("api.m.jd.com", "item-soa.jd.com", "p.3.cn", "cd.jd.com")):
                return
            try:
                text = response.text()
            except Exception:
                return
            absorb_json_text(text, facts)

        self._page.on("response", on_response)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if self._context:
                self._context.close()
        finally:
            if self._playwright:
                self._playwright.stop()

    def warmup(self) -> None:
        self._goto("https://www.jd.com/")
        time.sleep(1.5)

    def search(self, keyword: str) -> list[SearchCard]:
        url = f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8"
        self._goto(url)
        try:
            self._page.wait_for_selector("a[href*='item.jd.com'], a[href*='jingdonghealth']", timeout=12000)
        except Exception:
            pass
        payload = self._page.evaluate(CARD_JS)
        self._raise_if_blocked(payload.get("url", ""), payload.get("title", ""), payload.get("text", ""))
        cards: list[SearchCard] = []
        for item in payload.get("items") or []:
            text = item.get("text") or ""
            cards.append(
                SearchCard(
                    sku=str(item.get("sku") or ""),
                    url=item.get("url") or "",
                    title=(item.get("title") or text).strip(),
                    text=text,
                    shop=_shop_from_card(text),
                    sales=parse_sales(text),
                )
            )
        return cards

    def open_offer(self, url: str, hint: SearchCard | None = None) -> Offer | None:
        sku = _sku_from_url(url)
        if not sku:
            return None
        facts = PageFacts()
        if hint:
            facts.shop = hint.shop
            facts.sales = hint.sales
        self._facts = facts
        self._goto(_canonical_url(url, sku))
        self._page.wait_for_timeout(1600)
        try:
            payload = self._page.evaluate(PAGE_JS)
        except Exception as exc:
            raise JdBlocked(f"商品页脚本没有执行：{exc}") from exc
        self._raise_if_blocked(payload.get("url", ""), payload.get("title", ""), payload.get("text", ""))
        title = (payload.get("skuTitle") or payload.get("title") or "").strip()
        title = re.sub(r"\s+", " ", title)
        title = re.sub(r"-京东.*$", "", title).strip()
        if payload.get("shop"):
            facts.shop = payload["shop"].strip()
        if payload.get("region"):
            facts.region = re.sub(r"\s+", "", payload["region"])[:40]
        apply_visible_text(payload.get("text") or "", payload.get("buttons") or [], facts)
        page_text = " ".join(
            part for part in (title, payload.get("spec") or "", payload.get("text") or "") if part
        )
        if not facts.shop:
            facts.shop = _shop_from_card(page_text)
        sales = facts.sales or (hint.sales if hint else 0) or parse_sales(page_text)
        return Offer(
            sku=sku,
            url=_canonical_url(url, sku),
            title=title or (hint.title if hint else ""),
            shop=facts.shop,
            coupon_price=facts.coupon_price,
            jd_price=facts.jd_price,
            sales=sales,
            in_stock=facts.in_stock,
            region=facts.region,
            page_text=page_text[:5000],
            price_source=facts.price_source,
        )

    def _goto(self, url: str) -> None:
        assert self._page is not None
        try:
            self._page.goto(url, wait_until="domcontentloaded")
        except Exception as exc:
            message = str(exc)
            if "ERR_" in message or "Timeout" in message or "timeout" in message:
                raise JdBlocked(message) from exc
            raise
        time.sleep(random.uniform(0.6, 1.4))

    def _raise_if_blocked(self, url: str, title: str, text: str) -> None:
        if is_risk_page(url, title, text):
            raise JdBlocked("京东弹出了验证页，等待后重试")


def _sku_from_url(url: str) -> str:
    match = re.search(r"(?:item\.jd\.com|item\.jingdonghealth\.cn)/(\d+)\.html", url or "")
    return match.group(1) if match else ""


def _canonical_url(url: str, sku: str) -> str:
    if "jingdonghealth" in (url or ""):
        return f"https://item.jingdonghealth.cn/{sku}.html"
    return f"https://item.jd.com/{sku}.html"


def _shop_from_card(text: str) -> str:
    for name in ("京东健康自营旗舰店", "京东健康大药房", "京东大药房", "京东买药"):
        if name in text:
            return name
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,30}(?:旗舰店|专营店|官方店|大药房))", text or "")
    if match:
        shop = match.group(1)
        if "自营" in text and "自营" not in shop:
            return shop + " 自营"
        return shop
    if "自营" in (text or ""):
        return "京东自营"
    return ""
