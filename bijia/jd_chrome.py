"""用本机已登录的 Google Chrome 打开京东。

macOS 默认走 AppleScript（只新开一个标签页）。
Windows（或显式 --chrome-debug-port）走 Chrome DevTools Protocol（CDP），
可附着已开启远程调试的 Chrome，或自动用用户配置目录拉起带调试端口的 Chrome。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from bijia.jd_browser import JdBlocked, SearchCard
from bijia.jd_http import JdLoginRequired
from bijia.models import Offer
from bijia.parse_page import PageFacts, apply_visible_text, parse_sales

DEFAULT_CDP_PORT = 9222

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


def resolve_chrome_backend(debug_port: int | None = None) -> tuple[str, int | None]:
    """返回 (backend, port)。backend 为 applescript 或 cdp。"""
    port = debug_port
    if port is None:
        env = (os.environ.get("BIJIA_CHROME_DEBUG_PORT") or "").strip()
        if env:
            port = int(env)
    if port is not None:
        return "cdp", int(port)
    if sys.platform == "win32":
        return "cdp", DEFAULT_CDP_PORT
    return "applescript", None


def cdp_endpoint(port: int) -> str:
    return f"http://127.0.0.1:{int(port)}"


def cdp_is_ready(port: int, timeout: float = 1.5) -> bool:
    url = f"{cdp_endpoint(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            response.read(256)
        return True
    except (urllib.error.URLError, TimeoutError, OSError):
        return False


def chrome_executable_candidates() -> list[Path]:
    if sys.platform == "win32":
        bases = [
            Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")),
            Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")),
            Path(os.environ.get("LOCALAPPDATA", "")),
        ]
        return [base / "Google" / "Chrome" / "Application" / "chrome.exe" for base in bases if str(base)]
    if sys.platform == "darwin":
        return [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")]
    return [
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/google-chrome-stable"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    ]


def find_chrome_executable() -> Path | None:
    env = (os.environ.get("BIJIA_CHROME_PATH") or os.environ.get("CHROME_PATH") or "").strip()
    if env:
        path = Path(env)
        if path.is_file():
            return path
    for path in chrome_executable_candidates():
        if path.is_file():
            return path
    return None


def default_chrome_user_data_dir() -> Path | None:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "Google" / "Chrome" / "User Data"
        return None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
    # Linux
    return Path.home() / ".config" / "google-chrome"


def launch_chrome_with_debugging(
    port: int,
    user_data_dir: Path | None = None,
    start_url: str = "https://www.jd.com/",
) -> subprocess.Popen:
    chrome = find_chrome_executable()
    if chrome is None:
        raise RuntimeError(
            "找不到 Google Chrome。请安装 Chrome，或设置环境变量 BIJIA_CHROME_PATH 指向 chrome.exe。"
        )
    profile = user_data_dir or default_chrome_user_data_dir()
    args = [
        str(chrome),
        f"--remote-debugging-port={int(port)}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        start_url,
    ]
    if profile is not None:
        args.insert(1, f"--user-data-dir={profile}")
    # Windows 下用新控制台，避免挂到当前终端
    kwargs: dict = {}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)


def ensure_cdp_chrome(
    port: int,
    auto_launch: bool = True,
    user_data_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> None:
    emit = log or (lambda message: print(message, flush=True))
    if cdp_is_ready(port):
        emit(f"已连接到 Chrome 远程调试端口 {port}")
        return
    if not auto_launch:
        raise RuntimeError(_cdp_howto(port))
    emit(
        f"端口 {port} 上还没有可调试的 Chrome。将尝试用你的 Chrome 用户目录启动 "
        f"（需先完全退出已打开的 Chrome，否则会失败）。"
    )
    try:
        launch_chrome_with_debugging(port, user_data_dir=user_data_dir)
    except Exception as exc:
        raise RuntimeError(f"自动启动 Chrome 失败：{exc}\n{_cdp_howto(port)}") from exc
    deadline = time.time() + 25
    while time.time() < deadline:
        if cdp_is_ready(port):
            emit(f"Chrome 远程调试已就绪（端口 {port}）")
            return
        time.sleep(0.4)
    raise RuntimeError(
        f"已尝试启动 Chrome，但 {port} 端口仍无响应。\n"
        f"常见原因：Chrome 仍在后台运行并占用了用户目录。请任务管理器结束所有 chrome.exe 后重试。\n"
        f"{_cdp_howto(port)}"
    )


def _cdp_howto(port: int) -> str:
    if sys.platform == "win32":
        chrome = r'"C:\Program Files\Google\Chrome\Application\chrome.exe"'
        profile = r'%LOCALAPPDATA%\Google\Chrome\User Data'
        return (
            "Windows 用法：先完全退出 Chrome，再任选其一——\n"
            f"  1) 手动启动：{chrome} --remote-debugging-port={port} --user-data-dir=\"{profile}\"\n"
            f"  2) 本工具自动拉起：python run.py --via chrome --chrome-debug-port {port}\n"
            "  3) 备选（不依赖本机 Chrome 标签页）：python run.py --login 后改用 --via http"
        )
    return (
        f"请先启动带远程调试的 Chrome：google-chrome --remote-debugging-port={port}\n"
        f"或：python run.py --via chrome --chrome-debug-port {port}\n"
        "备选：python run.py --login 后使用 --via http"
    )


def create_jd_chrome(
    debug_port: int | None = None,
    auto_launch: bool = True,
    user_data_dir: Path | None = None,
    log: Callable[[str], None] | None = None,
) -> "JdChromeAppleScript | JdChromeCDP":
    backend, port = resolve_chrome_backend(debug_port)
    if backend == "cdp":
        assert port is not None
        return JdChromeCDP(port=port, auto_launch=auto_launch, user_data_dir=user_data_dir, log=log)
    return JdChromeAppleScript()


class JdChrome:
    """构造时按平台选择后端：Windows→CDP，macOS→AppleScript（可被 debug_port 覆盖）。"""

    def __new__(
        cls,
        debug_port: int | None = None,
        auto_launch: bool = True,
        user_data_dir: Path | None = None,
        log: Callable[[str], None] | None = None,
    ):
        return create_jd_chrome(
            debug_port=debug_port,
            auto_launch=auto_launch,
            user_data_dir=user_data_dir,
            log=log,
        )


class JdChromeAppleScript:
    """macOS：用 osascript 驱动已打开的 Google Chrome。"""

    def __init__(self) -> None:
        self.tab_id: int | None = None
        self.window_index = 1
        self.slow = False
        self.search_blocked = False
        self.login_required = False
        self.backend = "applescript"

    def close(self) -> None:
        return

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
        return _cards_from_search(payload)

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
        return _parse_js_payload(raw)

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


class JdChromeCDP:
    """通过 CDP 附着/驱动 Chrome（Windows 主路径，也可用在 Mac/Linux）。"""

    def __init__(
        self,
        port: int = DEFAULT_CDP_PORT,
        auto_launch: bool = True,
        user_data_dir: Path | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self.port = int(port)
        self.slow = False
        self.search_blocked = False
        self.login_required = False
        self.backend = "cdp"
        self._log = log or (lambda message: print(message, flush=True))
        self._playwright = None
        self._browser = None
        self._page = None
        ensure_cdp_chrome(self.port, auto_launch=auto_launch, user_data_dir=user_data_dir, log=self._log)
        self._connect()

    def _connect(self) -> None:
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        endpoint = cdp_endpoint(self.port)
        self._browser = self._playwright.chromium.connect_over_cdp(endpoint)
        if self._browser.contexts:
            context = self._browser.contexts[0]
        else:
            context = self._browser.new_context()
        self._page = context.new_page()
        self._log(f"CDP 已附着 Chrome（{endpoint}），将使用新标签页访问京东")

    def close(self) -> None:
        # 只断开 CDP，不关闭用户的 Chrome 进程
        try:
            if self._page is not None:
                self._page.close()
        except Exception:
            pass
        try:
            if self._browser is not None:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright is not None:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._browser = None
        self._playwright = None

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
            raise JdLoginRequired("这个 Chrome 窗口里京东没有显示券后价（请确认已登录）")
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
        return _cards_from_search(payload)

    def _goto(self, url: str) -> None:
        assert self._page is not None
        self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        time.sleep(2.0 if self.slow else 1.0)

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
        assert self._page is not None
        try:
            raw = self._page.evaluate(javascript)
        except Exception:
            return {}
        if isinstance(raw, dict):
            return raw
        return _parse_js_payload(raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False))


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


def _cards_from_search(payload: dict) -> list[SearchCard]:
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


def _parse_js_payload(raw: str | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {"text": str(raw)[:500]}
    return data if isinstance(data, dict) else {}


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
