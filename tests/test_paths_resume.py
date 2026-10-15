"""路径默认值与失败续跑相关单元测试。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from bijia.excel_io import load_done_ids, select_rows, unfinished
from bijia.models import Product
from bijia.runner import ROOT, resolve_default_input, resolve_input_path


def _product(pk: str, link: str = "", price: str = "") -> Product:
    item = Product(
        pk_id=pk,
        third_sku="s",
        channel="YF",
        name="样例",
        brand="品牌",
        generic="通用名",
        spec="10mg*10片",
        manufacturer="厂家",
        approval="国药准字H00000000",
        given_link=link,
    )
    item.existing_price = price  # type: ignore[attr-defined]
    return item


class PathResolveTests(unittest.TestCase):
    def test_env_bijia_input_wins(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "from-env.xlsx"
            target.write_bytes(b"pk")
            with mock.patch.dict(os.environ, {"BIJIA_INPUT": str(target)}):
                self.assertEqual(resolve_default_input(), target.resolve())

    def test_repo_workbook_is_preferred_when_present(self) -> None:
        repo_file = ROOT / "全量比价.xlsx"
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BIJIA_INPUT", None)
            resolved = resolve_default_input()
        if repo_file.exists():
            self.assertEqual(resolved, repo_file.resolve())
        else:
            self.assertEqual(resolved.name, "全量比价.xlsx")

    def test_resolve_input_path_expands_user_and_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "desk.xlsx"
            target.write_bytes(b"pk")
            with mock.patch.dict(os.environ, {"BIJIA_INPUT": str(target)}):
                self.assertEqual(resolve_input_path(None), target.resolve())
            self.assertEqual(resolve_input_path(target), target.resolve())


class ResumeTests(unittest.TestCase):
    def test_failure_status_is_not_done(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            jsonl = Path(tmp) / "a.jsonl"
            rows = [
                {"普康商品ID": "A", "状态": "已匹配"},
                {"普康商品ID": "B", "状态": "待复核"},
                {"普康商品ID": "C", "状态": "京东无"},
                {"普康商品ID": "D", "状态": "失败待重试"},
                {"普康商品ID": "D", "状态": "失败待重试"},
            ]
            jsonl.write_text(
                "\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n",
                encoding="utf-8",
            )
            done = load_done_ids(jsonl)
            self.assertEqual(done, {"A", "B", "C"})
            self.assertNotIn("D", done)

    def test_select_rows_retries_failed_and_skips_done(self) -> None:
        products = [
            _product("A", price="12.3"),
            _product("B"),
            _product("C", link="https://item.jd.com/1.html"),
            _product("D"),
        ]
        self.assertEqual([p.pk_id for p in unfinished(products)], ["B", "C", "D"])
        batch = select_rows(products, limit=10, pick="sequential", done={"B"})
        self.assertEqual([p.pk_id for p in batch], ["C", "D"])

    def test_limit_zero_means_all_unfinished(self) -> None:
        products = [_product(str(i)) for i in range(5)]
        batch = select_rows(products, limit=0, pick="sequential", done={"1", "3"})
        self.assertEqual([p.pk_id for p in batch], ["0", "2", "4"])


if __name__ == "__main__":
    unittest.main()
