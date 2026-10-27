"""批量查询消费者负向体验复议记录，并安全回写独立复议状态。"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import db
import result_query_settings
from blockers import clear_blockers, safe_click
from login import raise_if_login_page, wait_for_content_or_login
from stealth import jitter_wait

RECONSIDERATION_RECORD_URL = (
    "https://mms.pinduoduo.com/orders/appeals/"
    "negativeExperienceReconsiderRecord?status=0&"
    "reconsiderSecondaryTabKey=negativeExperience"
)

PLATFORM_RESULT_STATUSES = {"待处理", "审核中", "申诉成功", "申诉失败"}
TERMINAL_RESULT_STATUSES = {"申诉成功", "申诉失败"}
CHECKABLE_LOCAL_STATUSES = [
    "已提交复议",
    "复议提交确认中",
    "复议结果待确认",
    "待处理",
    "审核中",
]

_ORDER_RE = re.compile(r"\d{6}-\d+")
_REFUND_RE = re.compile(r"返还金额\s*[:：]\s*(\d+(?:\.\d{1,2})?)")
_SHANGHAI_TZ = timezone(timedelta(hours=8))
_TIME_MATCH_TOLERANCE_SECONDS = 10 * 60


def resolve_platform_result(result_cell_text: str) -> tuple[str, str, str | None]:
    """返回 (数据库状态, 平台原始状态, 未识别详情)。"""
    lines = [line.strip() for line in result_cell_text.splitlines() if line.strip()]
    matches = [status for status in PLATFORM_RESULT_STATUSES if status in lines]
    if len(matches) != 1:
        matches = [
            status for status in PLATFORM_RESULT_STATUSES
            if status in result_cell_text
        ]
    if len(matches) == 1:
        return matches[0], matches[0], None
    raw = result_cell_text.strip()
    return "复议结果待确认", raw, f"平台出现未知复议结果：{raw or '空白'}"


def extract_reconsideration_rows_from_current_page(
    page: Page, *, previous_rows: list[dict[str, str]] | None = None,
    timeout_ms: int = 45_000,
) -> list[dict[str, str]]:
    """不能把表格外框已出现当作数据已加载；等待业务行或明确空态。"""
    deadline = time.monotonic() + timeout_ms / 1000
    while True:
        raise_if_login_page(page)
        payload = _read_reconsideration_table(page)
        if not payload.get("loading"):
            rows = _parse_reconsideration_payload(payload)
            if rows and rows != previous_rows:
                return rows
            if payload.get("empty") and not payload.get("rows"):
                return []
        remaining = int((deadline - time.monotonic()) * 1000)
        if remaining <= 0:
            raise RuntimeError(
                f"复议结果列表等待 {timeout_ms / 1000:g} 秒后仍未就绪："
                "页面仍在加载、数据不完整或翻页后仍是上一页，请稍后重试。"
            )
        page.wait_for_timeout(min(250, remaining))


def _read_reconsideration_table(page: Page) -> dict:
    """按表头文字映射列，避免前端调整列顺序后读错状态。"""
    table = page.locator('[data-testid="beast-core-table"]').first
    table.wait_for(state="visible", timeout=20_000)
    payload = table.evaluate(
        """
        (table) => {
            const headers = Array.from(
                table.querySelectorAll(
                    'th[data-testid="beast-core-table-th"]'
                )
            ).map((cell) => (cell.textContent || '').trim());
            const rows = Array.from(
                table.querySelectorAll(
                    'tr[data-testid="beast-core-table-body-tr"]'
                )
            ).map((row) => Array.from(
                row.querySelectorAll('td[data-testid="beast-core-table-td"]')
            ).map((cell) => (cell.innerText || cell.textContent || '').trim()));
            const visible = node => {
                const s = getComputedStyle(node), r = node.getBoundingClientRect();
                return s.display !== 'none' && s.visibility !== 'hidden'
                    && Number(s.opacity || 1) !== 0 && r.width > 0 && r.height > 0;
            };
            const scope = table.closest('[data-testid="beast-core-spin"]')
                || table.parentElement || table;
            const loading = Array.from(scope.querySelectorAll(
                '[data-testid="beast-core-spin-mask"], [aria-busy="true"], '
                + '[data-testid*="loading"], [class*="spinning"], [class*="Loading"]'
            )).some(visible) || scope.getAttribute('aria-busy') === 'true';
            const empty = Array.from(table.querySelectorAll('div, span, td')).some(
                node => visible(node) && ['暂无数据', '暂无记录', '暂无复议记录',
                    '没有符合条件的订单'].includes((node.innerText || '').trim())
            );
            return {headers, rows, loading, empty};
        }
        """
    )
    return payload


def _parse_reconsideration_payload(payload: dict) -> list[dict[str, str]]:
    headers = [str(value).strip() for value in payload.get("headers", [])]
    required = ["订单编号", "问题类型", "复议金额(元)", "复议时间", "复议结果"]
    missing = [header for header in required if header not in headers]
    if missing:
        return []
    indices = {header: headers.index(header) for header in required}

    extracted: list[dict[str, str]] = []
    for cells in payload.get("rows", []):
        values = [str(value).strip() for value in cells]
        if len(values) < len(headers):
            continue
        order_match = _ORDER_RE.search(values[indices["订单编号"]])
        if order_match is None:
            continue
        result_cell = values[indices["复议结果"]]
        refund_match = _REFUND_RE.search(result_cell)
        extracted.append(
            {
                "order_no": order_match.group(0),
                "issue_type": values[indices["问题类型"]],
                "reconsider_amount": values[indices["复议金额(元)"]],
                "reconsidered_at": values[indices["复议时间"]],
                "result_cell_text": result_cell,
                "refund_amount": refund_match.group(1) if refund_match else "",
            }
        )
    # Partial rows must not be mistaken for a finished page.
    return extracted if len(extracted) == len(payload.get("rows", [])) else []


def _normalized_amount(value: object) -> Decimal | None:
    match = re.search(r"\d+(?:\.\d{1,2})?", str(value or ""))
    if match is None:
        return None
    try:
        return Decimal(match.group(0))
    except InvalidOperation:
        return None


def _parse_platform_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=_SHANGHAI_TZ
        )
    except ValueError:
        return None


def _parse_local_submitted_at(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=_SHANGHAI_TZ)
    return parsed.astimezone(_SHANGHAI_TZ)


def match_local_reconsideration_record(
    row: dict[str, str],
    candidates: list[dict],
    used_keys: set[tuple[str, str]],
) -> tuple[dict | None, str | None]:
    """订单号优先；重复记录再按问题、金额和复议时间唯一匹配。"""
    available = [
        record for record in candidates
        if (record["order_no"], record["record_key"]) not in used_keys
    ]
    if not available:
        return None, "本地没有尚未匹配的待查询复议记录"
    if len(available) == 1:
        return available[0], None

    issue_type = row.get("issue_type", "").strip()
    issue_matches = [
        record for record in available
        if str(record.get("issue_type") or "").strip() == issue_type
    ]
    if issue_matches:
        available = issue_matches
    if len(available) == 1:
        return available[0], None

    platform_amount = _normalized_amount(row.get("reconsider_amount"))
    if platform_amount is not None:
        amount_matches = [
            record for record in available
            if _normalized_amount(record.get("penalty_amount")) == platform_amount
        ]
        if amount_matches:
            available = amount_matches
    if len(available) == 1:
        return available[0], None

    platform_time = _parse_platform_time(row.get("reconsidered_at", ""))
    if platform_time is not None:
        timed: list[tuple[float, dict]] = []
        for record in available:
            submitted_at = _parse_local_submitted_at(record.get("submitted_at"))
            if submitted_at is not None:
                timed.append(
                    (abs((submitted_at - platform_time).total_seconds()), record)
                )
        timed.sort(key=lambda item: item[0])
        if (
            timed
            and timed[0][0] <= _TIME_MATCH_TOLERANCE_SECONDS
            and (len(timed) == 1 or timed[0][0] < timed[1][0])
        ):
            return timed[0][1], None

    keys = "、".join(str(record.get("record_key") or "") for record in available)
    return None, f"同一订单存在多条复议记录且无法唯一匹配：{keys}"


def _page_signature(rows: list[dict[str, str]]) -> str:
    return "|".join(
        f"{row['order_no']}#{row['reconsidered_at']}#{row['result_cell_text']}"
        for row in rows
    )


def _next_page_is_disabled(next_button: Locator) -> bool:
    classes = next_button.get_attribute("class") or ""
    aria_disabled = (next_button.get_attribute("aria-disabled") or "").lower()
    return "disabled" in classes.lower() or aria_disabled == "true"


def _wait_for_page_changed(page: Page, previous_signature: str) -> None:
    try:
        page.wait_for_function(
            """
            (previous) => {
                const body = document.querySelector(
                    'tbody[data-testid="beast-core-table-middle-tbody"]'
                );
                const current = (body?.innerText || body?.textContent || '').trim();
                return current && current !== previous;
            }
            """,
            arg=previous_signature,
            timeout=15_000,
        ).dispose()
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("点击下一页后复议记录表格没有更新") from exc


def prepare_result_query(shop: str, submitted_since: str | None = None) -> tuple[list[dict], str]:
    submitted_since = result_query_settings.normalize_since(submitted_since)
    pending = db.list_reconsideration_records(shop, statuses=CHECKABLE_LOCAL_STATUSES)
    targets = result_query_settings.filter_records(
        pending, submitted_since, "submitted_at", "platform_reconsidered_at",
    )
    reason = result_query_settings.empty_query_reason(len(pending), submitted_since, "复议") if not targets else ""
    return targets, reason


def check_reconsideration_results(
    page: Page,
    shop: str,
    on_progress: Callable[[int, int, int, int], None] | None = None,
    max_pages: int = 100,
    submitted_since: str | None = None,
    target_orders: list[dict] | None = None,
) -> dict:
    submitted_since = result_query_settings.normalize_since(submitted_since)
    if target_orders is None:
        local_records, empty_reason = prepare_result_query(shop, submitted_since)
    else:
        local_records, empty_reason = target_orders, "本批次没有待回查的复议记录。"
    if not local_records:
        return {
            "updates": [], "checked_count": 0, "matched_count": 0,
            "unmatched_local_count": 0, "ambiguous_count": 0,
            "skipped": True, "complete": True, "message": empty_reason,
        }
    by_order: dict[str, list[dict]] = defaultdict(list)
    for record in local_records:
        by_order[record["order_no"]].append(record)

    page.goto(RECONSIDERATION_RECORD_URL, wait_until="domcontentloaded")
    raise_if_login_page(page)
    clear_blockers(page)
    wait_for_content_or_login(
        page, page.locator('[data-testid="beast-core-table"]').first,
    )

    checked_count = 0
    matched_count = 0
    ambiguous_count = 0
    updates: list[dict] = []
    used_keys: set[tuple[str, str]] = set()
    visited_pages: set[str] = set()
    previous_rows = None

    for page_no in result_query_settings.page_numbers(max_pages, submitted_since):
        rows = extract_reconsideration_rows_from_current_page(page, previous_rows=previous_rows)
        signature = _page_signature(rows)
        if signature in visited_pages:
            raise RuntimeError("复议记录分页出现重复页面，已停止以避免死循环")
        visited_pages.add(signature)
        checked_count += len(rows)

        for row in rows:
            if result_query_settings.platform_before_since(row["reconsidered_at"], submitted_since):
                continue
            candidates = by_order.get(row["order_no"], [])
            if not candidates:
                continue
            record, reason = match_local_reconsideration_record(
                row, candidates, used_keys
            )
            if record is None:
                ambiguous_count += 1
                print(f"订单 {row['order_no']} 未更新：{reason}")
                continue
            if submitted_since is not None and not result_query_settings.same_submission(
                record, row["reconsidered_at"], "submitted_at", _TIME_MATCH_TOLERANCE_SECONDS,
            ):
                ambiguous_count += 1
                continue
            status, raw_result, detail = resolve_platform_result(
                row["result_cell_text"]
            )
            db.record_reconsideration_review_result(
                shop,
                row["order_no"],
                record["record_key"],
                status,
                result_text=raw_result,
                platform_reconsidered_at=row["reconsidered_at"],
                refund_amount=row["refund_amount"],
                detail=detail,
            )
            used_keys.add((row["order_no"], record["record_key"]))
            matched_count += 1
            updates.append(
                {
                    "order_no": row["order_no"],
                    "record_key": record["record_key"],
                    "status": status,
                    "result_text": raw_result,
                    "refund_amount": row["refund_amount"],
                }
            )

        if on_progress is not None:
            on_progress(page_no, checked_count, matched_count, len(updates))

        if submitted_since is not None and len(used_keys) == len(local_records):
            print("[复议结果] 设定提交时间范围内的目标记录已全部查齐，停止翻页。")
            break

        next_button = page.locator(
            '[data-testid="beast-core-pagination-next"]'
        ).first
        if next_button.count() == 0 or _next_page_is_disabled(next_button):
            break
        previous_rows = rows
        safe_click(page, next_button, "复议记录下一页")

    unmatched_local_count = len(local_records) - len(used_keys)
    if unmatched_local_count:
        print(f"[复议结果] 平台列表查询结束，仍有 {unmatched_local_count} 条目标记录未匹配，请核对页面筛选范围。")
    return {
        "updates": updates,
        "checked_count": checked_count,
        "matched_count": matched_count,
        "unmatched_local_count": unmatched_local_count,
        "ambiguous_count": ambiguous_count,
        "complete": not unmatched_local_count,
        "message": f"还有 {unmatched_local_count} 条目标复议未匹配，查询未完全覆盖，请核对平台筛选范围。" if unmatched_local_count else "",
    }
