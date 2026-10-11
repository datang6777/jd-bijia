"""按条比价。验证页不等人点，等待后继续；失败记成「失败待重试」，不写成「京东无」。"""

from __future__ import annotations

import random
import time
import traceback
from pathlib import Path

from bijia.excel_io import append_record, export_xlsx, load_done_ids, load_products, select_rows
from bijia.jd_browser import JdBlocked, SearchCard
from bijia.jd_chrome import JdChrome
from bijia.jd_http import JdHttp, JdLoginRequired
from bijia.match import INVALID_BRANDS, decide, shop_tier
from bijia.models import Decision, Offer, Product
from bijia.spec import parse_outer_pack, parse_spec

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = Path("/Users/zhaodatang/Desktop/副本全量比价1.xlsx")
DEFAULT_OUTPUT = ROOT / "output" / "试跑100.xlsx"
DEFAULT_JSONL = ROOT / "output" / "试跑100.jsonl"
DEFAULT_PROFILE = ROOT / "chrome-profile"


def run_batch(
    input_path: Path = DEFAULT_INPUT,
    xlsx_path: Path = DEFAULT_OUTPUT,
    jsonl_path: Path = DEFAULT_JSONL,
    profile_dir: Path = DEFAULT_PROFILE,
    limit: int = 100,
    pick: str = "mixed",
    headless: bool = True,
    via: str = "http",
    log=print,
) -> dict[str, int]:
    products = load_products(input_path)
    done = load_done_ids(jsonl_path)
    batch = select_rows(products, limit, pick, done)
    counts = {"已匹配": 0, "待复核": 0, "京东无": 0, "失败待重试": 0}

    def log(message: str) -> None:
        print(message, flush=True)

    log(f"待跑 {len(batch)} 条（挑选方式 {pick}，已完成跳过 {len(done)} 条）")
    if not batch:
        log("没有待跑的行")
        return counts

    cache: dict[tuple, tuple[Decision, str]] = {}
    consecutive_blocks = 0
    client: JdHttp | JdChrome
    if via == "chrome":
        client = JdChrome()
        log("使用当前已登录的 Chrome，会新开一个京东标签页")
    else:
        client = JdHttp(cookie_file=profile_dir / "jd-state.json")
    for index, product in enumerate(batch, start=1):
        key = _identity(product)
        if key in cache and not product.has_given_link:
            decision, source = cache[key]
            source = "同款复用"
        else:
            decision, source = _run_one(client, product, log)
            if not product.has_given_link:
                cache[key] = (decision, source)
        record = _record(product, decision, source)
        append_record(jsonl_path, record)
        export_xlsx(jsonl_path, xlsx_path)
        counts[decision.status] = counts.get(decision.status, 0) + 1
        log(
            f"[{index}/{len(batch)}] {decision.status} {product.pk_id} "
            f"{product.generic} {product.spec} -> {decision.price or '-'} {decision.shop or ''} {decision.reason}"
        )
        if decision.status == "失败待重试" and any(word in decision.reason for word in ("验证", "人数过多", "频控")):
            consecutive_blocks += 1
        else:
            consecutive_blocks = 0
        if consecutive_blocks >= 3:
            log("连续被京东拦住，休息 3 分钟后放慢继续")
            time.sleep(180)
            client.slow = True
            consecutive_blocks = 0
        time.sleep(random.uniform(0.8, 1.6))
    log("汇总 " + "，".join(f"{name} {value}" for name, value in counts.items()))
    log(f"结果文件 {xlsx_path}")
    return counts


def _run_one(client: JdHttp | JdChrome, product: Product, log) -> tuple[Decision, str]:
    last_error = ""
    link_decision: Decision | None = None
    for attempt in range(3):
        try:
            if product.has_given_link:
                offer = client.open_offer(product.given_link)
                if offer is None or not offer.title:
                    raise JdBlocked("商品页没有读到标题")
                link_decision = decide(product, [offer], True)
                if link_decision.status != "京东无":
                    return link_decision, "已有链接"
                log(f"  已有链接对不上（{link_decision.reason}），改为搜索")
            offers = _search_offers(client, product)
            return decide(product, offers, True), "搜索"
        except JdLoginRequired as exc:
            if link_decision is not None:
                return (
                    Decision(
                        status="失败待重试",
                        url=link_decision.url,
                        shop=link_decision.shop,
                        title=link_decision.title,
                        region=link_decision.region,
                        reason=f"{link_decision.reason}；继续搜索需要登录京东",
                    ),
                    "已有链接",
                )
            return Decision(status="失败待重试", reason=str(exc)), "需要登录"
        except JdBlocked as exc:
            text = str(exc)
            if link_decision is not None:
                return (
                    Decision(
                        status="失败待重试",
                        url=link_decision.url,
                        shop=link_decision.shop,
                        title=link_decision.title,
                        region=link_decision.region,
                        reason=f"{link_decision.reason}；{text}",
                    ),
                    "已有链接",
                )
            return Decision(status="失败待重试", reason=text), "被拦住"
        except Exception as exc:
            last_error = f"{exc}"
            log(f"  {product.pk_id} 出错：{exc}")
            traceback.print_exc()
            time.sleep(5)
            if attempt == 2:
                break
    return Decision(status="失败待重试", reason=last_error or "重试后仍失败"), "失败"


def _search_offers(client: JdHttp | JdChrome, product: Product) -> list[Offer]:
    offers: list[Offer] = []
    seen: set[str] = set()
    for keyword in _keywords(product):
        cards = client.search(keyword)
        picked = _pick_cards(product, cards)[:3]
        for card in picked:
            if card.sku in seen:
                continue
            seen.add(card.sku)
            offer = client.open_offer(card.url, hint=card)
            if offer is None:
                continue
            offers.append(offer)
            if _good_enough(product, offers):
                return offers
        decision = decide(product, offers, True) if offers else None
        if decision and decision.status in {"已匹配", "待复核"}:
            return offers
        time.sleep(random.uniform(0.8, 1.6))
    return offers


def _good_enough(product: Product, offers: list[Offer]) -> bool:
    decision = decide(product, offers, True)
    if decision.status != "已匹配" or decision.factor != 1:
        return False
    return shop_tier(decision.shop, decision.title) <= 2


def _pick_cards(product: Product, cards: list[SearchCard]) -> list[SearchCard]:
    generic = _compact(product.spec and product.generic)
    base = _compact(product.generic)
    ranked: list[tuple[int, int, SearchCard]] = []
    fallback: list[tuple[int, int, SearchCard]] = []
    for card in cards:
        blob = _compact(card.title + card.text)
        tier = shop_tier(card.shop, card.text)
        item = (tier, -(card.sales or 0), card)
        fallback.append(item)
        if base and base not in blob and (len(base) < 4 or base[:4] not in blob):
            continue
        ranked.append(item)
    if not ranked:
        return []
    ranked.sort(key=lambda item: (item[0], item[1]))
    return [item[2] for item in ranked]


def _keywords(product: Product) -> list[str]:
    brand = (product.brand or "").strip()
    if brand.lower() in {item.lower() for item in INVALID_BRANDS} or brand in INVALID_BRANDS:
        brand = ""
    generic = (product.generic or "").strip()
    raw = []
    if brand and generic:
        raw.append(f"{brand} {generic}")
    if generic:
        raw.append(generic)
    elif product.name:
        raw.append(product.name[:40])
    seen: set[str] = set()
    result = []
    for item in raw:
        text = " ".join(item.split())
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result[:4]


def _identity(product: Product) -> tuple:
    return (
        product.brand,
        product.generic,
        product.spec,
        product.approval,
        product.manufacturer,
        parse_outer_pack(product.name),
        product.given_link if product.has_given_link else "",
    )


def _record(product: Product, decision: Decision, source: str) -> dict:
    return {
        "普康商品ID": product.pk_id,
        "三方sku": product.third_sku,
        "店铺渠道": product.channel,
        "商品名称": product.name,
        "品牌": product.brand,
        "通用名": product.generic,
        "商品规格": product.spec,
        "生产商家": product.manufacturer,
        "批准文号": product.approval,
        "状态": decision.status,
        "京东券后价": decision.price,
        "价格口径": decision.price_basis,
        "换算倍数": decision.factor if decision.price else "",
        "换算说明": decision.factor_text,
        "京东链接": decision.url,
        "京东店铺": decision.shop,
        "页面标题": decision.title,
        "配送区域": decision.region,
        "置信度": decision.confidence,
        "说明": decision.reason,
        "来源": source,
    }


def _compact(text: str) -> str:
    return "".join((text or "").split()).lower()


def debug_spec(spec: str) -> str:
    parsed = parse_spec(spec)
    return (
        f"dose_mg={parsed.dose_mg} iu={parsed.dose_iu} per_ml={parsed.per_ml} "
        f"per_g={parsed.per_g} pkg_g={parsed.package_g} pkg_ml={parsed.package_ml} "
        f"count={parsed.count}{parsed.count_unit or ''} size={parsed.size_mm or parsed.size_bare} "
        f"percent={parsed.percent} conc={parsed.conc_mg}"
    )
