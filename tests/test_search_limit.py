"""搜索频控文案检测与批次侧冷却逻辑。"""

from __future__ import annotations

import unittest
from unittest import mock

from bijia.jd_browser import JdBlocked
from bijia.jd_chrome import JdChromeCDP, _is_risk, _is_search_limited, _search_block_reason
from bijia.jd_http import _raise_for_gate
from bijia.models import Decision, Product
from bijia.runner import _run_batch_loop


class SearchLimitDetectTests(unittest.TestCase):
    def test_frequent_search_phrase_is_limited(self) -> None:
        text = "抱歉由于访问频繁导致无法搜索，请稍后再试！"
        self.assertTrue(_is_search_limited("", text))
        self.assertTrue(_is_risk("", text))
        reason = _search_block_reason(text)
        self.assertIn("访问频繁", reason)
        self.assertIn("无法搜索", reason)

    def test_similar_phrases(self) -> None:
        for text in (
            "访问频繁，请稍后再试",
            "当前无法搜索，请稍后再试",
            "搜索太频繁了",
        ):
            self.assertTrue(_is_search_limited("", text), msg=text)

    def test_http_gate_raises_on_frequent_search(self) -> None:
        html = "<html>抱歉由于访问频繁导致无法搜索，请稍后再试！</html>"
        with self.assertRaises(JdBlocked) as ctx:
            _raise_for_gate(html, "https://so.m.jd.com/", allow_item=False)
        self.assertIn("访问频繁", str(ctx.exception))

    def test_cdp_search_sets_blocked_flag(self) -> None:
        client = JdChromeCDP.__new__(JdChromeCDP)
        client.slow = False
        client.search_blocked = False
        client.login_required = False
        client._page = object()
        client._log = lambda _m: None

        with mock.patch.object(
            client,
            "_goto",
        ), mock.patch.object(
            client,
            "_wait_search",
            return_value={
                "href": "https://search.jd.com/Search?keyword=x",
                "title": "京东",
                "text": "抱歉由于访问频繁导致无法搜索，请稍后再试！",
                "items": [],
            },
        ):
            with self.assertRaises(JdBlocked) as ctx:
                client.search("测试药")
        self.assertTrue(client.search_blocked)
        self.assertIn("访问频繁", str(ctx.exception))
        self.assertIn("本轮其余搜索先跳过", str(ctx.exception))


class BatchCooldownTests(unittest.TestCase):
    def test_search_blocked_skips_search_only_rows_and_keeps_links(self) -> None:
        linked = Product(
            pk_id="L1",
            third_sku="s",
            channel="YF",
            name="有链接",
            brand="B",
            generic="G",
            spec="10mg*10片",
            manufacturer="M",
            approval="H1",
            given_link="https://item.jd.com/1.html",
        )
        search_only = Product(
            pk_id="S1",
            third_sku="s",
            channel="YF",
            name="无链接",
            brand="B",
            generic="G",
            spec="10mg*10片",
            manufacturer="M",
            approval="H1",
        )

        class FakeClient:
            search_blocked = True
            slow = False

            def open_offer(self, url, hint=None):
                from bijia.models import Offer

                return Offer(
                    sku="1",
                    url=url,
                    title="B G 10mg*10片",
                    shop="京东大药房",
                    coupon_price=9.9,
                    jd_price=12.0,
                    page_text="B G 10mg*10片 京东大药房",
                    price_source="券后价",
                )

            def search(self, keyword):
                raise JdBlocked("搜索已被频控，本轮其余搜索先跳过")

        logs: list[str] = []
        with tempfile_jsonl() as (jsonl, xlsx):
            with mock.patch("bijia.runner.time.sleep"):
                counts = _run_batch_loop(
                    client=FakeClient(),
                    batch=[search_only, linked],
                    jsonl_path=jsonl,
                    xlsx_path=xlsx,
                    counts={"已匹配": 0, "待复核": 0, "京东无": 0, "失败待重试": 0},
                    cache={},
                    consecutive_blocks=0,
                    emit=logs.append,
                )
        self.assertEqual(counts["失败待重试"], 1)
        self.assertEqual(counts["已匹配"], 1)
        self.assertTrue(any("--pick links" in line for line in logs))

    def test_consecutive_search_blocks_use_long_cooldown(self) -> None:
        sleeps: list[float] = []

        class AlwaysBlocked:
            search_blocked = False
            slow = False

            def open_offer(self, url, hint=None):
                raise JdBlocked("搜索页访问频繁导致无法搜索，请稍后再试")

            def search(self, keyword):
                raise JdBlocked("搜索页访问频繁导致无法搜索，请稍后再试")

        products = [
            Product(
                pk_id=f"P{i}",
                third_sku="s",
                channel="YF",
                name="n",
                brand="B",
                generic="G",
                spec="1片",
                manufacturer="M",
                approval="H",
            )
            for i in range(3)
        ]
        with tempfile_jsonl() as (jsonl, xlsx):
            with mock.patch("bijia.runner.time.sleep", side_effect=lambda s: sleeps.append(s)):
                with mock.patch("bijia.runner.random.uniform", side_effect=lambda a, b: a if a >= 300 else 1.0):
                    _run_batch_loop(
                        client=AlwaysBlocked(),
                        batch=products,
                        jsonl_path=jsonl,
                        xlsx_path=xlsx,
                        counts={"已匹配": 0, "待复核": 0, "京东无": 0, "失败待重试": 0},
                        cache={},
                        consecutive_blocks=0,
                        emit=lambda _m: None,
                    )
        # 至少有一次 5–10 分钟量级冷却（mocked 为 300）
        self.assertTrue(any(s >= 300 for s in sleeps), msg=str(sleeps))


def tempfile_jsonl():
    import tempfile
    from pathlib import Path

    class _Ctx:
        def __enter__(self):
            self._tmp = tempfile.TemporaryDirectory()
            root = Path(self._tmp.name)
            return root / "a.jsonl", root / "a.xlsx"

        def __exit__(self, *args):
            self._tmp.cleanup()

    return _Ctx()


if __name__ == "__main__":
    unittest.main()
