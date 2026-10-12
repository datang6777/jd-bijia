"""CDP 拟人模式：复用标签、参数传递等单元测试（不连真 Chrome）。"""

from __future__ import annotations

import unittest
from unittest import mock

from bijia import jd_chrome
from bijia.jd_chrome import pick_existing_jd_page


class FakePage:
    def __init__(self, url: str) -> None:
        self.url = url


class FakeContext:
    def __init__(self, pages: list[FakePage]) -> None:
        self.pages = pages

    def new_page(self) -> FakePage:
        page = FakePage("about:blank")
        self.pages.append(page)
        return page


class FakeBrowser:
    def __init__(self, contexts: list[FakeContext]) -> None:
        self.contexts = contexts

    def new_context(self) -> FakeContext:
        ctx = FakeContext([])
        self.contexts.append(ctx)
        return ctx


class PickJdPageTests(unittest.TestCase):
    def test_reuses_existing_jd_tab(self) -> None:
        jd = FakePage("https://www.jd.com/")
        other = FakePage("https://example.com/")
        browser = FakeBrowser([FakeContext([other, jd])])
        context, page, owned = pick_existing_jd_page(browser)
        self.assertFalse(owned)
        self.assertIs(page, jd)
        self.assertIs(context, browser.contexts[0])

    def test_opens_new_when_no_jd_tab(self) -> None:
        browser = FakeBrowser([FakeContext([FakePage("https://example.com/")])])
        _context, page, owned = pick_existing_jd_page(browser)
        self.assertTrue(owned)
        self.assertEqual(page.url, "about:blank")


class HumanFlagTests(unittest.TestCase):
    def test_create_passes_human_to_cdp(self) -> None:
        with mock.patch.object(jd_chrome, "resolve_chrome_backend", return_value=("cdp", 9222)):
            with mock.patch.object(jd_chrome, "JdChromeCDP") as cdp_cls:
                cdp_cls.return_value = mock.Mock(backend="cdp", human=True)
                jd_chrome.create_jd_chrome(
                    debug_port=9222,
                    auto_launch=False,
                    human=True,
                    min_gap=20,
                    max_gap=40,
                )
        kwargs = cdp_cls.call_args.kwargs
        self.assertTrue(kwargs["human"])
        self.assertEqual(kwargs["min_gap"], 20)
        self.assertEqual(kwargs["max_gap"], 40)
        self.assertFalse(kwargs["auto_launch"])


if __name__ == "__main__":
    unittest.main()
