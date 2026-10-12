"""按比价规范在候选商品里选一条，并算出券后价。"""

from __future__ import annotations

import re

from bijia.models import Decision, Offer, Product
from bijia.spec import (
    combine_factor,
    compare_specs,
    extract_variants,
    parse_spec,
    variant_relation,
)

TIER1_SHOPS = (
    "京东大药房",
    "京东买药",
    "京东健康大药房",
    "京东健康自营旗舰店",
    "京东健康医药",
)

INVALID_BRANDS = {"", "#n/a", "无品牌", "/", "-", "无", "none", "null", "#N/A"}

APPROVAL_RE = re.compile(
    r"国药准字\s*[A-Z]\s*[A-Z0-9]{6,}"
    r"|食健备\s*[A-Z0-9]+"
    r"|[\u4e00-\u9fff]{0,8}械(?:注准|备)\s*[A-Z0-9]+号?",
    re.I,
)


def shop_tier(shop: str, text: str = "") -> int:
    # 只看这家店的名字。页面正文里常会提到「京东大药房」的推荐位，不能拿来定级。
    blob = (shop or "").strip() or (text or "")
    if any(key in blob for key in TIER1_SHOPS):
        return 1
    if "自营" in blob:
        return 2
    return 3


def decide(
    product: Product,
    offers: list[Offer],
    search_ok: bool,
    error: str = "",
    prefer_sku: str = "",
) -> Decision:
    if not search_ok:
        return Decision(status="失败待重试", reason=error or "页面没有打开，稍后重试")

    prefer = (prefer_sku or "").strip()
    ranked: list[_Scored] = []
    saw_generic = False
    drop_reasons: list[str] = []
    price_gap: _Scored | None = None
    for offer in offers:
        scored = score_offer(product, offer)
        if scored.saw_generic:
            saw_generic = True
        if scored.keep:
            ranked.append(scored)
        elif scored.drop_reason:
            drop_reasons.append(scored.drop_reason)
            if scored.drop_reason == "price" and price_gap is None:
                price_gap = scored

    if not ranked:
        if price_gap is not None and set(drop_reasons) <= {"price"}:
            needs_login = "登录" in (price_gap.offer.price_source or "")
            return Decision(
                status="失败待重试",
                url=price_gap.offer.url,
                shop=price_gap.offer.shop,
                title=_one_line(price_gap.offer.title),
                region=price_gap.offer.region,
                reason="京东未登录，券后价要登录后才显示"
                if needs_login
                else "搜索列表没有读到价格（可能懒加载或需登录）",
            )
        if saw_generic:
            return Decision(status="京东无", reason="搜到了同名商品，但品牌、规格或口味型号对不上，或该地区无货")
        return Decision(status="京东无", reason="没有搜到品牌、名称、规格对得上的在售商品")

    ranked.sort(
        key=lambda item: (
            0 if prefer and item.offer.sku == prefer else 1,
            *item.sort_key,
        )
    )
    best = ranked[0]
    status = "已匹配" if best.high else "待复核"
    reason = best.reason
    if prefer and best.offer.sku == prefer:
        reason = "链接 SKU 命中搜索列表；" + reason
    return Decision(
        status=status,
        price=best.price_text,
        url=best.offer.url,
        shop=best.offer.shop,
        title=_one_line(best.offer.title),
        factor=best.factor,
        factor_text=best.factor_text,
        region=best.offer.region,
        confidence="高" if best.high else "低",
        reason=reason,
        price_basis=best.price_basis,
        sku=best.offer.sku,
    )


def score_offer(product: Product, offer: Offer):
    blob = " ".join(
        part
        for part in (offer.title, offer.shop, offer.page_text[:4000] if offer.page_text else "")
        if part
    )
    generic_state = _generic_state(product.generic, offer.title or blob)
    if generic_state == "miss":
        return _Scored(offer, keep=False, saw_generic=False, drop_reason="generic")

    relation = compare_specs(parse_spec(product.spec), parse_spec(_guess_spec(offer)))
    if not relation.ok:
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="spec")

    combined = combine_factor(relation.factor, product.name, f"{offer.title} {offer.page_text[:500]}")
    if combined is None:
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="pack")
    factor, factor_text = combined
    if relation.detail and relation.kind == "multiple":
        factor_text = relation.detail + (f"，{factor_text}" if "单件" in factor_text else "")

    if not offer.in_stock:
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="oos")

    brand_state = _brand_state(product.brand, offer.title, offer.shop)
    approval_state = _approval_state(product.approval, blob)
    if approval_state == "conflict":
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="approval")
    maker_state = _maker_state(product.manufacturer, blob)

    if brand_state == "miss" and maker_state != "match" and approval_state != "match":
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="brand")

    variants = variant_relation(extract_variants(product.spec + " " + product.name), extract_variants(blob))
    if variants[0] == "conflict":
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="variant")

    unit_price, basis = _pick_price(offer)
    if unit_price is None:
        return _Scored(offer, keep=False, saw_generic=True, drop_reason="price")
    final_price = round(unit_price * factor, 2)

    reasons = [factor_text or relation.detail or "规格相符"]
    high = True
    if brand_state == "missing":
        if approval_state != "match" and maker_state != "match":
            high = False
            reasons.append("表格品牌为空，页面也没对上批准文号或厂家")
        elif maker_state == "match" and approval_state != "match":
            reasons.append("品牌为空，按生产厂家对齐")
        elif approval_state == "match":
            reasons.append("品牌为空，批准文号一致")
    elif brand_state == "miss":
        reasons.append("品牌不一致，生产厂家一致")
    if approval_state == "match" and brand_state == "match":
        reasons.append("批准文号一致")
    if variants[0] == "uncertain":
        high = False
        reasons.append(variants[1])
    if relation.uncertain:
        high = False
        reasons.append(relation.detail)
    if basis != "券后价":
        high = False
        reasons.append(basis)
    if generic_state == "weak":
        high = False
        reasons.append("通用名只部分出现在标题里")
    if factor > 1:
        reasons.append(f"换算后价格 { _fmt_price(unit_price) }×{factor}")

    tier = shop_tier(offer.shop, f"{offer.title} {offer.page_text[:300]}")
    return _Scored(
        offer=offer,
        keep=True,
        saw_generic=True,
        high=high,
        factor=factor,
        factor_text=factor_text,
        price=final_price,
        price_text=_fmt_price(final_price),
        price_basis=basis,
        reason="；".join(dict.fromkeys(reasons)),
        tier=tier,
        sales=offer.sales or 0,
    )


class _Scored:
    def __init__(
        self,
        offer: Offer,
        keep: bool,
        saw_generic: bool = False,
        drop_reason: str = "",
        high: bool = False,
        factor: int = 1,
        factor_text: str = "",
        price: float | None = None,
        price_text: str = "",
        price_basis: str = "",
        reason: str = "",
        tier: int = 3,
        sales: int = 0,
    ):
        self.offer = offer
        self.keep = keep
        self.saw_generic = saw_generic
        self.drop_reason = drop_reason
        self.high = high
        self.factor = factor
        self.factor_text = factor_text
        self.price = price
        self.price_text = price_text
        self.price_basis = price_basis
        self.reason = reason
        self.tier = tier
        self.sales = sales

    @property
    def sort_key(self):
        return (
            0 if self.high else 1,
            self.factor,
            self.tier,
            -self.sales,
            self.price if self.price is not None else 10**9,
        )


def _pick_price(offer: Offer) -> tuple[float | None, str]:
    if offer.coupon_price is not None and offer.coupon_price > 0:
        return offer.coupon_price, "券后价"
    if offer.price_source == "搜索列表价" and offer.jd_price is not None and offer.jd_price > 0:
        return offer.jd_price, "搜索列表价"
    if offer.jd_price is not None and offer.jd_price > 0:
        return offer.jd_price, "页面没显示券后价，先用京东价"
    return None, ""


def _generic_state(generic: str, title: str) -> str:
    base, extras = _split_name(generic)
    if not base:
        return "weak"
    title_n = _norm(title)
    if base in title_n:
        if extras and not all(extra in title_n for extra in extras):
            return "weak"
        return "match"
    if len(base) >= 4 and base[:4] in title_n:
        return "weak"
    return "miss"


def _brand_state(brand: str, title: str, shop: str) -> str:
    raw = (brand or "").strip()
    if raw.lower() in {item.lower() for item in INVALID_BRANDS} or raw in INVALID_BRANDS:
        return "missing"
    blob = _norm(title) + _norm(shop)
    token = _norm(raw).replace("牌", "")
    if token and token in blob:
        return "match"
    return "miss"


def _approval_state(approval: str, text: str) -> str:
    wanted = _norm_approval(approval)
    if not wanted:
        return "missing"
    found = [_norm_approval(item) for item in APPROVAL_RE.findall(text or "")]
    found = [item for item in found if item]
    if wanted in _norm(text).upper().replace(" ", ""):
        return "match"
    if any(item == wanted for item in found):
        return "match"
    if found:
        return "conflict"
    return "missing"


def _maker_state(manufacturer: str, text: str) -> str:
    core = re.sub(r"[（(].*?[)）]", "", manufacturer or "")
    for token in ("股份有限公司", "有限责任公司", "有限公司", "公司"):
        core = core.replace(token, "")
    core = core.strip()
    if len(core) < 4:
        return "weak"
    if core in (text or ""):
        return "match"
    if len(core) >= 6 and core[:6] in (text or ""):
        return "match"
    return "miss"


def _guess_spec(offer: Offer) -> str:
    """页面标题和正文里截一段最像规格的文字。"""
    text = f"{offer.title} {offer.page_text[:1500]}"
    text = text.replace("（", "(").replace("）", ")")
    patterns = [
        r"\d+(?:\.\d+)?\s*(?:毫克|mg|克|g|毫升|ml|单位)\s*[*×xX]\s*\d+\s*(?:片|粒|支|袋|丸|贴|只|板|瓶|盒)(?:\s*[*×xX]\s*\d+\s*(?:板|袋|瓶|盒|包))?",
        r"\d+(?:\.\d+)?\s*(?:毫米|厘米|mm|cm)\s*[*×xX]\s*\d+(?:\.\d+)?\s*(?:毫米|厘米|mm|cm)?\s*[*×xX]\s*\d+\s*(?:片|贴|只|袋)",
        r"\d+(?:\.\d+)?\s*(?:克|毫升|毫克|mg|g|ml)\s*[*×]?\s*\d*\s*(?:袋|瓶|盒|支|罐)?",
        r"\d+(?:\.\d+)?\s*%\s*[*×xX]\s*\d+(?:\.\d+)?\s*(?:克|g|毫升|ml)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.I)
        if match:
            return match.group(0)
    return offer.title or ""


def _split_name(name: str) -> tuple[str, list[str]]:
    text = _norm(name)
    extras = [_norm(item) for item in re.findall(r"[（(]([^()（）]+)[)）]", name or "")]
    extras = [item for item in extras if item and item not in {"otc", "rx"}]
    base = re.sub(r"[（(][^()（）]*[)）]", "", text)
    return base, extras


def _norm(text: str) -> str:
    text = (text or "").strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"\s+", "", text)
    return text


def _norm_approval(text: str) -> str:
    text = (text or "").upper().replace(" ", "").replace("（", "").replace("）", "")
    if text in {"", "/", "-", "无", "NONE"}:
        return ""
    return text


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:180]


def _fmt_price(value: float) -> str:
    text = f"{value:.2f}".rstrip("0").rstrip(".")
    return text
