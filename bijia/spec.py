"""规格解析。数量可以按整数倍换算，剂量和克重不可以。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


CN_NUM = {
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
    "十": 10,
}

_UNIT_ALIASES = [
    ("毫克", "mg"),
    ("微克", "ug"),
    ("千克", "kg"),
    ("毫升", "ml"),
    ("厘米", "cm"),
    ("毫米", "mm"),
    ("人份", "人份"),
    ("单位", "iu"),
    ("mg", "mg"),
    ("kg", "kg"),
    ("ml", "ml"),
    ("mm", "mm"),
    ("cm", "cm"),
    ("iu", "iu"),
    ("ug", "ug"),
    ("克", "g"),
    ("升", "l"),
    ("g", "g"),
    ("l", "l"),
    ("片", "片"),
    ("粒", "粒"),
    ("支", "支"),
    ("袋", "袋"),
    ("丸", "丸"),
    ("贴", "贴"),
    ("只", "只"),
    ("条", "条"),
    ("包", "包"),
    ("瓶", "瓶"),
    ("盒", "盒"),
    ("枚", "枚"),
    ("张", "张"),
    ("个", "个"),
    ("份", "份"),
    ("板", "板"),
    ("吸", "吸"),
    ("罐", "罐"),
    ("%", "%"),
]
_UNIT_ALIASES.sort(key=lambda item: len(item[0]), reverse=True)

DOSE_MASS = {"mg", "g", "ug", "kg"}
VOLUME = {"ml", "l"}
LENGTH = {"mm", "cm"}
COUNT_UNITS = {"片", "粒", "支", "袋", "丸", "贴", "只", "条", "包", "瓶", "盒", "枚", "张", "个", "份", "板", "人份", "吸", "罐"}
MULTIPLY_UNITS = {"板", "瓶", "袋", "盒", "包", "罐"}
BASE_COUNT = {"片", "粒", "丸", "贴", "支", "只", "条", "枚", "张", "个", "份", "人份", "吸", "包"}

OUTER_RE = re.compile(
    r"(?:【\s*)?(\d+|[一二两三四五六七八九十])\s*(盒|份|只|支|包|条|件|个)\s*装\s*】?"
    r"|【\s*(\d+|[一二两三四五六七八九十])\s*(盒|份|只|支|包|条|件|个)\s*】"
)

IGNORE_PARENS = {"otc", "rx", "处方药", "甲类", "乙类", "非处方药"}
COATINGS = ("薄膜衣", "糖衣", "素片", "肠溶", "缓释", "控释")
COLORS = ("云朵渐变色", "浅蓝色", "浅蓝", "粉色", "黑色", "白色", "蓝色", "红色", "绿色", "黄色", "透明", "肤色", "紫色", "橙色", "灰色")


@dataclass
class ParsedSpec:
    raw: str = ""
    dose_mg: float | None = None
    dose_iu: float | None = None
    per_ml: float | None = None
    per_g: float | None = None
    package_g: float | None = None
    package_ml: float | None = None
    percent: float | None = None
    conc_mg: tuple[float, float] | None = None
    size_mm: tuple[float, float] | None = None
    size_bare: tuple[float, float] | None = None
    count: float | None = None
    count_unit: str | None = None
    variants: dict[str, set[str]] = field(default_factory=dict)

    @property
    def recognized(self) -> bool:
        return any(
            value is not None
            for value in (
                self.dose_mg,
                self.dose_iu,
                self.per_ml,
                self.per_g,
                self.package_g,
                self.package_ml,
                self.percent,
                self.conc_mg,
                self.size_mm,
                self.size_bare,
                self.count,
            )
        )


@dataclass
class SpecRelation:
    ok: bool
    factor: int = 1
    kind: str = "incompatible"
    detail: str = ""
    uncertain: bool = False


def parse_outer_pack(name: str) -> int:
    """从商品名称里的「两盒装 / 【3盒】」取外包装盒数。没有则是 1。"""
    if not name:
        return 1
    text = name.replace("（", "【").replace("）", "】")
    found = 1
    for match in OUTER_RE.finditer(text):
        raw = match.group(1) or match.group(3)
        number = _cn_to_int(raw)
        if number and number > 0:
            found = number
    return found


def parse_spec(spec: str) -> ParsedSpec:
    raw = (spec or "").strip()
    variants = extract_variants(raw)
    inner = _first_measure_paren(raw)
    main = re.sub(r"[（(][^()（）]*[)）]", " ", raw)
    parsed = _parse_expr(main)
    if inner:
        inner_parsed = _parse_expr(inner)
        if inner_parsed.count:
            inner_parsed.package_g = parsed.package_g or inner_parsed.package_g
            inner_parsed.package_ml = parsed.package_ml or inner_parsed.package_ml
            if parsed.percent is not None and inner_parsed.percent is None:
                inner_parsed.percent = parsed.percent
            parsed = inner_parsed
    parsed.raw = raw
    parsed.variants = variants
    return parsed


def extract_variants(text: str) -> dict[str, set[str]]:
    groups: dict[str, set[str]] = {
        "flavor": set(),
        "color": set(),
        "model": set(),
        "age": set(),
        "coat": set(),
        "trait": set(),
    }
    if not text:
        return groups
    source = text.replace("（", "(").replace("）", ")")
    for content in re.findall(r"\(([^()]*)\)", source):
        token = content.strip()
        low = token.lower()
        if not token or low in IGNORE_PARENS:
            continue
        if re.search(r"\d", token) and re.search(r"片|粒|支|袋|丸|贴|克|毫升|mg|ml|毫米|厘米", token, re.I):
            continue
        if token.endswith("味"):
            groups["flavor"].add(token)
        elif re.search(r"\d+\s*-\s*\d+\s*岁", token):
            groups["age"].add(re.sub(r"\s+", "", token))
        elif any(token.startswith(coat) or coat in token for coat in COATINGS):
            for coat in COATINGS:
                if coat in token:
                    groups["coat"].add(coat)
        elif token.endswith("色") or token in COLORS:
            groups["color"].add(token)
        else:
            groups["trait"].add(token)

    for match in re.findall(r"[\u4e00-\u9fff]{1,8}味", source):
        if match.startswith("品") or match.startswith("滋"):
            continue
        groups["flavor"].add(match)
    if "无香型" in source:
        groups["flavor"].add("无香型")
    if "有香型" in source:
        groups["flavor"].add("有香型")
    for coat in COATINGS:
        if coat in source:
            groups["coat"].add(coat)
    for color in COLORS:
        if color in source:
            groups["color"].add(color)
    for match in re.findall(r"\d{1,2}\s*-\s*\d{1,2}\s*岁", source):
        groups["age"].add(re.sub(r"\s+", "", match))
    for match in re.findall(r"[A-Za-z]{1,8}(?:-[A-Za-z0-9ⅢⅣⅤⅢIVX]+){1,6}", source):
        groups["model"].add(match.lower())
    for match in re.findall(r"[A-Za-z0-9]*[ⅢⅣⅤⅢ]+型", source):
        groups["model"].add(match.lower())
    for match in re.findall(r"(?<![A-Za-z])(?:XXXL|XXL|XL|XS|S|M|L)(?=码|只|-|/|\b)", source):
        groups["model"].add(f"size:{match.lower()}")
    return groups


def compare_specs(target: ParsedSpec, found: ParsedSpec) -> SpecRelation:
    if not target.recognized:
        return SpecRelation(False, detail="表格规格无法识别", uncertain=True)
    if not found.recognized:
        return SpecRelation(False, detail="页面规格无法识别", uncertain=True)

    if target.conc_mg or found.conc_mg:
        if not target.conc_mg or not found.conc_mg:
            return SpecRelation(False, detail="浓度写法对不上", uncertain=True)
        if not _close(target.conc_mg[0], found.conc_mg[0], 1) or not _close(target.conc_mg[1], found.conc_mg[1], 1):
            return SpecRelation(False, detail="浓度不一致，不能换算")
        return _with_count(target, found, "浓度相同")

    if target.percent is not None and found.percent is not None:
        if not _close(target.percent, found.percent, 0.2):
            return SpecRelation(False, detail="浓度不一致")

    if target.dose_mg is not None or found.dose_mg is not None:
        if target.dose_mg is not None and found.dose_mg is not None:
            if not _close(target.dose_mg, found.dose_mg, 0.51):
                return SpecRelation(False, detail="剂量不一致，不能换算")
            return _with_count(target, found, "剂量相同")
        counted = _with_count(target, found, "数量相同")
        if not counted.ok or counted.factor != 1:
            return SpecRelation(False, detail="一边没写剂量，数量又不是同一盒，不能换算")
        counted.uncertain = True
        counted.detail = "数量相同，但有一边没写剂量"
        return counted

    if target.dose_iu is not None or found.dose_iu is not None:
        if not _close(target.dose_iu, found.dose_iu, 0.51):
            return SpecRelation(False, detail="单位剂量不一致")
        return _with_count(target, found, "单位剂量相同")

    if target.per_ml is not None or found.per_ml is not None:
        if target.per_ml is not None and found.per_ml is not None:
            if not _close(target.per_ml, found.per_ml, 0.2):
                return SpecRelation(False, detail="单支容量不一致")
            return _with_count(target, found, "单支容量相同")

    if target.per_g is not None or found.per_g is not None:
        if target.per_g is not None and found.per_g is not None:
            if not _close(target.per_g, found.per_g, 0.2):
                return SpecRelation(False, detail="单袋克重不一致，不能换算")
            return _with_count(target, found, "单袋克重相同")

    size_rel = _size_relation(target, found)
    if size_rel == "conflict":
        return SpecRelation(False, detail="尺寸不一致")
    if target.size_mm or target.size_bare or found.size_mm or found.size_bare:
        counted = _with_count(target, found, "尺寸相同" if size_rel == "match" else "尺寸未在页面写明")
        if not counted.ok:
            return counted
        if size_rel != "match":
            counted.uncertain = True
            counted.detail = "数量可换算，但页面没写清尺寸"
        return counted

    weight = _package_relation(target.package_g, found.package_g, target, found, "克重")
    if weight is not None:
        return weight
    volume = _package_relation(target.package_ml, found.package_ml, target, found, "容量")
    if volume is not None:
        return volume

    if target.per_g is not None and found.package_g is not None and (target.count or 1) == 1:
        if _close(target.per_g, found.package_g, 0.2):
            return SpecRelation(True, 1, "exact", "克重相同")
    if found.per_g is not None and target.package_g is not None and (found.count or 1) == 1:
        if _close(found.per_g, target.package_g, 0.2):
            return SpecRelation(True, 1, "exact", "克重相同")
    if target.per_ml is not None and found.package_ml is not None and (target.count or 1) == 1:
        if _close(target.per_ml, found.package_ml, 0.2):
            return SpecRelation(True, 1, "exact", "容量相同")
    if found.per_ml is not None and target.package_ml is not None and (found.count or 1) == 1:
        if _close(found.per_ml, target.package_ml, 0.2):
            return SpecRelation(True, 1, "exact", "容量相同")

    return SpecRelation(False, detail="规格对不上", uncertain=True)


def variant_relation(target: dict[str, set[str]], found: dict[str, set[str]]) -> tuple[str, str]:
    """返回 match / uncertain / conflict。"""
    uncertain = []
    for key, label in (
        ("flavor", "口味"),
        ("color", "颜色"),
        ("model", "型号"),
        ("age", "适用年龄"),
        ("coat", "衣层/剂型"),
    ):
        left = target.get(key) or set()
        right = found.get(key) or set()
        if not left:
            continue
        if not right:
            uncertain.append(label)
            continue
        if left.isdisjoint(right):
            return "conflict", f"{label}不同：表格{'/'.join(sorted(left))}，页面{'/'.join(sorted(right))}"
    if uncertain:
        return "uncertain", "页面未写明" + "、".join(uncertain)
    return "match", ""


def combine_factor(inner_factor: int, target_name: str, found_title: str) -> tuple[int, str] | None:
    target_outer = parse_outer_pack(target_name)
    found_outer = parse_outer_pack(found_title)
    if found_outer > target_outer and inner_factor == 1:
        return None
    if target_outer % found_outer != 0:
        return None
    outer_factor = target_outer // found_outer
    factor = inner_factor * outer_factor
    notes = []
    if inner_factor > 1:
        notes.append(f"数量×{inner_factor}")
    if outer_factor > 1:
        notes.append(f"单件×{outer_factor}")
    if not notes:
        notes.append("同规格")
    return factor, "，".join(notes)


def _with_count(target: ParsedSpec, found: ParsedSpec, prefix: str) -> SpecRelation:
    factor = _count_factor(target.count, target.count_unit, found.count, found.count_unit)
    if factor is None:
        return SpecRelation(False, detail="数量对不上，或不是整数倍")
    if factor == 1:
        return SpecRelation(True, 1, "exact", prefix)
    unit = target.count_unit or found.count_unit or ""
    found_count = _fmt_num(found.count or 0)
    target_count = _fmt_num(target.count or 0)
    detail = f"{prefix}，{found_count}{unit}×{factor}={target_count}{unit}"
    return SpecRelation(True, factor, "multiple", detail)


def _count_factor(
    target_count: float | None,
    target_unit: str | None,
    found_count: float | None,
    found_unit: str | None,
) -> int | None:
    if target_count is None and found_count is None:
        return 1
    if target_count is None or found_count is None or found_count == 0:
        return None
    if target_unit and found_unit and target_unit != found_unit:
        return None
    ratio = target_count / found_count
    nearest = round(ratio)
    if nearest < 1:
        return None
    if abs(ratio - nearest) > 0.03:
        return None
    return nearest


def _package_relation(
    target_value: float | None,
    found_value: float | None,
    target: ParsedSpec,
    found: ParsedSpec,
    label: str,
) -> SpecRelation | None:
    if target_value is None and found_value is None:
        return None
    if target_value is None or found_value is None:
        return None
    if not _close(target_value, found_value, max(0.2, target_value * 0.01)):
        return SpecRelation(False, detail=f"{label}不一致，不能换算")
    factor = _count_factor(target.count, target.count_unit, found.count, found.count_unit)
    if factor is None:
        if (target.count or 1) == 1 and (found.count or 1) == 1:
            return SpecRelation(True, 1, "exact", f"{label}相同")
        return SpecRelation(False, detail=f"{label}相同但件数不是整数倍")
    if factor == 1:
        return SpecRelation(True, 1, "exact", f"{label}相同")
    return SpecRelation(True, factor, "multiple", f"{label}相同，件数×{factor}")


def _size_relation(target: ParsedSpec, found: ParsedSpec) -> str:
    if not (target.size_mm or target.size_bare or found.size_mm or found.size_bare):
        return "none"
    if target.size_mm and found.size_mm and _pair_close(target.size_mm, found.size_mm, 1.6):
        return "match"
    if target.size_bare and found.size_bare and _pair_close(target.size_bare, found.size_bare, 0.2):
        return "match"
    if target.size_bare and found.size_mm:
        scaled = (target.size_bare[0] * 10, target.size_bare[1] * 10)
        if _pair_close(target.size_bare, found.size_mm, 1.6) or _pair_close(scaled, found.size_mm, 1.6):
            return "match"
    if target.size_mm and found.size_bare:
        scaled = (found.size_bare[0] * 10, found.size_bare[1] * 10)
        if _pair_close(target.size_mm, found.size_bare, 1.6) or _pair_close(target.size_mm, scaled, 1.6):
            return "match"
    if (target.size_mm or target.size_bare) and not (found.size_mm or found.size_bare):
        return "missing"
    if (found.size_mm or found.size_bare) and (target.size_mm or target.size_bare):
        return "conflict"
    return "missing"


def _parse_expr(expr: str) -> ParsedSpec:
    parsed = ParsedSpec()
    text = expr.replace("×", "*").replace("＊", "*").replace("：", ":")
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", "", text)
    conc = re.search(
        r"(\d+(?:\.\d+)?)(克|毫克|mg|g|ml|毫升)\s*:\s*(\d+(?:\.\d+)?)(克|毫克|mg|g|ml|毫升)",
        text,
        re.I,
    )
    if conc and text.startswith(conc.group(0)):
        left = _to_mg(float(conc.group(1)), _canon_unit(conc.group(2)) or "g")
        right = _to_mg(float(conc.group(3)), _canon_unit(conc.group(4)) or "mg")
        if left is not None and right is not None:
            parsed.conc_mg = (left, right)
            rest = text[len(conc.group(0)) :]
            if rest.startswith("*"):
                rest_parsed = _parse_expr(rest[1:])
                parsed.count = rest_parsed.count
                parsed.count_unit = rest_parsed.count_unit
            return parsed

    text = re.sub(
        r"(\d+(?:\.\d+)?)\*(\d+(?:\.\d+)?)(毫米|厘米|mm|cm)",
        lambda m: f"{m.group(1)}{m.group(3)}*{m.group(2)}{m.group(3)}",
        text,
        flags=re.I,
    )
    text = re.sub(r"\*(袋|盒|瓶|包|罐)(?!\d)", r"*1\1", text)

    doses_mg: list[float] = []
    doses_iu: list[float] = []
    vols: list[float] = []
    grams: list[float] = []
    lengths: list[float] = []
    bare: list[float] = []
    percents: list[float] = []
    counts: list[tuple[float, str]] = []

    for part in text.split("*"):
        if not part:
            continue
        match = re.match(r"(\d+(?:\.\d+)?)(.*)$", part, re.I)
        if not match:
            continue
        number = float(match.group(1))
        unit = _canon_unit(match.group(2))
        if unit == "%":
            percents.append(number)
        elif unit in LENGTH:
            lengths.append(number * (10 if unit == "cm" else 1))
        elif unit == "iu":
            doses_iu.append(number)
        elif unit in DOSE_MASS:
            mg = _to_mg(number, unit)
            grams = grams + [_to_g(number, unit) or 0]
            if mg is not None:
                doses_mg.append(mg)
        elif unit in VOLUME:
            ml = number * 1000 if unit == "l" else number
            vols.append(ml)
        elif unit in COUNT_UNITS:
            counts.append((number, unit))
        elif unit == "":
            if match.group(2).strip() == "":
                bare.append(number)

    if len(lengths) >= 2:
        parsed.size_mm = (lengths[0], lengths[1])
    elif len(bare) >= 2 and counts:
        parsed.size_bare = (bare[0], bare[1])

    folded = _fold_counts(counts)
    if folded:
        parsed.count, parsed.count_unit = folded

    if percents:
        parsed.percent = percents[0]

    has_count = parsed.count is not None
    if doses_iu:
        parsed.dose_iu = doses_iu[0]
    if doses_mg:
        if has_count and parsed.count_unit in BASE_COUNT | {"袋", "包", "瓶", "盒"}:
            if parsed.count_unit in {"袋", "包", "罐"} and not doses_iu:
                parsed.per_g = _to_g_from_mg(doses_mg[0])
            elif vols:
                parsed.per_ml = vols[0]
            else:
                parsed.dose_mg = doses_mg[0]
                if parsed.count_unit in {"袋", "包", "罐"}:
                    parsed.per_g = _to_g_from_mg(doses_mg[0])
                    parsed.dose_mg = None
        elif not has_count:
            parsed.package_g = _to_g_from_mg(doses_mg[0])
        else:
            parsed.dose_mg = doses_mg[0]
    if vols:
        if has_count:
            parsed.per_ml = vols[0]
        elif parsed.package_ml is None:
            parsed.package_ml = vols[0]
    if grams and not doses_mg and not has_count and parsed.package_g is None:
        parsed.package_g = grams[0]
    return parsed


def _fold_counts(counts: list[tuple[float, str]]) -> tuple[float, str] | None:
    if not counts:
        return None
    total, unit = counts[0]
    for number, next_unit in counts[1:]:
        if next_unit in MULTIPLY_UNITS and unit in BASE_COUNT | MULTIPLY_UNITS:
            total *= number
            if unit in MULTIPLY_UNITS and next_unit in BASE_COUNT:
                unit = next_unit
        else:
            total *= number
            unit = next_unit
    return total, unit


def _first_measure_paren(spec: str) -> str | None:
    for content in re.findall(r"[（(]([^()（）]+)[)）]", spec or ""):
        if re.search(r"\d", content) and re.search(
            r"片|粒|支|袋|丸|贴|只|条|包|瓶|盒|板|克|毫升|mg|ml|g|毫米|厘米",
            content,
            re.I,
        ):
            return content
    return None


def _canon_unit(raw: str) -> str:
    text = (raw or "").strip().lower()
    if not text:
        return ""
    for name, canon in _UNIT_ALIASES:
        if text.startswith(name.lower()):
            return canon
    return ""


def _to_mg(number: float, unit: str) -> float | None:
    if unit == "mg":
        return number
    if unit == "g":
        return number * 1000
    if unit == "kg":
        return number * 1_000_000
    if unit == "ug":
        return number / 1000
    return None


def _to_g(number: float, unit: str) -> float | None:
    mg = _to_mg(number, unit)
    if mg is None:
        return None
    return mg / 1000


def _to_g_from_mg(mg: float) -> float:
    return mg / 1000


def _close(left: float | None, right: float | None, tol: float) -> bool:
    if left is None or right is None:
        return False
    return abs(left - right) <= tol


def _pair_close(left: tuple[float, float], right: tuple[float, float], tol: float) -> bool:
    a = sorted(left)
    b = sorted(right)
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def _cn_to_int(raw: str) -> int | None:
    if raw.isdigit():
        return int(raw)
    if raw == "十":
        return 10
    if raw in CN_NUM:
        return CN_NUM[raw]
    return None


def _fmt_num(value: float) -> str:
    if abs(value - round(value)) < 0.001:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")
