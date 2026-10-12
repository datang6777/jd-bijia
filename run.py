#!/usr/bin/env python3
"""京东比价入口。

默认输入：环境变量 BIJIA_INPUT，否则仓库根目录「全量比价.xlsx」，
再否则尝试 ~/Desktop（或 ~/桌面）上的同名/副本表。
结果默认写到 output/比价结果.xlsx 与 output/比价结果.jsonl（jsonl 用于断点续跑）。
「失败待重试」不会记入终态，再次运行会自动重跑。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from bijia.jd_browser import JdBrowser
from bijia.runner import (
    DEFAULT_INPUT,
    DEFAULT_JSONL,
    DEFAULT_OUTPUT,
    DEFAULT_PROFILE,
    resolve_input_path,
    run_batch,
    summarize_progress,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="京东比价（只读原表，结果写到 output/；可用 jsonl 断点续跑）"
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help=f"输入 xlsx。默认：BIJIA_INPUT 或仓库/桌面候选（当前解析为 {DEFAULT_INPUT}）",
    )
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="导出结果 xlsx")
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL, help="断点续跑 jsonl")
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE, help="登录态目录（含 jd-state.json）")
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="本批条数；0 表示所有未完成（含失败待重试）",
    )
    parser.add_argument(
        "--pick",
        choices=("mixed", "links", "search", "sequential"),
        default="mixed",
        help="挑行方式：mixed 链接/搜索各半；sequential 按表顺序",
    )
    parser.add_argument(
        "--via",
        choices=("chrome", "http"),
        default="chrome",
        help="chrome=本机已登录 Chrome（Windows 用 CDP，macOS 用 AppleScript）；http=用 --login 的 cookie",
    )
    parser.add_argument(
        "--chrome-debug-port",
        type=int,
        default=None,
        help="Chrome 远程调试端口（默认 Windows=9222；设置后 macOS 也走 CDP）。也可用 BIJIA_CHROME_DEBUG_PORT",
    )
    parser.add_argument(
        "--no-chrome-launch",
        action="store_true",
        help="CDP 模式下若端口未就绪则报错，不自动拉起 Chrome",
    )
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口（仅部分路径）")
    parser.add_argument("--probe", default="", help="只打开一个京东商品链接，确认能读到券后价")
    parser.add_argument(
        "--login",
        action="store_true",
        help="打开浏览器登录京东，保存到 chrome-profile/jd-state.json（供 --via http）",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="只统计未完成/失败待重试数量，不访问京东",
    )
    args = parser.parse_args()
    input_path = resolve_input_path(args.input)

    if args.login:
        _login(args.profile)
        return

    if args.probe:
        _probe(args.probe, args.profile, headless=not args.headed)
        return

    if args.stats:
        stats = summarize_progress(input_path, args.jsonl)
        print(f"输入表 {input_path}")
        for key, value in stats.items():
            print(f"{key}: {value}")
        print("说明：终态=已匹配/待复核/京东无；失败待重试会在下次 run 时自动重跑。")
        return

    run_batch(
        input_path=input_path,
        xlsx_path=args.output,
        jsonl_path=args.jsonl,
        profile_dir=args.profile,
        limit=args.limit,
        pick=args.pick,
        headless=not args.headed,
        via=args.via,
        chrome_debug_port=args.chrome_debug_port,
        chrome_auto_launch=not args.no_chrome_launch,
    )


def _probe(url: str, profile: Path, headless: bool) -> None:
    if "item.jd.com" in url or "jingdonghealth" in url or "item.m.jd.com" in url:
        from bijia.jd_http import JdHttp

        offer = JdHttp(profile / "jd-state.json").open_offer(url)
        if offer is None:
            print("没有打开商品")
            return
        print(f"标题 {offer.title}")
        print(f"店铺 {offer.shop}")
        print(f"券后价 {offer.coupon_price}")
        print(f"京东价 {offer.jd_price}")
        print(f"有货 {offer.in_stock}")
        print(f"地区 {offer.region}")
        print(f"来源 {offer.price_source}")
        print(f"链接 {offer.url}")
        return
    with JdBrowser(profile, headless=headless) as browser:
        browser.warmup()
        if "item.jd.com" in url or "jingdonghealth" in url:
            offer = browser.open_offer(url)
            if offer is None:
                print("没有打开商品")
                return
            print(f"标题 {offer.title}")
            print(f"店铺 {offer.shop}")
            print(f"券后价 {offer.coupon_price}")
            print(f"京东价 {offer.jd_price}")
            print(f"有货 {offer.in_stock}")
            print(f"地区 {offer.region}")
            print(f"来源 {offer.price_source}")
            print(f"链接 {offer.url}")
            return
        cards = browser.search(url)
        print(f"搜索到 {len(cards)} 条")
        for card in cards[:8]:
            print(f"- {card.shop or '未知店铺'} | {card.sales} | {card.title[:80]} | {card.url}")


def _login(profile: Path) -> None:
    from playwright.sync_api import sync_playwright

    from bijia.jd_http import MOBILE_UA, JdHttp

    browser_dir = profile / "login-browser"
    state_path = profile / "jd-state.json"
    browser_dir.mkdir(parents=True, exist_ok=True)
    print("正在打开京东登录页。请在窗口里登录，登录成功后会自动检查券后价。")
    print("（Windows/macOS 通用；登录态供 --via http 使用。若你已在本机 Chrome 登录京东，可改用 --via chrome。）")
    with sync_playwright() as playwright:
        launch_kwargs = dict(
            user_data_dir=str(browser_dir),
            headless=False,
            locale="zh-CN",
            viewport={"width": 420, "height": 900},
            user_agent=MOBILE_UA,
            is_mobile=True,
            has_touch=True,
        )
        try:
            context = playwright.chromium.launch_persistent_context(channel="chrome", **launch_kwargs)
        except Exception as exc:
            print(f"未能用系统 Chrome 启动（{exc}），改用 Playwright Chromium。")
            context = playwright.chromium.launch_persistent_context(**launch_kwargs)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto("https://plogin.m.jd.com/login/login?appid=2193&returnurl=https%3A%2F%2Fm.jd.com%2F")
        deadline = time.time() + 480
        logged_in = False
        while time.time() < deadline:
            cookies = context.cookies()
            if any(cookie.get("name") in {"pt_key", "thor"} and cookie.get("value") for cookie in cookies):
                context.storage_state(path=str(state_path))
                logged_in = True
                break
            page.wait_for_timeout(2000)
        context.close()
    if not logged_in:
        print("8 分钟内没有检测到登录。")
        return
    offer = JdHttp(state_path).open_offer("https://item.jd.com/10099459140643.html")
    print(
        f"登录后试读：券后价 {offer.coupon_price if offer else None}，"
        f"京东价 {offer.jd_price if offer else None}，"
        f"口径 {offer.price_source if offer else ''}"
    )
    print("登录状态已保存。可用：python run.py --via http --limit 20")


if __name__ == "__main__":
    main()
