"""--pick links：不回退搜索；有 jd_price 时不因缺券后价判未登录。"""

from __future__ import annotations

import unittest
from unittest import mock

from bijia.jd_browser import JdBlocked
from bijia.jd_http import parse_mobile_item
from bijia.match import decide
from bijia.models import Offer, Product
from bijia.runner import _run_one


def _product(**kwargs) -> Product:
    base = dict(
        pk_id="1",
        third_sku="s",
        channel="YF",
        name="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
        brand="芬利泰",
        generic="苯磺酸氨氯地平片",
        spec="5毫克*28片",
        manufacturer="吉林天衡英睿制药有限公司",
        approval="国药准字H20203370",
        given_link="https://item.jd.com/100123456.html",
    )
    base.update(kwargs)
    return Product(**base)


class LinksOnlyRunnerTests(unittest.TestCase):
    def test_pick_links_never_falls_back_to_search(self) -> None:
        # 标题规格对不上 → decide 京东无，但有 jd_price；links 模式不得 search
        mismatched = Offer(
            sku="100123456",
            url="https://item.jd.com/100123456.html",
            title="完全无关的另一商品 维生素C",
            shop="京东大药房",
            coupon_price=None,
            jd_price=18.5,
            price_source="页面没显示券后价，先用京东价",
            page_text="完全无关的另一商品 维生素C 京东大药房",
        )

        class Client:
            def open_offer(self, url, hint=None):
                return mismatched

            def search(self, keyword):
                raise AssertionError("pick=links 不应调用 search")

        logs: list[str] = []
        decision, source = _run_one(Client(), _product(), logs.append, pick="links")
        self.assertEqual(source, "已有链接")
        self.assertEqual(decision.status, "待复核")
        self.assertIn("18.5", decision.price.replace("18.50", "18.5"))
        self.assertEqual(decision.sku, "100123456")
        self.assertFalse(any("改为搜索" in line for line in logs))

    def test_pick_links_keeps_jd_none_when_no_price(self) -> None:
        bad = Offer(
            sku="100123456",
            url="https://item.jd.com/100123456.html",
            title="无关商品",
            shop="某店",
            page_text="无关商品",
        )

        class Client:
            def open_offer(self, url, hint=None):
                return bad

            def search(self, keyword):
                raise AssertionError("pick=links 不应调用 search")

        decision, source = _run_one(Client(), _product(), lambda _m: None, pick="links")
        self.assertEqual(source, "已有链接")
        self.assertEqual(decision.status, "京东无")


class SoftLoginPriceTests(unittest.TestCase):
    def test_jd_price_present_not_login_required(self) -> None:
        html = """
        "skuName":"芬利泰 苯磺酸氨氯地平片 5毫克*28片","brandName":"芬利泰",
        "shopName":"京东大药房","StockState":33,
        "priceFloor": {"price":"?","ext":{}},
        "jdPrice":"18.50"
        """
        parsed = parse_mobile_item(html, "100123456", "https://item.jd.com/100123456.html")
        self.assertEqual(parsed.jd_price, 18.5)
        self.assertNotEqual(parsed.price_source, "需要登录")
        self.assertIn("京东价", parsed.price_source)
        decision = decide(_product(), [parsed], True)
        self.assertNotEqual(decision.status, "失败待重试")
        self.assertNotIn("未登录", decision.reason)

    def test_masked_both_prices_still_login(self) -> None:
        html = """
        "skuName":"大亚芬克 布洛芬缓释片0.3g*14片","brandName":"大亚芬克",
        "shopName":"老百姓上海大药房旗舰店","StockState":33,
        "priceFloor": {"price":"1?","ext":{"jdPrice":"1?"}}
        """
        parsed = parse_mobile_item(html, "10099459140643", "https://item.jd.com/10099459140643.html")
        self.assertIsNone(parsed.jd_price)
        self.assertEqual(parsed.price_source, "需要登录")


if __name__ == "__main__":
    unittest.main()
