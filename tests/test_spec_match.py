import unittest

from bijia.jd_http import parse_mobile_item
from bijia.match import decide, shop_tier
from bijia.models import Offer, Product
from bijia.parse_page import PageFacts, absorb_json_text, apply_visible_text
from bijia.spec import compare_specs, parse_outer_pack, parse_spec


def product(**kwargs) -> Product:
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
    )
    base.update(kwargs)
    return Product(**base)


def offer(**kwargs) -> Offer:
    base = dict(
        sku="100",
        url="https://item.jd.com/100.html",
        title="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
        shop="京东大药房",
        coupon_price=12.8,
        jd_price=18.0,
        sales=1000,
        in_stock=True,
        page_text="芬利泰 苯磺酸氨氯地平片 5毫克*28片 吉林天衡英睿制药有限公司 国药准字H20203370 京东大药房",
    )
    base.update(kwargs)
    return Offer(**base)


class SpecTests(unittest.TestCase):
    def test_dose_and_count(self):
        parsed = parse_spec("5毫克*28片")
        self.assertAlmostEqual(parsed.dose_mg, 5)
        self.assertEqual(parsed.count, 28)
        self.assertEqual(parsed.count_unit, "片")

    def test_gram_to_mg_and_board(self):
        parsed = parse_spec("0.3克*24片*2板")
        self.assertAlmostEqual(parsed.dose_mg, 300)
        self.assertEqual(parsed.count, 48)
        self.assertEqual(parsed.count_unit, "片")

    def test_mg_boards_equal_tablets(self):
        relation = compare_specs(parse_spec("50mg*20片*3板"), parse_spec("50mg*60片"))
        self.assertTrue(relation.ok)
        self.assertEqual(relation.factor, 1)

    def test_abbreviated_count_can_match_full_dose(self):
        relation = compare_specs(parse_spec("20片"), parse_spec("0.25克*20片"))
        self.assertTrue(relation.ok)
        self.assertEqual(relation.factor, 1)
        self.assertTrue(relation.uncertain)

    def test_missing_dose_cannot_scale_count(self):
        relation = compare_specs(parse_spec("50毫克*60片"), parse_spec("20片"))
        self.assertFalse(relation.ok)

    def test_strength_cannot_scale(self):
        relation = compare_specs(parse_spec("25毫克*20片"), parse_spec("50毫克*10片"))
        self.assertFalse(relation.ok)

    def test_quantity_can_scale(self):
        relation = compare_specs(parse_spec("50毫克*60片"), parse_spec("50毫克*20片"))
        self.assertTrue(relation.ok)
        self.assertEqual(relation.factor, 3)

    def test_weight_cannot_scale(self):
        relation = compare_specs(parse_spec("700克"), parse_spec("350克"))
        self.assertFalse(relation.ok)

    def test_liquid_pack(self):
        parsed = parse_spec("240毫升(10毫升*24支)(草莓味)")
        self.assertAlmostEqual(parsed.per_ml, 10)
        self.assertEqual(parsed.count, 24)
        self.assertEqual(parsed.count_unit, "支")
        self.assertIn("草莓味", parsed.variants["flavor"])

    def test_size(self):
        parsed = parse_spec("70*22毫米*6片")
        self.assertEqual(parsed.size_mm, (70, 22))
        self.assertEqual(parsed.count, 6)
        relation = compare_specs(parsed, parse_spec("70毫米*22毫米*6片"))
        self.assertTrue(relation.ok)
        self.assertEqual(relation.factor, 1)

    def test_concentration(self):
        relation = compare_specs(parse_spec("10克:300毫克"), parse_spec("10克:150毫克"))
        self.assertFalse(relation.ok)
        same = compare_specs(parse_spec("10克:300毫克"), parse_spec("10g:300mg"))
        self.assertTrue(same.ok)

    def test_outer_pack(self):
        self.assertEqual(parse_outer_pack("氨氯地平片【两盒装】"), 2)
        self.assertEqual(parse_outer_pack("【四盒装】氨咖黄敏"), 4)
        self.assertEqual(parse_outer_pack("普通装"), 1)


class MatchTests(unittest.TestCase):
    def test_pharmacy_exact_wins(self):
        third = offer(
            sku="2",
            url="https://item.jd.com/2.html",
            shop="某某大药房旗舰店",
            coupon_price=9,
            sales=99999,
            title="芬利泰 苯磺酸氨氯地平片 5毫克*28片",
        )
        decision = decide(product(), [third, offer()], True)
        self.assertEqual(decision.status, "已匹配")
        self.assertEqual(decision.sku, "100")
        self.assertEqual(decision.price, "12.8")

    def test_exact_third_party_beats_converted_pharmacy(self):
        pharmacy = offer(
            title="信谊 硫唑嘌呤片 50mg*20片",
            page_text="信谊 硫唑嘌呤片 50mg*20片 京东大药房 国药准字H31021422 上海上药信谊药厂",
            coupon_price=20,
            sku="20",
        )
        third = offer(
            sku="60",
            url="https://item.jd.com/60.html",
            shop="某某大药房旗舰店",
            title="信谊 硫唑嘌呤片 50mg*60片",
            page_text="信谊 硫唑嘌呤片 50mg*60片 国药准字H31021422 上海上药信谊药厂",
            coupon_price=55,
            sales=20,
        )
        item = product(
            name="信谊 硫唑嘌呤片 50mg*60片",
            brand="信谊",
            generic="硫唑嘌呤片",
            spec="50mg*60片",
            manufacturer="上海上药信谊药厂有限公司",
            approval="国药准字H31021422",
        )
        decision = decide(item, [pharmacy, third], True)
        self.assertEqual(decision.sku, "60")
        self.assertEqual(decision.factor, 1)

    def test_box_multiplier(self):
        item = product(name="芬利泰 苯磺酸氨氯地平片 5毫克*28片【两盒装】")
        decision = decide(item, [offer()], True)
        self.assertEqual(decision.status, "已匹配")
        self.assertEqual(decision.factor, 2)
        self.assertEqual(decision.price, "25.6")

    def test_flavor_conflict_is_not_a_match(self):
        item = product(
            name="钙铁锌口服液 240毫升(10毫升*24支)(草莓味)",
            brand="石药牌",
            generic="钙铁锌口服液",
            spec="240毫升(10毫升*24支)(草莓味)",
            manufacturer="石药集团中诺药业(泰州)有限公司",
            approval="食健备G202032001815",
        )
        wrong = offer(
            title="石药牌 钙铁锌口服液 10毫升*24支(原味)",
            shop="京东大药房",
            page_text="石药牌 钙铁锌口服液 10毫升*24支 原味 石药集团中诺药业 食健备G202032001815",
            coupon_price=40,
        )
        decision = decide(item, [wrong], True)
        self.assertEqual(decision.status, "京东无")

    def test_missing_flavor_needs_review(self):
        item = product(
            name="钙铁锌口服液(草莓味)",
            brand="石药牌",
            generic="钙铁锌口服液",
            spec="10毫升*24支(草莓味)",
            manufacturer="石药集团中诺药业(泰州)有限公司",
            approval="",
        )
        found = offer(
            title="石药牌 钙铁锌口服液 10毫升*24支",
            page_text="石药牌 钙铁锌口服液 10毫升*24支 石药集团中诺药业",
            coupon_price=40,
        )
        decision = decide(item, [found], True)
        self.assertEqual(decision.status, "待复核")
        self.assertEqual(decision.price, "40")

    def test_out_of_stock_is_jd_none(self):
        decision = decide(product(), [offer(in_stock=False)], True)
        self.assertEqual(decision.status, "京东无")

    def test_blank_brand_uses_factory(self):
        item = product(brand="#N/A", approval="")
        decision = decide(item, [offer(title="苯磺酸氨氯地平片 5毫克*28片 吉林天衡英睿")], True)
        self.assertIn(decision.status, {"已匹配", "待复核"})
        self.assertEqual(decision.price, "12.8")

    def test_wrong_brand_rejected(self):
        found = offer(
            title="络活喜 苯磺酸氨氯地平片 5毫克*28片",
            shop="京东大药房",
            page_text="络活喜 苯磺酸氨氯地平片 5毫克*28片 辉瑞制药有限公司 国药准字H10950224",
        )
        decision = decide(product(), [found], True)
        self.assertEqual(decision.status, "京东无")

    def test_shop_tier(self):
        self.assertEqual(shop_tier("京东买药", ""), 1)
        self.assertEqual(shop_tier("京东健康自营旗舰店", ""), 1)
        self.assertEqual(shop_tier("信谊京东自营旗舰店", "自营"), 2)
        self.assertEqual(shop_tier("某某旗舰店", ""), 3)

    def test_search_failed_is_retry(self):
        decision = decide(product(), [], False, "验证页")
        self.assertEqual(decision.status, "失败待重试")

    def test_hidden_price_keeps_the_item(self):
        hidden = offer(coupon_price=None, jd_price=None, price_source="需要登录")
        decision = decide(product(), [hidden], True)
        self.assertEqual(decision.status, "失败待重试")
        self.assertIn("登录", decision.reason)
        self.assertEqual(decision.shop, "京东大药房")
        self.assertEqual(decision.url, "https://item.jd.com/100.html")


class MobilePageTests(unittest.TestCase):
    def test_masked_price_and_shop(self):
        html = """
        window._itemOnly = ({"item":{"brandName":"大亚芬克","skuName":"大亚芬克 布洛芬缓释片0.3g*14片 1盒"}});
        "shopName":"老百姓上海大药房旗舰店","StockState":33,
        "wqAddrDefault":"0|1_72_0|北京_朝阳区_麦子店街道_|addr",
        "priceFloor": {"price":"1?","ext":{"jdPrice":"1?"}}
        """
        parsed = parse_mobile_item(html, "10099459140643", "https://item.jd.com/10099459140643.html")
        self.assertIn("布洛芬缓释片", parsed.title)
        self.assertEqual(parsed.shop, "老百姓上海大药房旗舰店")
        self.assertTrue(parsed.in_stock)
        self.assertEqual(parsed.region, "北京朝阳区麦子店街道")
        self.assertIsNone(parsed.coupon_price)
        self.assertEqual(parsed.price_source, "需要登录")


class PageParseTests(unittest.TestCase):
    def test_coupon_from_text_and_json(self):
        facts = PageFacts()
        apply_visible_text("到手价 ¥49.90 京东价 ¥69", ["加入购物车"], facts)
        self.assertEqual(facts.coupon_price, 49.9)
        labeled = PageFacts()
        apply_visible_text("收藏¥148到手价¥158满100减10", ["加入购物车"], labeled)
        self.assertEqual(labeled.coupon_price, 148)
        self.assertTrue(facts.in_stock)
        blocked = PageFacts(in_stock=True)
        apply_visible_text("该地区暂时无货", ["到货通知"], blocked)
        self.assertFalse(blocked.in_stock)
        payload = '{"finalPrice":{"price":"12.50","type":"到手价"},"shopName":"京东大药房","stockState":33}'
        facts2 = PageFacts()
        absorb_json_text(payload, facts2)
        self.assertEqual(facts2.coupon_price, 12.5)
        self.assertEqual(facts2.shop, "京东大药房")
        self.assertTrue(facts2.in_stock)


if __name__ == "__main__":
    unittest.main()
