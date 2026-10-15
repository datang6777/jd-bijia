"""openpyxl 写出的 inlineStr 单元格必须能被 load_products 读到。"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

from bijia.excel_io import load_products
from bijia.runner import ROOT


class InlineStrLoadTests(unittest.TestCase):
    def test_openpyxl_workbook_loads_non_empty_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "inline.xlsx"
            book = Workbook()
            sheet = book.active
            sheet.append(
                [
                    "普康商品ID",
                    "三方sku",
                    "店铺",
                    "商品名称",
                    "品牌",
                    "通用名",
                    "商品规格",
                    "生产商家",
                    "批准文号",
                    "图片",
                    "京东价",
                    "京东链接",
                    "京东截图",
                ]
            )
            sheet.append(
                [
                    "PK_INLINE_1",
                    "sku-1",
                    "YF",
                    "测试药 10mg*10片",
                    "测试品牌",
                    "测试通用名",
                    "10mg*10片",
                    "测试厂家",
                    "国药准字H12345678",
                    "",
                    "",
                    "https://item.jd.com/100.html",
                    "",
                ]
            )
            book.save(path)
            book.close()

            # 确认 openpyxl 确实写出了 inlineStr（回归前提）
            with zipfile.ZipFile(path) as zf:
                xml = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
            self.assertIn('t="inlineStr"', xml)

            products = load_products(path)
            self.assertEqual(len(products), 1)
            product = products[0]
            self.assertEqual(product.pk_id, "PK_INLINE_1")
            self.assertEqual(product.brand, "测试品牌")
            self.assertEqual(product.generic, "测试通用名")
            self.assertEqual(product.spec, "10mg*10片")
            self.assertEqual(product.manufacturer, "测试厂家")
            self.assertEqual(product.given_link, "https://item.jd.com/100.html")
            self.assertTrue(product.has_given_link)
            self.assertEqual(getattr(product, "existing_price", ""), "")

    def test_repo_shared_string_workbook_still_loads(self) -> None:
        path = ROOT / "全量比价.xlsx"
        if not path.is_file():
            self.skipTest("仓库内没有 全量比价.xlsx")
        products = load_products(path)
        self.assertGreater(len(products), 10)
        sample = next(item for item in products if item.pk_id)
        self.assertTrue(sample.pk_id)
        self.assertTrue(sample.generic or sample.name or sample.spec)


if __name__ == "__main__":
    unittest.main()
