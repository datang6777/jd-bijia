"""用当前已经登录的 Google Chrome 打开京东。只占用一个新标签页。"""

from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from urllib.parse import quote

from bijia.jd_browser import JdBlocked, SearchCard
from bijia.jd_http import JdLoginRequired
from bijia.models import Offer
from bijia.parse_page import PageFacts, apply_visible_text, parse_sales

ITEM_JS = r"""
(() => {
  const text = (document.body && document.body.innerText) || "";
  const title = (document.title || "").replace(/【行情[\s\S]*$/, "").replace(/-京东$/, "").trim();
  const skuEl = document.querySelector(".sku-name, [class*='sku-name']");
  const skuTitle = skuEl ? skuEl.innerText.replace(/\s+/g, " ").trim() : "";
  return JSON.stringify({
    href: location.href,
    title: skuTitle || title,
    text: text.replace(/\s+/g, " ").trim().slice(0, 4500)
  });
})()
"""

SEARCH_JS = r"""
(() => {
  const text = ((document.body && document.body.innerText) || "").replace(/\s+/g, " ").trim();
  const seen = new Set();
  const items = [];
  for (const card of document.querySelectorAll("[data-sku]")) {
    const sku = card.getAttribute("data-sku") || "";
    if (!/^\d{6,}$/.test(sku) || seen.has(sku)) continue;
    const cardText = (card.innerText || "").replace(/\s+/g, " ").trim();
    if (cardText.length < 8) continue;
    seen.add(sku);
    items.push({
      sku: sku,
      url: "https://item.jd.com/" + sku + ".html",
      title: cardText.slice(0, 180),
      text: cardText.slice(0, 500)
    });
    if (items.length >= 20) break;
  }
  return JSON.stringify({
    href: location.href,
    title: document.title || "",
    text: text.slice(0, 800),
    items: items
  });
})()
"""


class JdChrome:
    def __init__(self) -> None:
        self.tab_id: int | None = None
        self.window_index = 1
        self.slow = False
        self.search_blocked = False
        self.login_required = False

    def open_offer(self, url: str, hint: SearchCard | None = None) -> Offer | None:
        sku = _sku_from_url(url)
        if not sku:
            return None
        self._goto(_canonical_url(url, sku))
        payload = self._wait_item()
        if _is_risk(payload.get("href", ""), payload.get("text", "")):
            raise JdBlocked("京东弹出了验证或频控，等待后重试")
        offer = offer_from_item(payload, sku)
        if hint and not offer.shop:
            offer.shop = hint.shop
        if hint and not offer.sales:
            offer.sales = hint.sales
        if offer.price_source == "需要登录":
            raise JdLoginRequired("这个 Chrome 窗口里京东没有显示券后价")
        return offer

    def search(self, keyword: str) -> list[SearchCard]:
        if self.login_required:
            raise JdLoginRequired("京东要求登录后才能搜索")
        if self.search_blocked:
            raise JdBlocked("搜索连续被京东拦住，本轮其余搜索先跳过")
        self._goto(f"https://search.jd.com/Search?keyword={quote(keyword)}&enc=utf-8")
        payload = self._wait_search()
        if _is_risk(payload.get("href", ""), payload.get("text", "")):
            self.search_blocked = True
            raise JdBlocked("搜索连续被京东拦住，本轮其余搜索先跳过")
        if "plogin" in payload.get("href", "") or "请登录" in (payload.get("text") or "")[:200]:
            self.login_required = True
            raise JdLoginRequired("搜索页要求重新登录京东")
        if not payload.get("items"):
            raise JdBlocked("搜索页没有读到商品列表")
        cards: list[SearchCard] = []
        for item in payload.get("items") or []:
            text = item.get("text") or ""
            cards.append(
                SearchCard(
                    sku=str(item.get("sku") or ""),
                    url=item.get("url") or "",
                    title=(item.get("title") or text).strip(),
                    text=text,
                    shop=_shop_from_text(text[:400]),
                    sales=parse_sales(text),
                )
            )
        return cards

    def _goto(self, url: str) -> None:
        self._ensure_tab()
        self._script(
            f"""
            tell application "Google Chrome"
              tell window {self.window_index}
                repeat with i from 1 to (count of tabs)
                  if (id of tab i as text) is "{self.tab_id}" then
                    set active tab index to i
                    set URL of tab i to {_apple_string(url)}
                    exit repeat
                  end if
                end repeat
              end tell
            end tell
            """
        )
        time.sleep(2.0 if self.slow else 1.2)

    def _ensure_tab(self) -> None:
        if self.tab_id is not None:
            return
        raw = self._script(
            """
            tell application "Google Chrome"
              if (count of windows) is 0 then
                make new window
              end if
              set windowIndex to 1
              repeat with w from 1 to (count of windows)
                repeat with t in tabs of window w
                  if URL of t contains "jd.com" then
                    set windowIndex to w
                    exit repeat
                  end if
                end repeat
              end repeat
              tell window windowIndex
                set newTab to make new tab with properties {URL:"about:blank"}
                return (windowIndex as text) & " " & (id of newTab as text)
              end tell
            end tell
            """
        )
        window_index, tab_id = raw.split()
        self.window_index = int(window_index)
        self.tab_id = int(tab_id)

    def _wait_item(self) -> dict:
        last: dict = {"href": "", "title": "", "text": ""}
        deadline = time.time() + (24 if not self.slow else 30)
        while time.time() < deadline:
            last = self._read_page(ITEM_JS)
            text = last.get("text") or ""
            if last.get("title") and any(word in text for word in ("到手价", "加入购物车", "立即购买", "无货")):
                return last
            if _is_risk(last.get("href", ""), text):
                return last
            time.sleep(1.3)
        return last

    def _wait_search(self) -> dict:
        last: dict = {"href": "", "title": "", "text": "", "items": []}
        deadline = time.time() + 18
        while time.time() < deadline:
            last = self._read_page(SEARCH_JS)
            if last.get("items") or _is_risk(last.get("href", ""), last.get("text", "")):
                return last
            time.sleep(1.3)
        return last

    def _read_page(self, javascript: str) -> dict:
        path = Path("/tmp/jd_bijia_page.js")
        path.write_text(javascript, encoding="utf-8")
        raw = self._script(
            f"""
            set jsSource to read POSIX file "/tmp/jd_bijia_page.js" as «class utf8»
            tell application "Google Chrome"
              tell window {self.window_index}
                repeat with i from 1 to (count of tabs)
                  if (id of tab i as text) is "{self.tab_id}" then
                    return execute tab i javascript jsSource
                  end if
                end repeat
              end tell
            end tell
            """
        )
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {"text": raw[:500]}
        return data if isinstance(data, dict) else {}

    def _script(self, source: str) -> str:
        path = Path("/tmp/jd_bijia.applescript")
        path.write_text(source, encoding="utf-8")
        result = subprocess.run(["osascript", str(path)], capture_output=True, text=True)
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "AppleScript 失败").strip()
            if "JavaScript" in message and "关闭" in message:
                raise JdBlocked("Chrome 里还没允许 Apple 事件中的 JavaScript")
            raise RuntimeError(message.splitlines()[-1][:300])
        return result.stdout.strip()


def offer_from_item(payload: dict, sku: str) -> Offer:
    title = re.sub(r"\s+", " ", payload.get("title") or "").strip()
    text = payload.get("text") or ""
    href = payload.get("href") or ""
    facts = PageFacts()
    buttons: list[str] = []
    if "加入购物车" in text:
        buttons.append("加入购物车")
    if "立即购买" in text:
        buttons.append("立即购买")
    if "到货通知" in text[:1500]:
        buttons.append("到货通知")
    apply_visible_text(f"{title} {text}", buttons, facts)
    shop = _shop_from_text(text[:1200]) or _shop_from_text(title)
    in_stock = facts.in_stock
    if any(word in text[:1200] for word in ("该地区暂时无货", "该地区无货", "该商品已下柜", "当前地址无货")):
        in_stock = False
    if facts.coupon_price is not None:
        source = "券后价"
    elif "登录查看价格" in text or "登录后可查看" in text:
        source = "需要登录"
    elif facts.jd_price is not None:
        source = "页面没显示券后价，先用京东价"
    else:
        source = ""
    return Offer(
        sku=sku,
        url=_canonical_url(href or f"https://item.jd.com/{sku}.html", sku),
        title=title,
        shop=shop,
        coupon_price=facts.coupon_price,
        jd_price=facts.jd_price,
        sales=parse_sales(text),
        in_stock=in_stock,
        region=_region_from_text(text),
        page_text=f"{title} {text}"[:4000],
        price_source=source,
    )


def _shop_from_text(text: str) -> str:
    for name in ("京东健康自营旗舰店", "京东健康大药房", "京东大药房", "京东买药"):
        if name in text:
            return name
    match = re.search(r"([\u4e00-\u9fffA-Za-z0-9]{2,24}(?:自营旗舰店|旗舰店|专营店))", text or "")
    if not match:
        return "京东自营" if "自营" in (text or "") else ""
    shop = match.group(1)
    if "自营" in text and "自营" not in shop:
        return shop + " 自营"
    return shop


def _region_from_text(text: str) -> str:
    match = re.search(r"(?:送至|配送至)\s*([\u4e00-\u9fff]{2,12})", text or "")
    return match.group(1) if match else ""


def _is_risk(url: str, text: str) -> bool:
    blob = f"{url}\n{(text or '')[:400]}"
    return any(word in blob for word in ("risk_handler", "暂时无法展示", "验证一下", "人数过多", "安全验证"))


def _sku_from_url(url: str) -> str:
    match = re.search(r"(?:item\.jd\.com|item\.jingdonghealth\.cn|item\.m\.jd\.com/product)/(\d+)", url or "")
    return match.group(1) if match else ""


def _canonical_url(url: str, sku: str) -> str:
    if "jingdonghealth" in (url or ""):
        return f"https://item.jingdonghealth.cn/{sku}.html"
    return f"https://item.jd.com/{sku}.html"


def _apple_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
