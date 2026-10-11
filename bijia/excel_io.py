"""读写比价表。只读原表，结果写到新文件，避免破坏原表里的图片。"""

from __future__ import annotations

import json
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from openpyxl import Workbook

from bijia.models import Product

NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"

RESULT_FIELDS = [
    "普康商品ID",
    "三方sku",
    "店铺渠道",
    "商品名称",
    "品牌",
    "通用名",
    "商品规格",
    "生产商家",
    "批准文号",
    "状态",
    "京东券后价",
    "价格口径",
    "换算倍数",
    "换算说明",
    "京东链接",
    "京东店铺",
    "页面标题",
    "配送区域",
    "置信度",
    "说明",
    "来源",
]


def load_products(path: str | Path) -> list[Product]:
    book = zipfile.ZipFile(path)
    try:
        return _load_products(book)
    finally:
        book.close()


def _load_products(book: zipfile.ZipFile) -> list[Product]:
    shared = _shared_strings(book)
    products: list[Product] = []
    row_index = 0
    for _event, elem in ET.iterparse(book.open("xl/worksheets/sheet1.xml"), events=("end",)):
        if elem.tag != f"{NS}row":
            continue
        row_index += 1
        cells = _row_cells(elem, shared)
        elem.clear()
        if row_index == 1:
            continue
        products.append(
            Product(
                pk_id=cells.get(1, ""),
                third_sku=cells.get(2, ""),
                channel=cells.get(3, ""),
                name=cells.get(4, ""),
                brand=cells.get(5, ""),
                generic=cells.get(6, ""),
                spec=cells.get(7, ""),
                manufacturer=cells.get(8, ""),
                approval=cells.get(9, ""),
                given_link=cells.get(12, ""),
                row_index=row_index,
            )
        )
        # 已有京东价的行用空字符串占位，调用方用 jd_price 映射过滤。
        products[-1].existing_price = cells.get(11, "")  # type: ignore[attr-defined]
    return products


def unfinished(products: list[Product]) -> list[Product]:
    return [item for item in products if not getattr(item, "existing_price", "")]


def select_rows(products: list[Product], limit: int, pick: str, done: set[str] | None = None) -> list[Product]:
    done = done or set()
    rows = [item for item in unfinished(products) if item.pk_id not in done]
    if pick == "links":
        chosen = [item for item in rows if item.has_given_link]
    elif pick == "search":
        chosen = [item for item in rows if not item.has_given_link]
    elif pick == "mixed":
        half = (limit // 2) if limit > 0 else 10**9
        linked = [item for item in rows if item.has_given_link][:half]
        rest_needed = (limit - len(linked)) if limit > 0 else 10**9
        rest = [item for item in rows if not item.has_given_link][:rest_needed]
        if limit > 0 and len(linked) + len(rest) < limit:
            seen = {item.pk_id for item in linked + rest}
            for item in rows:
                if item.pk_id in seen:
                    continue
                rest.append(item)
                if len(linked) + len(rest) >= limit:
                    break
        return (linked + rest)[:limit] if limit > 0 else linked + rest
    else:
        chosen = rows
    if limit > 0:
        return chosen[:limit]
    return chosen


def load_done_ids(jsonl_path: Path) -> set[str]:
    if not jsonl_path.exists():
        return set()
    done: set[str] = set()
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("状态") in {"已匹配", "待复核", "京东无"}:
            done.add(record["普康商品ID"])
    return done


def append_record(jsonl_path: Path, record: dict) -> None:
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with jsonl_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def export_xlsx(jsonl_path: Path, xlsx_path: Path) -> None:
    latest: dict[str, dict] = {}
    order: list[str] = []
    if jsonl_path.exists():
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            pk = record["普康商品ID"]
            if pk not in latest:
                order.append(pk)
            latest[pk] = record
    xlsx_path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = "试跑结果"
    sheet.append(RESULT_FIELDS)
    for pk in order:
        record = latest[pk]
        sheet.append([record.get(field, "") for field in RESULT_FIELDS])
    book.save(xlsx_path)
    book.close()


def _shared_strings(book: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    values: list[str] = []
    for node in root.findall(f"{NS}si"):
        texts: list[str] = []
        for text in node.iter(f"{NS}t"):
            if text.text:
                texts.append(text.text)
        values.append("".join(texts))
    return values


def _row_cells(elem: ET.Element, shared: list[str]) -> dict[int, str]:
    cells: dict[int, str] = {}
    for cell in elem.findall(f"{NS}c"):
        ref = cell.attrib.get("r", "")
        column = _column_index(ref)
        if not column:
            continue
        kind = cell.attrib.get("t")
        value_node = cell.find(f"{NS}v")
        if kind == "s" and value_node is not None and value_node.text:
            cells[column] = shared[int(value_node.text)]
        elif value_node is not None and value_node.text:
            cells[column] = value_node.text
    return cells


def _column_index(ref: str) -> int:
    letters = "".join(char for char in ref if char.isalpha())
    index = 0
    for char in letters:
        index = index * 26 + (ord(char) - 64)
    return index
