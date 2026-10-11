#!/usr/bin/env python3
"""京东比价试跑。

默认从桌面上的全量表取 100 条：一半用表里已有链接，一半自己搜索。
不改原表，结果写到 output/试跑100.xlsx。
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from bijia.jd_browser import JdBrowser
from bijia.runner import DEFAULT_INPUT, DEFAULT_JSONL, DEFAULT_OUTPUT, DEFAULT_PROFILE, run_batch


def main() -> None:
    parser = argparse.ArgumentParser(description="京东比价")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--jsonl", type=Path, default=DEFAULT_JSONL)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--limit", type=int, default=100, help="条数，0 表示未完成的全部")
    parser.add_argument("--pick", choices=("mixed", "links", "search", "sequential"), default="mixed")
    parser.add_argument("--via", choices=("chrome", "http"), default="chrome", help="chrome 用当前已登录的窗口")
    parser.add_argument("--headed", action="store_true", help="显示浏览器窗口")
    parser.add_argument("--probe", default="", help="只打开一个京东商品链接，用来确认能读到券后价")
    parser.add_argument("--login", action="store_true", help="打开浏览器登录京东，登录后才能看到券后价和搜索")
    args = parser.parse_args()

    if args.login:
        _login(args.profile)
        return

    if args.probe:
        _probe(args.probe, args.profile, headless=not args.headed)
        return

    run_batch(
        input_path=args.input,
        xlsx_path=args.output,
        jsonl_path=args.jsonl,
        profile_dir=args.profile,
        limit=args.limit,
        pick=args.pick,
        headless=not args.headed,
        via=args.via,
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
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(browser_dir),
            channel="chrome",
            headless=False,
            locale="zh-CN",
            viewport={"width": 420, "height": 900},
            user_agent=MOBILE_UA,
            is_mobile=True,
            has_touch=True,
        )
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
    print(f"登录后试读：券后价 {offer.coupon_price if offer else None}，京东价 {offer.jd_price if offer else None}，口径 {offer.price_source if offer else ''}")
    print(f"登录状态已保存。重新运行 python run.py 即可继续比价。")


if __name__ == "__main__":
    main()
