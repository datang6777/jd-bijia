"""按条比价。验证页不等人点，等待后继续；失败记成「失败待重试」，不写成「京东无」。"""

from __future__ import annotations

import json
import os
import random
import sys
import time
import traceback
from collections.abc import Callable
from pathlib import Path

from bijia.excel_io import append_record, export_xlsx, load_done_ids, load_products, select_rows, unfinished
from bijia.jd_browser import JdBlocked, SearchCard
from bijia.jd_chrome import JdChrome, create_jd_chrome
from bijia.jd_http import JdHttp, JdLoginRequired
from bijia.match import INVALID_BRANDS, decide, shop_tier
from bijia.models import Decision, Offer, Product
from bijia.spec import parse_outer_pack, parse_spec

ROOT = Path(__file__).resolve().parents[1]
REPO_INPUT_NAME = "全量比价.xlsx"
DEFAULT_OUTPUT = ROOT / "output" / "比价结果.xlsx"
DEFAULT_JSONL = ROOT / "output" / "比价结果.jsonl"
DEFAULT_PROFILE = ROOT / "chrome-profile"


def _windows_known_desktop() -> Path | None:
    """用 Shell Known Folder 解析真实「桌面」路径（含 OneDrive 重定向）。"""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [
                ("Data1", wintypes.DWORD),
                ("Data2", wintypes.WORD),
                ("Data3", wintypes.WORD),
                ("Data4", wintypes.BYTE * 8),
            ]

        # FOLDERID_Desktop {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        folder_id = GUID(
            0xB4BFCC3A,
            0xDB2C,
            0x424C,
            (wintypes.BYTE * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
        )
        path_ptr = ctypes.c_wchar_p()
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(folder_id),
            0,
            None,
            ctypes.byref(path_ptr),
        )
        if result != 0 or not path_ptr.value:
            return None
        path = Path(path_ptr.value)
        ctypes.windll.ole32.CoTaskMemFree(path_ptr)
        return path
    except Exception:
        return None


def _desktop_dirs() -> list[Path]:
    """跨平台桌面候选：Windows Desktop/桌面/OneDrive，以及 Known Folder。"""
    home = Path.home()
    dirs: list[Path] = []
    known = _windows_known_desktop()
    if known is not None:
        dirs.append(known)
    dirs.extend([home / "Desktop", home / "桌面"])
    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        dirs.append(Path(userprofile) / "Desktop")
        dirs.append(Path(userprofile) / "桌面")
    for key in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        base = os.environ.get(key)
        if base:
            dirs.append(Path(base) / "Desktop")
            dirs.append(Path(base) / "桌面")
    # 去重保序
    ordered: list[Path] = []
    seen: set[str] = set()
    for path in dirs:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def input_candidates() -> list[Path]:
    """按优先级列出可能的输入表路径（不一定存在）。"""
    names = (
        REPO_INPUT_NAME,
        "副本全量比价1.xlsx",
        "副本全量比价.xlsx",
        "全量比价1.xlsx",
    )
    candidates: list[Path] = [ROOT / REPO_INPUT_NAME]
    for desktop in _desktop_dirs():
        for name in names:
            candidates.append(desktop / name)
    # 去重但保序
    seen: set[Path] = set()
    ordered: list[Path] = []
    for path in candidates:
        key = path.resolve() if path.exists() else path
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def resolve_default_input() -> Path:
    """解析默认输入表：BIJIA_INPUT > 已存在的候选路径 > 仓库内 全量比价.xlsx。"""
    env = (os.environ.get("BIJIA_INPUT") or "").strip()
    if env:
        return Path(env).expanduser().resolve()
    for path in input_candidates():
        if path.is_file():
            return path.resolve()
    return (ROOT / REPO_INPUT_NAME).resolve()


def resolve_input_path(path: Path | str | None = None) -> Path:
    """CLI/调用方传入的 --input；空则走默认解析。"""
    if path is None or str(path).strip() == "":
        return resolve_default_input()
    return Path(path).expanduser().resolve()


# 模块导入时解析一次，供 argparse default 与外部引用；运行前仍会再 resolve。
DEFAULT_INPUT = resolve_default_input()


def summarize_progress(input_path: Path, jsonl_path: Path) -> dict[str, int]:
    """只读统计：表内未填京东价、jsonl 终态、失败待重试、本批可跑。不访问京东。"""
    products = load_products(input_path)
    open_rows = unfinished(products)
    done = load_done_ids(jsonl_path)
    pending = [item for item in open_rows if item.pk_id not in done]
    failed = 0
    if jsonl_path.exists():
        latest: dict[str, str] = {}
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            latest[record["普康商品ID"]] = record.get("状态", "")
        failed = sum(1 for status in latest.values() if status == "失败待重试")
    return {
        "表内总行": len(products),
        "表内已有京东价": len(products) - len(open_rows),
        "表内未填价": len(open_rows),
        "jsonl终态已完成": len(done),
        "失败待重试": failed,
        "可继续跑": len(pending),
    }


def run_batch(
    input_path: Path | None = None,
    xlsx_path: Path = DEFAULT_OUTPUT,
    jsonl_path: Path = DEFAULT_JSONL,
    profile_dir: Path = DEFAULT_PROFILE,
    limit: int = 100,
    pick: str = "mixed",
    headless: bool = True,
    via: str = "http",
    log: Callable[[str], None] | None = None,
    chrome_debug_port: int | None = None,
    chrome_auto_launch: bool = True,
) -> dict[str, int]:
    emit: Callable[[str], None] = log or (lambda message: print(message, flush=True))
    input_path = resolve_input_path(input_path)
    if not input_path.is_file():
        raise FileNotFoundError(
            f"找不到输入表：{input_path}\n"
            f"可用 --input 指定，或设置环境变量 BIJIA_INPUT，"
            f"或把工作簿放到仓库根目录命名为 {REPO_INPUT_NAME}。"
        )

    products = load_products(input_path)
    done = load_done_ids(jsonl_path)
    batch = select_rows(products, limit, pick, done)
    counts = {"已匹配": 0, "待复核": 0, "京东无": 0, "失败待重试": 0}

    emit(f"输入表 {input_path}")
    emit(f"待跑 {len(batch)} 条（挑选方式 {pick}，终态已跳过 {len(done)} 条；失败待重试会自动重跑）")
    if not batch:
        emit("没有待跑的行")
        return counts

    cache: dict[tuple, tuple[Decision, str]] = {}
    consecutive_blocks = 0
    client: JdHttp | JdChrome
    if via == "chrome":
        client = create_jd_chrome(
            debug_port=chrome_debug_port,
            auto_launch=chrome_auto_launch,
            log=emit,
        )
        backend = getattr(client, "backend", "chrome")
        if backend == "cdp":
            emit("使用 Chrome CDP（远程调试）驱动已登录窗口，会新开一个标签页")
        else:
            emit("使用当前已登录的 Chrome（AppleScript），会新开一个京东标签页")
    else:
        client = JdHttp(cookie_file=profile_dir / "jd-state.json")
    try:
        return _run_batch_loop(
            client=client,
            batch=batch,
            jsonl_path=jsonl_path,
            xlsx_path=xlsx_path,
            counts=counts,
            cache=cache,
            consecutive_blocks=consecutive_blocks,
            emit=emit,
        )
    finally:
        closer = getattr(client, "close", None)
        if callable(closer):
            closer()


_SEARCH_FAIL_MARKERS = (
    "验证",
    "人数过多",
    "频控",
    "访问频繁",
    "无法搜索",
    "搜索已被频控",
    "没有读到商品列表",
    "本轮其余搜索先跳过",
)


def _run_batch_loop(
    *,
    client,
    batch: list[Product],
    jsonl_path: Path,
    xlsx_path: Path,
    counts: dict[str, int],
    cache: dict[tuple, tuple[Decision, str]],
    consecutive_blocks: int,
    emit: Callable[[str], None],
) -> dict[str, int]:
    search_tip_shown = False
    for index, product in enumerate(batch, start=1):
        if getattr(client, "search_blocked", False) and not search_tip_shown:
            emit(
                "搜索已被京东频控锁定本轮。建议先跑有链接的行："
                "python run.py --pick links --limit 0"
                "（本工作簿约有二百条已有京东链接）"
            )
            search_tip_shown = True
        if getattr(client, "search_blocked", False) and not product.has_given_link:
            decision = Decision(
                status="失败待重试",
                reason="搜索已被频控，本轮跳过无链接行；请稍后重试或先跑 --pick links",
            )
            source = "搜索频控跳过"
        else:
            key = _identity(product)
            if key in cache and not product.has_given_link:
                decision, source = cache[key]
                source = "同款复用"
            else:
                decision, source = _run_one(client, product, emit)
                if not product.has_given_link:
                    cache[key] = (decision, source)
        record = _record(product, decision, source)
        append_record(jsonl_path, record)
        export_xlsx(jsonl_path, xlsx_path)
        counts[decision.status] = counts.get(decision.status, 0) + 1
        emit(
            f"[{index}/{len(batch)}] {decision.status} {product.pk_id} "
            f"{product.generic} {product.spec} -> {decision.price or '-'} {decision.shop or ''} {decision.reason}"
        )
        if decision.status == "失败待重试" and any(
            word in decision.reason for word in _SEARCH_FAIL_MARKERS
        ):
            consecutive_blocks += 1
        else:
            consecutive_blocks = 0
        # 验证码/频控/连续空搜：冷却 5–10 分钟后再继续搜索类请求
        if consecutive_blocks >= 2:
            cool = random.uniform(300, 600)
            emit(
                f"连续被京东搜索拦住或空结果，冷却 {int(cool // 60)} 分钟后放慢继续；"
                "也可中断后改用 python run.py --pick links"
            )
            time.sleep(cool)
            client.slow = True
            consecutive_blocks = 0
        time.sleep(random.uniform(0.8, 1.6))
    emit("汇总 " + "，".join(f"{name} {value}" for name, value in counts.items()))
    emit(f"结果文件 {xlsx_path}")
    if getattr(client, "search_blocked", False):
        emit("本批搜索曾被频控。未完成的有链接行可用：python run.py --pick links --limit 0")
    return counts


def _run_one(client: JdHttp | JdChrome, product: Product, log: Callable[[str], None]) -> tuple[Decision, str]:
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
