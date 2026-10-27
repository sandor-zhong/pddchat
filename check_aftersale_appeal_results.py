"""批量回查售后申诉结果；部分通过时保存详情页实际返还金额。"""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import db
import result_query_settings
from aftersale_result_settings import (
    DEFAULT_QUERY_INTERVAL_SECONDS,
    validate_query_interval,
)
from blockers import clear_blockers, safe_click
from login import LoginRequiredError, raise_if_login_page, wait_for_content_or_login
from screenshots import save_error_screenshot, to_relative_evidence_path
from stealth import jitter_wait

AFTERSALE_RESULT_URL = (
    "https://mms.pinduoduo.com/orders/appeals/aftersale/list?from=list&key=0"
)
PLATFORM_STATUSES = {
    "待处理", "审核中", "申诉成功", "申诉失败", "已撤回", "部分通过"
}
RESULT_STATUSES = ("申诉成功", "申诉失败", "部分通过")
TERMINAL_STATUSES = {"申诉成功", "申诉失败", "已撤回", "部分通过"}
WAITING_STATUSES = {"已提交", "待处理", "审核中", "结果待确认"}
_ORDER_RE = re.compile(r"\d{6}-\d+")
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_TIME_TOLERANCE_SECONDS = 30 * 60
_RETURN_AMOUNT_RE = re.compile(
    r"返还金额(?:为)?\s*[：:]?\s*[¥￥]?\s*"
    r"(\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)\s*元"
)
_RESULT_COLUMNS = (
    "订单编号", "申诉类型", "申诉时间", "申诉项", "申诉金额(元)", "审核状态"
)
_RESULT_TABLE_TIMEOUT_MS = 45_000
_RESULT_TABLE_SNAPSHOT = r"""
    (table) => {
        const visible = (node) => {
            if (!node) return false;
            const style = getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return style.display !== 'none' && style.visibility !== 'hidden'
                && Number(style.opacity || 1) !== 0 && rect.width > 0 && rect.height > 0;
        };
        const scope = table.closest('[data-testid="beast-core-spin"]')
            || table.parentElement || table;
        // Spin 外框在加载结束后仍会保留，只检查可见的遮罩和加载提示。
        const loading = [scope, ...scope.querySelectorAll(
            '[data-testid="beast-core-spin-mask"], [aria-busy="true"], '
            + '[data-testid*="loading"], [class*="spinning"], [class*="Loading"]'
        )].some(node => visible(node) && (
            node !== scope || node.getAttribute('aria-busy') === 'true'
        )) || Array.from(scope.querySelectorAll('div, span, p')).some(node =>
            visible(node) && /^加载中[.。…]*$/.test((node.innerText || '').trim())
        );
        const emptyTexts = ['暂无数据', '暂无记录', '暂无申诉记录', '没有符合条件的订单'];
        const empty = Array.from(table.querySelectorAll('div, span, td')).some(node =>
            visible(node) && emptyTexts.includes((node.innerText || '').trim())
        );
        return {
            loading,
            empty,
            headers: Array.from(table.querySelectorAll(
                'th[data-testid="beast-core-table-th"]'
            )).map(cell => (cell.textContent || '').trim()),
            rows: Array.from(table.querySelectorAll(
                'tr[data-testid="beast-core-table-body-tr"]'
            )).filter(visible).map(row => Array.from(row.querySelectorAll(
                'td[data-testid="beast-core-table-td"]'
            )).map(cell => (cell.innerText || cell.textContent || '').trim()))
        };
    }
"""


def extract_aftersale_result_rows(
    page: Page,
    *,
    previous_rows: list[dict[str, str]] | None = None,
    timeout_ms: int = _RESULT_TABLE_TIMEOUT_MS,
) -> list[dict[str, str]]:
    """等待完整数据或明确空状态，翻页时拒绝上一页数据及加载中的快照。"""
    deadline = time.monotonic() + timeout_ms / 1000
    table = page.locator('[data-testid="beast-core-table"]').first
    wait_for_content_or_login(page, table, timeout=timeout_ms)
    payload: dict = {}
    missing = list(_RESULT_COLUMNS)
    while True:
        raise_if_login_page(page)
        try:
            payload = table.evaluate(_RESULT_TABLE_SNAPSHOT, timeout=1000)
        except PlaywrightTimeoutError:
            raise_if_login_page(page)
            payload = {}
        headers = [str(value).strip() for value in payload.get("headers", [])]
        missing = [name for name in _RESULT_COLUMNS if name not in headers]
        if payload and not payload.get("loading"):
            # 空列表可能没有“审核状态”列，但必须有明确空提示和其余业务表头。
            if (
                payload.get("empty")
                and all(name in headers for name in _RESULT_COLUMNS[:-1])
                and not any(_ORDER_RE.search(" ".join(cells)) for cells in payload.get("rows", []))
            ):
                return []
            if not missing:
                rows = _parse_aftersale_result_payload(payload)
                if (
                    rows
                    and len(rows) == len(payload.get("rows", []))
                    and all(row["platform_status"] for row in rows)
                    and rows != previous_rows
                ):
                    return rows
        remaining_ms = int((deadline - time.monotonic()) * 1000)
        if remaining_ms <= 0:
            break
        page.wait_for_timeout(min(250, remaining_ms))

    raise_if_login_page(page)
    if payload.get("loading"):
        reason = "页面仍显示加载中"
    elif missing:
        reason = f"售后申诉结果表缺少字段：{'、'.join(missing)}"
    elif previous_rows is not None and _parse_aftersale_result_payload(payload) == previous_rows:
        reason = "翻页后仍显示上一页数据"
    else:
        reason = "未出现完整订单数据或明确的无记录提示"
    raise RuntimeError(
        f"售后申诉结果列表等待 {timeout_ms / 1000:g} 秒后未就绪：{reason}。"
        "请稍后重试，若反复出现请检查页面或网络。"
    )


def _parse_aftersale_result_payload(payload: dict) -> list[dict[str, str]]:
    """按表头文字映射已就绪的快照，避免列顺序调整造成错读。"""
    headers = [str(value).strip() for value in payload.get("headers", [])]
    indices = {name: headers.index(name) for name in _RESULT_COLUMNS}

    rows: list[dict[str, str]] = []
    for raw_cells in payload.get("rows", []):
        cells = [str(value).strip() for value in raw_cells]
        if len(cells) < len(headers):
            continue
        order_match = _ORDER_RE.search(cells[indices["订单编号"]])
        if order_match is None:
            continue
        status_cell = cells[indices["审核状态"]]
        # 审核状态单元格可能同时包含“申诉优赔特权”或审核失败原因。
        # 结果回查只保存标准状态，避免把附加文案误判成未知状态。
        platform_status = next(
            (status for status in RESULT_STATUSES if status in status_cell),
            status_cell,
        )
        rows.append({
            "order_no": order_match.group(0),
            "appeal_type": cells[indices["申诉类型"]],
            "platform_appealed_at": cells[indices["申诉时间"]],
            "appeal_item": cells[indices["申诉项"]],
            "appeal_amount": cells[indices["申诉金额(元)"]],
            "platform_status": platform_status,
        })
    return rows


def _amount(value: object) -> Decimal | None:
    match = re.search(r"\d+(?:\.\d{1,2})?", str(value or ""))
    if match is None:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def parse_aftersale_return_amount(text: str) -> str:
    """只接受明确标为返还金额的数值，不把申请金额或订单金额当作到账金额。"""
    amounts = {
        Decimal(value.replace(",", ""))
        for value in _RETURN_AMOUNT_RE.findall(text)
    }
    if len(amounts) != 1:
        raise ValueError("售后申诉详情未显示唯一的返还金额")
    return format(amounts.pop(), ".2f")


def read_partial_aftersale_return_amount(page: Page, row: dict[str, str]) -> str:
    """精确定位申诉行，读取其详情新标签页中的返还金额并关闭该页。"""
    table = page.locator('[data-testid="beast-core-table"]').first
    target = table.locator('tr[data-testid="beast-core-table-body-tr"]')
    for field in (
        "order_no", "appeal_type", "platform_appealed_at", "appeal_item",
        "appeal_amount", "platform_status",
    ):
        target = target.filter(has=page.get_by_text(row[field], exact=True))
    if target.count() != 1:
        raise RuntimeError(f"订单 {row['order_no']} 的售后申诉详情行无法唯一定位")
    link = target.get_by_text("详情", exact=True)
    if link.count() != 1:
        raise RuntimeError(f"订单 {row['order_no']} 的详情入口数量异常")

    detail_page = None
    try:
        with page.expect_popup(timeout=20_000) as popup:
            safe_click(page, link, "部分通过申诉详情")
        detail_page = popup.value
        detail_page.wait_for_load_state("domcontentloaded", timeout=30_000)
        raise_if_login_page(detail_page)
        clear_blockers(detail_page)
        card = detail_page.locator(
            '[class*="Aftersale_appealDetailCard"]'
        ).filter(has_text="部分通过").filter(has_text=_RETURN_AMOUNT_RE)
        wait_for_content_or_login(detail_page, card)
        return parse_aftersale_return_amount(card.inner_text())
    finally:
        if detail_page is not None:
            try:
                detail_page.close()
            except PlaywrightError as exc:
                print(f"关闭售后申诉详情标签页失败：{exc}")


def _platform_time(value: object) -> datetime | None:
    try:
        return datetime.strptime(str(value).strip(), "%Y-%m-%d %H:%M").replace(
            tzinfo=_SHANGHAI_TZ
        )
    except ValueError:
        return None


def _local_time(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_SHANGHAI_TZ)
    return parsed.astimezone(_SHANGHAI_TZ)


def _platform_record_key(row: dict[str, str]) -> str:
    """使用列表原始字段生成同步记录键；返还金额变化不影响重复查询。"""
    identity = [row.get(field, "").strip() for field in (
        "order_no", "appeal_type", "platform_appealed_at", "appeal_item", "appeal_amount",
    )]
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return f"result_{digest}"


def match_aftersale_result_record(
    row: dict[str, str],
    candidates: list[dict],
    used_keys: set[tuple[str, str]],
) -> tuple[dict | None, str | None]:
    """订单号优先，再以申诉项、金额和提交时间唯一匹配。"""
    available = [
        record for record in candidates
        if (record["order_no"], record["record_key"]) not in used_keys
        and (
            not str(record.get("record_key") or "").startswith("result_")
            or record["record_key"] == _platform_record_key(row)
        )
        and (
            not record.get("platform_appealed_at")
            or record["platform_appealed_at"] == row.get("platform_appealed_at")
        )
        and (
            not record.get("appeal_item")
            or str(record["appeal_item"]).strip() == row.get("appeal_item", "").strip()
        )
    ]
    if not available:
        return None, "本地没有尚未匹配的售后申诉记录"

    item = row.get("appeal_item", "").strip()
    item_matches = [
        record for record in available
        if str(record.get("appeal_item") or "").strip() == item
    ]
    if item_matches:
        available = item_matches
    if len(available) == 1:
        return available[0], None

    # 已回查记录的金额可能已替换为实际返还金额，优先使用平台提交时间。
    platform_time = row.get("platform_appealed_at", "").strip()
    if platform_time:
        time_matches = [
            record for record in available
            if str(record.get("platform_appealed_at") or "").strip() == platform_time
        ]
        if len(time_matches) == 1:
            return time_matches[0], None
        if time_matches:
            available = time_matches

    platform_amount = _amount(row.get("appeal_amount"))
    if platform_amount is not None:
        amount_matches = [
            record for record in available
            if _amount(record.get("appeal_amount")) == platform_amount
        ]
        if amount_matches:
            available = amount_matches
    if len(available) == 1:
        return available[0], None

    appeared_at = _platform_time(row.get("platform_appealed_at"))
    if appeared_at is not None:
        timed: list[tuple[float, dict]] = []
        for record in available:
            submitted_at = _local_time(record.get("submitted_at"))
            if submitted_at is not None:
                timed.append((abs((submitted_at - appeared_at).total_seconds()), record))
        timed.sort(key=lambda item: item[0])
        if (
            timed
            and timed[0][0] <= _TIME_TOLERANCE_SECONDS
            and (len(timed) == 1 or timed[0][0] < timed[1][0])
        ):
            return timed[0][1], None

    keys = "、".join(str(record.get("record_key") or "") for record in available)
    return None, f"同一订单存在多条售后申诉且无法唯一匹配：{keys}"


def _next_disabled(locator: Locator) -> bool:
    classes = (locator.get_attribute("class") or "").lower()
    aria = (locator.get_attribute("aria-disabled") or "").lower()
    return "disabled" in classes or aria == "true"


def prepare_result_query(shop: str, submitted_since: str | None = None) -> tuple[list[dict], str]:
    submitted_since = result_query_settings.normalize_since(submitted_since)
    all_records = db.list_aftersale_appeal_records(shop)
    local_records = [
        record for record in all_records
        if record.get("status") in TERMINAL_STATUSES
        or (
            record.get("status") in WAITING_STATUSES
            and (
                record.get("status") == "已提交"
                or record.get("submitted_at")
                or record.get("result_text")
            )
        )
    ]
    targets = result_query_settings.filter_records(
        local_records, submitted_since, "submitted_at", "platform_appealed_at",
    )
    reason = result_query_settings.empty_query_reason(len(local_records), submitted_since, "售后申诉") if not targets else ""
    return targets, reason


def check_aftersale_appeal_results(
    page: Page,
    shop: str,
    on_progress: Callable[[int, int, int, int], None] | None = None,
    max_pages: int = 100,
    query_interval_seconds: int = DEFAULT_QUERY_INTERVAL_SECONDS,
    submitted_since: str | None = None,
    target_orders: list[dict] | None = None,
) -> dict:
    submitted_since = result_query_settings.normalize_since(submitted_since)
    query_interval_seconds = validate_query_interval(query_interval_seconds)
    if target_orders is None:
        local_records, empty_reason = prepare_result_query(shop, submitted_since)
    else:
        local_records, empty_reason = target_orders, "本批次没有待回查的售后申诉记录。"
    if not local_records:
        return {
            "updates": [],
            "checked_count": 0,
            "matched_count": 0,
            "imported_count": 0,
            "ignored_platform_count": 0,
            "unmatched_local_count": 0,
            "ambiguous_count": 0,
            "truncated": False,
            "skipped": True, "complete": True, "message": empty_reason,
        }
    page.goto(AFTERSALE_RESULT_URL, wait_until="domcontentloaded", timeout=60_000)
    raise_if_login_page(page)
    clear_blockers(page)
    try:
        wait_for_content_or_login(
            page, page.locator('[data-testid="beast-core-table"]').first
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError(
            "未能加载售后申诉结果列表，请检查店铺登录状态或页面是否异常。"
            f"当前页面：{page.url}"
        ) from exc
    by_order: dict[str, list[dict]] = defaultdict(list)
    for record in local_records:
        by_order[str(record["order_no"])].append(record)

    used: set[tuple[str, str]] = set()
    seen_platform_keys: set[str] = set()
    local_keys = {(r["order_no"], r["record_key"]) for r in local_records}
    updates: list[dict] = []
    checked = matched = ambiguous = ignored_platform = 0
    truncated = False
    previous_rows = None
    visited_pages: set[str] = set()

    for page_number in result_query_settings.page_numbers(max_pages, submitted_since):
        rows = extract_aftersale_result_rows(page, previous_rows=previous_rows)
        if submitted_since is not None and rows:
            signature = json.dumps(rows, ensure_ascii=False, sort_keys=True)
            if signature in visited_pages:
                raise RuntimeError("售后结果分页出现重复页面，查询未完成，请核对未查记录。")
            visited_pages.add(signature)
        checked += len(rows)
        for row in rows:
            if result_query_settings.platform_before_since(row["platform_appealed_at"], submitted_since):
                continue
            platform_key = _platform_record_key(row)
            if platform_key in seen_platform_keys:
                continue
            seen_platform_keys.add(platform_key)
            candidates = by_order.get(row["order_no"], [])
            record, reason = match_aftersale_result_record(row, candidates, used)
            if record is None:
                if reason and "多条" in reason:
                    ambiguous += 1
                else:
                    # 结果查询只回写本工具已经记录并提交过的售后申诉；
                    # 平台存在而本地不存在的记录不自动补录。
                    ignored_platform += 1
                continue
            matched += 1
            if submitted_since is not None and not result_query_settings.same_submission(
                record, row["platform_appealed_at"], "submitted_at", _TIME_TOLERANCE_SECONDS,
            ):
                matched -= 1
                ambiguous += 1
                continue

            raw_status = row["platform_status"].strip()
            detail = None
            status = raw_status
            appeal_amount = row["appeal_amount"]
            if raw_status == "部分通过":
                # 列表金额仅用于匹配记录；缺少详情金额时绝不回退到申请金额。
                appeal_amount = ""
                page.wait_for_timeout(query_interval_seconds * 1000)
                try:
                    appeal_amount = read_partial_aftersale_return_amount(page, row)
                except LoginRequiredError:
                    raise
                except (PlaywrightError, RuntimeError, ValueError) as exc:
                    detail = f"部分通过：详情返还金额读取失败，金额待下次查询补全：{exc}"
                    print(f"[售后结果][{row['order_no']}] {detail}")
            if raw_status not in PLATFORM_STATUSES:
                status = "结果待确认"
                detail = f"平台出现未知售后申诉审核状态：{raw_status or '空白'}"
                shot = save_error_screenshot(
                    page, f"aftersale_unknown_result_{row['order_no']}"
                )
                if shot is not None:
                    detail += f"（现场截图：{to_relative_evidence_path(shot)}）"
            db.record_aftersale_appeal_result(
                shop,
                record["order_no"],
                record["record_key"],
                status,
                result_text=raw_status,
                platform_appealed_at=row["platform_appealed_at"],
                appeal_item=row["appeal_item"],
                appeal_amount=appeal_amount,
                detail=detail,
            )
            used.add((record["order_no"], record["record_key"]))
            updates.append({
                "order_no": record["order_no"],
                "record_key": record["record_key"],
                "status": status,
                "appeal_amount": appeal_amount,
                "platform_appealed_at": row["platform_appealed_at"],
                "detail": detail,
            })

        if on_progress:
            on_progress(page_number, checked, matched, len(updates))
        if submitted_since is not None and used == local_keys:
            print("[售后结果] 设定提交时间范围内的目标记录已全部查齐，停止翻页。")
            break
        if submitted_since is not None and not rows:
            break
        next_button = page.locator('[data-testid="beast-core-pagination-next"]')
        if next_button.count() == 0 or _next_disabled(next_button.first):
            break
        previous_rows = rows
        page.wait_for_timeout(query_interval_seconds * 1000)
        safe_click(page, next_button.first, "售后申诉结果下一页")
        jitter_wait(page, 650, 0.35)

    if local_keys - used:
        print(f"[售后结果] 平台列表查询结束，仍有 {len(local_keys - used)} 条目标记录未匹配，请核对页面筛选范围。")
    return {
        "updates": updates,
        "checked_count": checked,
        "matched_count": matched,
        # 保留旧字段兼容现有 JSON 消费方；查询模式已不再自动补录。
        "imported_count": 0,
        "ignored_platform_count": ignored_platform,
        "unmatched_local_count": len(local_keys - used),
        "ambiguous_count": ambiguous,
        "truncated": truncated,
        "complete": not (local_keys - used) and not truncated,
        "message": (
            f"已查询 {max_pages} 页，达到页数上限，平台仍有后续记录未查询。"
            if truncated else (
                ("平台售后申诉列表暂无记录。" if checked == 0 else "")
                + f"还有 {len(local_keys - used)} 条目标售后申诉未匹配，查询未完全覆盖，请核对平台筛选范围。"
                if local_keys - used
                else ("平台售后申诉列表暂无记录。" if checked == 0 else "")
            )
        ),
    }
