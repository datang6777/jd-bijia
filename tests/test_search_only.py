"""search-only：列表取价与 SKU 优先。"""

from __future__ import annotations

import unittest

from bijia.jd_browser import SearchCard
from bijia.jd_chrome import offer_from_search_card
from bijia.match import decide
from bijia.models import Offer, Product
from bijia.runner import _search_only_keywords


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


class SearchOnlyParseTests(unittest.TestCase):
    def test_offer_from_search_card(self) -> None:
        card = SearchCard(
            sku="100123456",
            url="https://item.jd.com/100123456.html",
            title="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
            text="芬利泰 苯磺酸氨氯地平片 5毫克*28片 ¥12.8 京东大药房 自营",
            shop="京东大药房",
            sales=100,
            price=12.8,
            self_operated=True,
        )
        offer = offer_from_search_card(card)
        self.assertEqual(offer.sku, "100123456")
        self.assertEqual(offer.jd_price, 12.8)
        self.assertEqual(offer.price_source, "搜索列表价")
        self.assertIn("自营", offer.shop)

    def test_prefer_sku_wins_among_kept(self) -> None:
        product = _product()
        other = Offer(
            sku="999",
            url="https://item.jd.com/999.html",
            title="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
            shop="三方旗舰店",
            jd_price=9.9,
            price_source="搜索列表价",
            page_text="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
        )
        linked = Offer(
            sku="100123456",
            url="https://item.jd.com/100123456.html",
            title="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
            shop="京东大药房",
            jd_price=12.8,
            price_source="搜索列表价",
            page_text="芬利泰 苯磺酸氨氯地平片 5毫克*28片 京东大药房",
        )
        decision = decide(product, [other, linked], True, prefer_sku="100123456")
        self.assertEqual(decision.sku, "100123456")
        self.assertIn("SKU", decision.reason)

    def test_search_only_keywords_include_spec_and_ziying(self) -> None:
        keys = _search_only_keywords(_product())
        self.assertTrue(any("5毫克*28片" in key or "苯磺酸氨氯地平片" in key for key in keys))
        self.assertTrue(any(key.endswith("自营") or " 自营" in key for key in keys))


if __name__ == "__main__":
    unittest.main()
