"""售后申诉：退款申请单取证、聊天分析与真实提交。"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import BrowserContext, Locator, Page
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import db
from appeal_classifier import (
    NO_CHAT_RECORD_LABEL,
    classify_appeal_evidence,
    compact_chat_json,
    get_client,
)
from appeal_submit import wait_for_submit_captcha
from chat_evidence import select_chat_evidence
from appeal_wording import AFTERSALE_DESCRIPTION_PROMPT
from appeal_style import diverse_completion
from blockers import clear_blockers, safe_click, stealth_safe_click
from paths import BASE_DIR, DEBUG_DIR, EVIDENCE_DIR, SHOP_ID
from pdd_chat import (
    build_chat_record_dict,
    click_next_page,
    query_chat_records,
)
from run_appeal_batch import open_chat_page
from screenshots import (
    safe_filename,
    save_error_screenshot,
    save_natural_content_screenshot,
    to_relative_evidence_path,
)
from stealth import (
    human_click, human_delay, human_type, paste_text, jitter_wait,
    business_pause, business_read_page, paced_business_operations,
)

AFTERSALE_APPEAL_URL = (
    "https://mms.pinduoduo.com/orders/appeals/aftersale/order"
    "?msfrom=mms_sidenav&activeIndex=0"
)
AFTERSALE_DETAIL_FRAGMENT = "/aftersales-ssr/detail"
_UNAVAILABLE_APPEAL_RESULT = "跳过：发起申诉按钮不可点击"
GOODS_APPEAL_ITEM = "货款申诉"
FREIGHT_APPEAL_ITEM = "运费申诉"
GOODS_PREFERRED_REASON = "消费者反馈商品存在质量问题，但凭证不足"
GOODS_FALLBACK_REASON = "非以上申诉原因"
FREIGHT_REASON_PARENT = "其他"
FREIGHT_REASON = "非以上申诉原因"
SUPPORTED_APPEAL_ITEMS = {GOODS_APPEAL_ITEM, FREIGHT_APPEAL_ITEM}
DISPUTE_APPEAL_ITEM = "纠纷退款率申诉"
AFTERSALE_APPEAL_TITLES = ("维权申诉", "极速退款申诉")
_APPEAL_TITLE_RE = re.compile("|".join(re.escape(title) for title in AFTERSALE_APPEAL_TITLES))
_APPEAL_BUTTON_RE = re.compile(f"^(?:{_APPEAL_TITLE_RE.pattern})$")


def _appeal_items(value: str) -> frozenset[str]:
    return frozenset(part for part in re.split(r"[、,，\s]+", value.strip()) if part)


def _is_combined_appeal(value: str) -> bool:
    items = _appeal_items(value)
    return len(items) >= 2 and items <= {
        GOODS_APPEAL_ITEM, FREIGHT_APPEAL_ITEM, DISPUTE_APPEAL_ITEM,
    }


def _supported_appeal_item(value: str) -> bool:
    return value in SUPPORTED_APPEAL_ITEMS or _is_combined_appeal(value)

AFTERSALE_EMPTY_TEXTS = (
    "暂无数据",
    "暂无订单",
    "暂无相关订单",
    "暂无售后申诉订单",
    "没有符合条件的订单",
)

_ORDER_RE = re.compile(r"\d{6}-\d+")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d{1,2})?")
_TIME_RE = re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}")


class AftersaleAppealStopped(Exception):
    """用户请求在当前安全边界停止。"""


def _raise_if_stopped(
    should_stop: Callable[[], bool] | None,
    stage: str,
) -> None:
    if should_stop is not None and should_stop():
        raise AftersaleAppealStopped(stage)


def _clean_text(locator: Locator) -> str:
    try:
        return re.sub(r"\s+", " ", locator.inner_text()).strip()
    except Exception:  # noqa: BLE001 - 页面被切走/销毁时按空文本处理
        return ""


def make_record_key(order_no: str, refund_time: str, appeal_item: str) -> str:
    """同一订单可能有多个售后单，退款时间和申诉项共同区分。"""
    return (
        f"{refund_time or '未知退款时间'}|"
        f"{appeal_item or '未知申诉项'}|{order_no}"
    )


def open_aftersale_appeal_page(page: Page) -> None:
    """进入售后申诉列表并清理可能遮挡列表的提示弹窗。"""
    navigated = "/orders/appeals/aftersale/order" not in page.url
    if navigated:
        page.goto(AFTERSALE_APPEAL_URL, wait_until="domcontentloaded", timeout=60_000)
    clear_blockers(page)
    page.locator('#mf-mms-orders-container [data-testid="beast-core-table"]').wait_for(
        state="visible", timeout=30_000
    )
    if navigated:
        business_read_page(page)


def wait_for_aftersale_list_ready(
    page: Page,
    *,
    attempt_timeout_ms: int = 20_000,
    attempts: int = 2,
    empty_stable_ms: int = 3_000,
) -> str:
    """等待有效订单行或平台明确空状态，避免表格外框先出现造成误判。

    返回 ``"rows"`` 或 ``"empty"``。无加载遮罩且空提示连续稳定至少三秒
    才确认空列表；期间出现加载、提示消失或表格替换，重新计时。
    未能确认有效订单或稳定空状态时，有限重试后保存现场并报错。
    """
    if attempts <= 0:
        raise ValueError("售后申诉列表等待次数必须为正整数。")
    if empty_stable_ms <= 0:
        raise ValueError("售后申诉列表空状态确认时间必须为正数。")

    script = r"""
        ({ emptyTexts, emptyStableMs }) => {
            let emptySince = null;
            let emptyTable = null;
            let emptyText = '';
            const resetEmpty = () => { emptySince = null; };
            return () => {
            const visible = (node) => {
                if (!node) return false;
                const style = getComputedStyle(node);
                const rect = node.getBoundingClientRect();
                return style.display !== 'none'
                    && style.visibility !== 'hidden'
                    && Number(style.opacity || 1) !== 0
                    && rect.width > 0
                    && rect.height > 0;
            };

            const root = document.querySelector('#mf-mms-orders-container');
            if (!root) { resetEmpty(); return false; }
            const loadingSelectors = [
                '[data-testid*="loading"]',
                '[class*="loading"]',
                '[class*="Loading"]',
                '[data-testid="beast-core-spin-mask"]',
                '[class*="spin-mask"]',
                '[class*="Spin-mask"]',
                '[aria-busy="true"]',
                '[role="progressbar"]'
            ].join(',');
            const loadingRoot = root.closest('[data-testid="beast-core-spin"]') || root;
            const loading = (loadingRoot.matches(loadingSelectors) && visible(loadingRoot))
                || Array.from(loadingRoot.querySelectorAll(loadingSelectors)).some(visible);
            if (loading) { resetEmpty(); return false; }
            const rows = Array.from(root.querySelectorAll(
                'tr[data-testid="beast-core-table-body-tr"]'
            )).filter(visible);
            const hasValidRow = rows.some((row) => {
                const cells = row.querySelectorAll(
                    'td[data-testid="beast-core-table-td"]'
                );
                if (cells.length < 9) return false;
                const orderText = cells[0]?.textContent || '';
                const hasOrder = /\d{6}-\d+/.test(orderText);
                const hasDetail = Array.from(
                    row.querySelectorAll('a, button, span, div')
                ).some((node) => (node.textContent || '').trim() === '售后详情');
                return hasOrder && hasDetail;
            });
            if (hasValidRow) { resetEmpty(); return 'rows'; }

            const table = root.querySelector('[data-testid="beast-core-table"]');
            const scope = table?.parentElement || table;
            const text = (scope?.innerText || '').replace(/\s+/g, ' ').trim();
            if (visible(table) && emptyTexts.some((marker) => text.includes(marker))) {
                const now = performance.now();
                if (emptySince === null || emptyTable !== table || emptyText !== text) {
                    emptySince = now;
                    emptyTable = table;
                    emptyText = text;
                }
                return now - emptySince >= emptyStableMs ? 'empty' : false;
            }
            resetEmpty();
            return false;
            };
        }
    """

    for attempt in range(1, attempts + 1):
        probe = page.evaluate_handle(
            script, {"emptyTexts": list(AFTERSALE_EMPTY_TEXTS), "emptyStableMs": empty_stable_ms},
        )
        try:
            handle = page.wait_for_function(
                "probe => probe()",
                arg=probe,
                timeout=attempt_timeout_ms,
            )
            try:
                state = str(handle.json_value())
            finally:
                handle.dispose()
            if state in {"rows", "empty"}:
                if state == "rows":
                    jitter_wait(page, 500, 0.15)
                return state
        except PlaywrightTimeoutError:
            print(
                f"售后申诉列表第 {attempt}/{attempts} 次等待未看到有效订单行"
                "或持续稳定的空状态。"
            )
        finally:
            probe.dispose()
        clear_blockers(page)
        if attempt < attempts:
            jitter_wait(page, 1_500, 0.25)

    shot = save_error_screenshot(page, f"aftersale_list_not_ready_{SHOP_ID or 'default'}")
    suffix = (
        f"，现场截图：{to_relative_evidence_path(shot)}"
        if shot is not None
        else ""
    )
    raise RuntimeError(
        "售后申诉列表在限定时间内未加载出有效订单，也未确认持续稳定的空状态，"
        "为避免漏单已停止" + suffix
    )


def extract_aftersale_rows(page: Page) -> list[dict[str, str]]:
    """读取列表要求的全部业务字段，避免把商品标题数字误当成订单号。"""
    rows = page.locator('#mf-mms-orders-container tr[data-testid="beast-core-table-body-tr"]')
    result: list[dict[str, str]] = []
    for index in range(rows.count()):
        row = rows.nth(index)
        cells = row.locator('td[data-testid="beast-core-table-td"]')
        if cells.count() < 9:
            continue
        order_match = _ORDER_RE.search(_clean_text(cells.nth(0)))
        if order_match is None:
            continue
        detail = row.get_by_text("售后详情", exact=True)
        if detail.count() == 0:
            continue
        goods_cell = cells.nth(1)
        order_info = _clean_text(goods_cell)
        ellipses = goods_cell.locator('[data-testid="beast-core-ellipsis"]')
        goods_parts = [
            _clean_text(ellipses.nth(part_index))
            for part_index in range(ellipses.count())
        ]
        goods_parts = [part for part in goods_parts if part]
        image = goods_cell.locator("img")
        goods_image_url = (
            (image.first.get_attribute("src") or "").strip()
            if image.count()
            else ""
        )
        shipping_status = _clean_text(cells.nth(2))
        paid_amount_text = _clean_text(cells.nth(3))
        paid_match = _AMOUNT_RE.search(paid_amount_text)
        refund_amount_text = _clean_text(cells.nth(4))
        amount_match = _AMOUNT_RE.search(refund_amount_text)
        refund_time_text = _clean_text(cells.nth(5))
        time_match = _TIME_RE.search(refund_time_text)
        order_no = order_match.group(0)
        appeal_item = _clean_text(cells.nth(6))
        refund_time = time_match.group(0) if time_match else refund_time_text
        item = {
            "order_no": order_no,
            "order_info": order_info,
            "goods_title": goods_parts[0] if goods_parts else order_info,
            "goods_spec": goods_parts[1] if len(goods_parts) > 1 else "",
            "goods_image_url": goods_image_url,
            "shipping_status": shipping_status,
            "paid_amount": paid_match.group(0) if paid_match else "",
            "refund_amount": amount_match.group(0) if amount_match else "",
            "refund_time": refund_time,
            "appeal_item": appeal_item,
            "countdown_text": _clean_text(cells.nth(7)),
        }
        item["record_key"] = make_record_key(order_no, refund_time, appeal_item)
        result.append(item)
    return result


def _find_row(page: Page, task: dict[str, str]) -> Locator | None:
    """在当前页重新定位任务行；详情页来回切换后不复用旧 Locator。"""
    rows = page.locator('#mf-mms-orders-container tr[data-testid="beast-core-table-body-tr"]')
    matches = []
    for index in range(rows.count()):
        row = rows.nth(index)
        text = _clean_text(row)
        cells = row.locator('td[data-testid="beast-core-table-td"]')
        if cells.count() < 9:
            continue
        order = _ORDER_RE.search(_clean_text(cells.nth(0)))
        if order is None or order.group(0) != task["order_no"]:
            continue
        if (
            (not task.get("refund_time") or task["refund_time"] == _clean_text(cells.nth(5)))
            and (not task.get("appeal_item") or _appeal_items(task["appeal_item"]) == _appeal_items(_clean_text(cells.nth(6))))
        ):
            matches.append(row)
    if len(matches) > 1:
        raise RuntimeError("售后申诉记录匹配到多行，已停止以避免错单")
    return matches[0] if matches else None


def can_start_aftersale_appeal(row: Locator) -> bool:
    """以当前订单行的发起申诉按钮状态为准，不以详情入口或倒计时判断。"""
    button = row.get_by_role("button", name="发起申诉", exact=True, include_hidden=True)
    try:
        if button.count() != 1 or not button.is_visible() or not button.is_enabled():
            return False
        return button.evaluate(r"""button => {
            if (button.closest('[inert], [data-disabled="true"]')) return false;
            if (button.matches('[class*="BTN_disabled_"]')) return false;
            if (getComputedStyle(button).pointerEvents === 'none') return false;
            for (let node = button; node; node = node.parentElement) {
                if (getComputedStyle(node).opacity === '0') return false;
            }
            return true;
        }""")
    except PlaywrightError:
        # 列表重绘导致按钮消失或无法读取时，保守跳过，不打开详情。
        return False


def open_aftersale_detail(
    context: BrowserContext,
    list_page: Page,
    row: Locator,
    order_no: str,
) -> Page:
    """严格点击当前行的“售后详情”，捕获平台新开的标签页。"""
    detail_link = row.get_by_text("售后详情", exact=True)
    if detail_link.count() != 1:
        raise RuntimeError(
            f"订单 {order_no} 的售后详情入口数量异常：{detail_link.count()}"
        )
    clear_blockers(list_page)
    detail_link.wait_for(state="visible", timeout=10_000)
    with context.expect_page(timeout=15_000) as page_info:
        human_delay(100, 300)
        human_click(list_page, detail_link)
    detail_page = page_info.value
    detail_page.wait_for_load_state("domcontentloaded")
    clear_blockers(detail_page)
    detail_page.get_by_role("heading", name="退款申请单", exact=True).wait_for(
        state="visible", timeout=30_000
    )
    if AFTERSALE_DETAIL_FRAGMENT not in detail_page.url:
        raise RuntimeError(f"售后详情页地址异常：{detail_page.url}")
    business_read_page(detail_page)
    return detail_page


def extract_aftersale_id(detail_page: Page) -> str:
    values = parse_qs(urlsplit(detail_page.url).query).get("id", [])
    if values and values[0].strip():
        return values[0].strip().rstrip(",，")
    text = _clean_text(
        detail_page.get_by_role("heading", name="退款申请单", exact=True)
        .locator("xpath=..")
    )
    matched = re.search(r"售后编码[：:]\s*(\d+)", text)
    return matched.group(1) if matched else ""


def save_refund_application_screenshot(
    detail_page: Page,
    order_no: str,
    record_key: str,
    aftersale_id: str,
) -> Path:
    """只截“退款申请单”完整卡片，作为必填凭证。"""
    heading = detail_page.get_by_role("heading", name="退款申请单", exact=True)
    if heading.count() != 1:
        raise RuntimeError(f"退款申请单标题数量异常：{heading.count()}")
    card = heading.locator("xpath=..")
    card.wait_for(state="visible", timeout=20_000)
    try:
        detail_page.wait_for_function(
            """
            () => Array.from(document.images).every(
                image => image.complete && image.naturalWidth > 0
            )
            """,
            timeout=10_000,
        ).dispose()
    except PlaywrightTimeoutError:
        print("退款申请单中的部分图片未在 10 秒内加载完成，继续截图。")
    jitter_wait(detail_page, 500)
    folder = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / f"aftersale_{safe_filename(aftersale_id or record_key)}"
    )
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "refund_application.png"
    save_natural_content_screenshot(
        detail_page,
        card,
        path,
        horizontal_padding=(5, 22),
        vertical_padding=(4, 24),
    )
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError("退款申请单截图没有成功写入磁盘。")
    print(f"退款申请单截图已保存：{path}")
    return path


def generate_aftersale_description(
    compact_chat: dict | None,
    classification: str,
    appeal_item: str,
    appeal_reason: str,
) -> str:
    """让文案与货款/运费原因一致，避免复用首次申诉的商品问题模板。"""
    if compact_chat is None or not compact_chat.get("m"):
        if appeal_item == FREIGHT_APPEAL_ITEM:
            return (
                "这单暂无法确认退货原因，现有材料不能说明这笔退货运费扣款应由商家承担的具体依据。"
            )
        return (
            "现有材料没有具体售后经过，尚无法确认这笔售后扣款对应的商家责任。"
        )

    payload = {
        "chat": compact_chat,
        "申诉项": appeal_item,
        "申诉原因": appeal_reason,
        "已有聊天分析结论": classification,
    }
    response = diverse_completion(
        get_client().chat.completions.create,
        max_chars=300,
        model="deepseek-v4-flash",
        messages=[
            {"role": "system", "content": AFTERSALE_DESCRIPTION_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False),
            },
        ],
        temperature=0.2,
        max_tokens=600,
        extra_body={"thinking": {"type": "disabled"}},
        timeout=30,
    )
    text = (response.choices[0].message.content or "").strip()
    if not text or len(text) > 300:
        raise RuntimeError("售后申诉描述为空或超过300字，请重新生成后提交。")
    return text


def analyze_aftersale_chat(
    chat_page: Page,
    order_no: str,
) -> tuple[str, list[dict], list[Path], dict | None]:
    """查询聊天、保存分页截图并生成压缩聊天数据；文案在选完原因后生成。"""
    open_chat_page(chat_page)
    records, no_chat_screenshot = query_chat_records(chat_page, order_no)
    data = build_chat_record_dict(
        order_no,
        records,
        no_chat_record_screenshot=no_chat_screenshot,
        issue_type="售后申诉",
    )
    evidence_paths = [
        BASE_DIR / relative_path
        for relative_path in data["evidence_screenshots"]
        if (BASE_DIR / relative_path).is_file()
    ]
    if not records:
        return (
            NO_CHAT_RECORD_LABEL,
            [],
            select_chat_evidence(evidence_paths, data),
            None,
        )
    compact = compact_chat_json(data)
    classification = classify_appeal_evidence(compact)
    evidence_paths = select_chat_evidence(evidence_paths, data)
    if not evidence_paths:
        raise RuntimeError("没有可用的聊天凭证图片，请检查截图采集是否成功。")
    return classification, records, evidence_paths, compact


def open_aftersale_appeal_modal(detail_page: Page) -> Locator:
    """优先点击维权申诉；仅无此入口时使用极速退款申诉。"""
    button = detail_page.get_by_role("button", name=_APPEAL_BUTTON_RE)
    try:
        button.first.wait_for(state="visible", timeout=10_000)
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("未找到售后申诉按钮：维权申诉 / 极速退款申诉。") from exc
    # 同一详情页可能同时提供两种入口，此时只走维权申诉。
    dispute_button = detail_page.get_by_role("button", name="维权申诉", exact=True)
    button = (
        dispute_button if dispute_button.count()
        else detail_page.get_by_role("button", name="极速退款申诉", exact=True)
    )
    button_count = button.count()
    if button_count != 1:
        raise RuntimeError(f"售后申诉按钮数量异常：{button_count}（维权申诉 / 极速退款申诉）")
    safe_click(detail_page, button, "售后申诉按钮（维权申诉 / 极速退款申诉）")
    modal = detail_page.locator(
        '[data-testid="beast-core-modal-innerWrapper"]:visible'
    ).filter(has_text=_APPEAL_TITLE_RE)
    modal.first.wait_for(state="visible", timeout=15_000)
    modal_count = modal.count()
    if modal_count != 1:
        raise RuntimeError(f"售后申诉表单数量异常：{modal_count}")
    return modal


def _modal_form_value(modal: Locator, label_text: str) -> str:
    label = modal.get_by_text(label_text, exact=True)
    if label.count() != 1:
        raise RuntimeError(f"售后申诉表单的“{label_text}”标签数量异常：{label.count()}")
    item = label.locator(
        'xpath=ancestor::*[@data-testid="beast-core-form-item"][1]'
    )
    text = _clean_text(item)
    return text.removeprefix(label_text).strip()


def verify_modal_context(modal: Locator, task: dict[str, str]) -> None:
    """防止列表刷新或标签页错位后把材料填到错误订单/申诉项。"""
    modal_order_text = _modal_form_value(modal, "申诉订单号")
    order_match = _ORDER_RE.search(modal_order_text)
    actual_order_no = order_match.group(0) if order_match else ""
    if actual_order_no != task["order_no"]:
        raise RuntimeError(
            "售后申诉模态框订单不一致："
            f"列表={task['order_no']!r}，表单={actual_order_no!r}。"
        )
    actual_item = _modal_form_value(modal, "申诉项")
    if _is_combined_appeal(task["appeal_item"]):
        # 保留页面默认勾选；使用 checkbox 文本避免顺序和分隔符差异。
        checked = modal.locator(
            '[id="subInfoVOS"] [data-testid="beast-core-checkbox"][data-checked="true"]'
        )
        matches = {_clean_text(checked.nth(i)) for i in range(checked.count())}
        item_matches = matches == _appeal_items(task["appeal_item"])
    else:
        item_matches = actual_item == task["appeal_item"]
    if not item_matches:
        raise RuntimeError(
            "售后申诉模态框申诉项不一致："
            f"列表={task['appeal_item']!r}，表单={actual_item!r}。"
        )


def choose_goods_reason(available_reasons: list[str]) -> str:
    """货款申诉优先质量凭证不足；没有该项时才选兜底原因。"""
    if GOODS_PREFERRED_REASON in available_reasons:
        return GOODS_PREFERRED_REASON
    if GOODS_FALLBACK_REASON in available_reasons:
        return GOODS_FALLBACK_REASON
    raise RuntimeError(
        "货款申诉原因列表既没有指定质量问题原因，也没有“非以上申诉原因”："
        f"{available_reasons}"
    )


def _cascader_list(portal: Locator, index: int) -> Locator:
    return portal.locator(
        f'ul[data-testid="beast-core-cascader-list-wrapper-{index}"]'
    )


def _cascader_options(menu: Locator) -> list[tuple[str, Locator]]:
    options = menu.locator('li[data-testid="beast-core-cascader-list-item"]')
    return [(_clean_text(options.nth(index)), options.nth(index)) for index in range(options.count())]


def _click_cascader_option(
    detail_page: Page,
    menu: Locator,
    option_text: str,
) -> None:
    # 文字定位始终限定在当前菜单内，避免索引因列表重绘而指向另一项。
    option = menu.get_by_text(option_text, exact=True)
    count = option.count()
    if count != 1:
        raise RuntimeError(
            f"级联原因“{option_text}”匹配数量异常：{count}"
        )
    stealth_safe_click(detail_page, option, f"申诉原因：{option_text}")


def save_reason_selection_diagnostics(
    detail_page: Page, modal: Locator, appeal_item: str, error: Exception,
) -> Path:
    """只读保存失败现场，包括匹配数为零、缺少原因和读回不一致的情况。"""
    data = {
        "appeal_item": appeal_item,
        "original_error": str(error),
        "error_type": type(error).__name__,
        "observed_at": datetime.now().isoformat(timespec="milliseconds"),
        "capture_errors": {},
    }

    def capture(name, action):
        try:
            data[name] = action()
        except Exception as exc:
            data["capture_errors"][name] = str(exc)

    capture("url", lambda: detail_page.url)
    order_no = parse_qs(urlsplit(data.get("url", "")).query).get("orderSn", ["unknown_order"])[0]
    data["order_no"] = order_no
    for key, label in (("order_fields", "申诉订单号"),
                       ("appeal_item_fields", "申诉项"),
                       ("reason_fields", "申诉原因")):
        capture(key, lambda label=label: modal.get_by_text(label, exact=True).locator(
            'xpath=ancestor::*[@data-testid="beast-core-form-item"][1]'
        ).evaluate_all("nodes => nodes.map(el => ({html: el.outerHTML, text: el.innerText}))"))
    if order_no == "unknown_order":
        order_text = " ".join(field["text"] for field in data.get("order_fields", []))
        match = _ORDER_RE.search(order_text)
        if match:
            order_no = match.group(0)
            data["order_no"] = order_no
    # 即使菜单已收起，也保存 DOM 中残留的菜单、提示框及其可见状态。
    capture("portals", lambda: detail_page.locator('[data-testid="beast-core-portal"]').evaluate_all("""nodes =>
        nodes.map(el => ({
            html: el.outerHTML,
            visible: Boolean(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden',
            menus: [...el.querySelectorAll('ul[data-testid^="beast-core-cascader-list-wrapper-"]')].map(menu => ({
                testid: menu.getAttribute('data-testid'),
                options: [...menu.querySelectorAll('[data-testid="beast-core-cascader-list-item"]')].map(item => ({
                    text: item.innerText, disabled: item.getAttribute('data-disabled')
                }))
            }))
        }))"""))
    folder = DEBUG_DIR / "aftersale_reason_diagnostics"
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"reason_{safe_filename(order_no)}_{datetime.now():%Y%m%d_%H%M%S_%f}_{os.getpid()}"
    report = stem.with_suffix(".html")
    # 在本地报告中以源码文本展示，不运行现场 HTML 里的脚本或样式。
    sections = ["<!doctype html><html><meta charset='utf-8'><title>售后申诉原因诊断</title><body>",
                "<h1>售后申诉原因选择失败</h1><pre>" + escape(json.dumps(
                    {key: value for key, value in data.items() if key not in (
                        "order_fields", "appeal_item_fields", "reason_fields", "portals")},
                    ensure_ascii=False, indent=2)) + "</pre>"]
    for key, title in (("order_fields", "申诉订单号"), ("appeal_item_fields", "申诉项"),
                       ("reason_fields", "申诉原因字段"), ("portals", "可选申诉原因浮层（含隐藏菜单和提示框）")):
        sections.append(f"<h2>{title}</h2>")
        entries = data.get(key, [])
        if not entries:
            sections.append("<p>未匹配到元素；如采集失败，请查看报告中的 capture_errors。</p>")
        for entry in entries:
            if "visible" in entry:
                sections.append(f"<p>visible: {entry['visible']}</p>")
            sections.append("<pre>" + escape(entry["html"]) + "</pre>")
    sections.append("</body></html>")
    report.write_text("\n".join(sections), encoding="utf-8")
    print(f"[售后申诉原因诊断] 订单 {order_no}，申诉项={appeal_item!r}；HTML 已保存：{report}")
    json_path = stem.with_suffix(".json")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[售后申诉原因诊断] 原始 HTML、可选原因及错误信息：{json_path}")
    return report


def _ordered_appeal_sections(modal: Locator, appeal_item: str) -> list[tuple[int, str]]:
    """使用页面勾选项的 DOM 顺序，而不是列表文字的顺序。"""
    if not _is_combined_appeal(appeal_item):
        return [(0, appeal_item)]
    checked = modal.locator(
        '[id="subInfoVOS"] [data-testid="beast-core-checkbox"][data-checked="true"]'
    )
    items = [_clean_text(checked.nth(i)) for i in range(checked.count())]
    if len(items) != len(_appeal_items(appeal_item)) or set(items) != _appeal_items(appeal_item):
        raise RuntimeError("无法确认表单各组申诉项及顺序，停止填写。")
    return list(enumerate(items))


def select_appeal_reason(
    detail_page: Page,
    modal: Locator,
    appeal_item: str,
    *,
    section_index: int | None = None,
    section_item: str | None = None,
) -> str:
    try:
        return _select_appeal_reason(detail_page, modal, appeal_item,
                                    section_index=section_index, section_item=section_item)
    except Exception as error:
        try:
            save_reason_selection_diagnostics(detail_page, modal, appeal_item, error)
        except Exception as capture_error:
            print(f"[售后申诉原因诊断] 保存现场失败：{capture_error}；原始选择错误：{error}")
        raise


def _select_appeal_reason(
    detail_page: Page,
    modal: Locator,
    appeal_item: str,
    *,
    section_index: int | None = None,
    section_item: str | None = None,
) -> str:
    """按申诉项和当前实际选项选择原因，并读回完整值校验。"""
    if not _supported_appeal_item(appeal_item):
        raise RuntimeError(f"暂不支持的售后申诉项：{appeal_item!r}")
    if section_index is None and _is_combined_appeal(appeal_item):
        reasons = []
        for index, item in _ordered_appeal_sections(modal, appeal_item):
            container = modal.locator(f'[id="subInfoVOS[{index}].reason"]')
            if not container.count():
                continue
            reasons.append(_select_appeal_reason(detail_page, modal, appeal_item,
                                                section_index=index, section_item=item))
        return "；".join(dict.fromkeys(reasons))
    index = section_index if section_index is not None else 0
    item = section_item or appeal_item
    container = modal.locator(f'[id="subInfoVOS[{index}].reason"]')
    reason_input = container.locator(
        'input[data-testid="beast-core-cascader-htmlInput"]'
    )
    reason_input.wait_for(state="visible", timeout=10_000)
    current_value = reason_input.input_value().strip()
    combined = _is_combined_appeal(appeal_item)
    is_goods = item == GOODS_APPEAL_ITEM
    # 同时包含货款和纠纷退款率申诉时，使用“非以上申诉原因”。
    goods_reason = (
        GOODS_FALLBACK_REASON
        if combined
        else GOODS_PREFERRED_REASON
    )
    if is_goods and current_value == goods_reason:
        return goods_reason
    if (
        item == FREIGHT_APPEAL_ITEM
        and FREIGHT_REASON_PARENT in current_value
        and FREIGHT_REASON in current_value
    ):
        return FREIGHT_REASON
    stealth_safe_click(detail_page, reason_input, "售后申诉原因")
    # 下拉菜单和悬停提示都使用 beast-core-portal。Locator 会在每次操作时
    # 重新解析，不能取最后一个浮层，否则鼠标悬停后会切换到新出现的提示框。
    portal = detail_page.locator(
        '[data-testid="beast-core-portal"]:visible'
    ).filter(has=detail_page.locator(
        'ul[data-testid="beast-core-cascader-list-wrapper-0"]'
    ))
    root_menu = _cascader_list(portal, 0)
    root_menu.wait_for(state="visible", timeout=10_000)

    if is_goods:
        target = (
            goods_reason if combined
            else choose_goods_reason([text for text, _ in _cascader_options(root_menu)])
        )
        _click_cascader_option(detail_page, root_menu, target)
        actual = reason_input.input_value().strip()
        if actual != target:
            raise RuntimeError(
                f"第{index + 1}组货款申诉原因读回校验失败：期望={target!r}，实际={actual!r}。"
            )
        return target

    _click_cascader_option(detail_page, root_menu, FREIGHT_REASON_PARENT)
    child_menu = _cascader_list(portal, 1)
    child_menu.wait_for(state="visible", timeout=10_000)
    _click_cascader_option(detail_page, child_menu, FREIGHT_REASON)
    actual = reason_input.input_value().strip()
    if FREIGHT_REASON_PARENT not in actual or FREIGHT_REASON not in actual:
        raise RuntimeError(
            "运费申诉原因读回校验失败："
            f"期望包含={FREIGHT_REASON_PARENT!r}/{FREIGHT_REASON!r}，实际={actual!r}。"
        )
    return FREIGHT_REASON


def fill_maximum_amount(modal: Locator, page: Page | None = None, *, index: int = 0) -> str:
    container = modal.locator(f'[id="subInfoVOS[{index}].amount"]')
    text = _clean_text(container)
    matched = re.search(r"最多可申诉\s*(\d+(?:\.\d{1,2})?)\s*元", text)
    if matched is None:
        raise RuntimeError(f"无法读取最多可申诉金额：{text!r}")
    maximum = matched.group(1)
    amount_input = container.locator(
        'input[data-testid="beast-core-inputNumber-htmlInput"]'
    )
    amount_input.wait_for(state="visible", timeout=10_000)
    if page is None:
        # 兼容独立逻辑测试和外部调用；实际浏览器流程始终传 page。
        amount_input.fill(maximum)
    else:
        human_click(page, amount_input)
        amount_input.press("Control+A")
        human_delay(70, 180)
        amount_input.press("Backspace")
        if amount_input.input_value():
            amount_input.fill("")
        human_type(page, amount_input, maximum, click_first=False)
    if amount_input.input_value().strip() != maximum:
        raise RuntimeError("申诉金额填写后读回校验失败。")
    return maximum


def _upload_images(
    modal: Locator,
    container_id: str,
    paths: list[Path],
    *,
    required: bool,
) -> list[Path]:
    valid = [path for path in paths if path.is_file()][:3]
    if required and not valid:
        raise RuntimeError(f"#{container_id} 没有可上传的图片。")
    if not valid:
        return []
    container = modal.locator(f'[id="{container_id}"]')
    if not container.count():
        container_id = container_id.replace('ImageUrls', 'VoucherUrls')
        container = modal.locator(f'[id="{container_id}"]')
    file_input = container.locator(
        'input[data-testid="beast-core-upload-input"][accept*=".jpg"]'
    )
    file_input.wait_for(state="attached", timeout=10_000)
    if file_input.count() != 1:
        raise RuntimeError(f"#{container_id} 图片上传框数量异常：{file_input.count()}")
    business_pause(modal.page, reading=True)
    resolved = [str(path.resolve()) for path in valid]
    if file_input.get_attribute("multiple") is not None:
        file_input.set_input_files(resolved)
    else:
        for path in resolved:
            business_pause(modal.page)
            file_input.set_input_files(path)
    return valid


def wait_for_uploads(detail_page: Page, modal: Locator) -> None:
    """等待上传错误/进度提示稳定；明确报错时中止，避免带坏凭证继续。"""
    jitter_wait(detail_page, 1_500)
    for _ in range(20):
        text = _clean_text(modal)
        if any(marker in text for marker in ("上传失败", "格式不支持", "图片过大")):
            raise RuntimeError("售后申诉凭证上传失败：" + text[-160:])
        if not any(marker in text for marker in ("上传中", "正在上传")):
            return
        jitter_wait(detail_page, 750, 0.20)
    raise RuntimeError("售后申诉凭证在限定时间内没有上传完成。")


def fill_description(detail_page: Page, modal: Locator, text: str) -> None:
    """粘贴申诉描述；读回不一致时重新粘贴一次。"""
    text = text.strip()[:300]
    if not text:
        raise RuntimeError("申诉描述不能为空。")
    textarea = modal.locator(
        '#description textarea[data-testid="beast-core-textArea-htmlInput"]'
    )
    textarea.wait_for(state="visible", timeout=10_000)

    actual = ""
    for attempt in range(1, 3):
        try:
            paste_text(detail_page, textarea, text)
        except (PlaywrightTimeoutError, AssertionError):
            if attempt == 2:
                raise
        human_delay(300, 650)
        actual = textarea.input_value()
        if actual == text:
            return
        if attempt == 1:
            print(
                "申诉描述首次输入读回不一致，"
                "将重新粘贴一次。"
            )

    limit = min(len(text), len(actual))
    difference = next(
        (index for index in range(limit) if text[index] != actual[index]),
        limit,
    )
    raise RuntimeError(
        "申诉描述填写后读回校验失败："
        f"期望长度={len(text)}，实际长度={len(actual)}，"
        f"首个差异位置={difference + 1}，"
        f"期望={text!r}，实际={actual!r}。"
    )


def submit_aftersale_form(
    detail_page: Page,
    modal: Locator,
    order_no: str,
) -> None:
    """精确点击售后维权表单的“提交申诉”，并确认表单已关闭。"""
    business_pause(detail_page, reading=True)
    submit = modal.get_by_role("button", name="提交申诉", exact=True)
    if submit.count() != 1:
        raise RuntimeError(f"售后申诉提交按钮数量异常：{submit.count()}")

    stealth_safe_click(detail_page, submit, "真实提交售后申诉")
    wait_for_submit_captcha(detail_page)
    try:
        modal.wait_for(state="hidden", timeout=20_000)
    except PlaywrightTimeoutError as exc:
        modal_text = _clean_text(modal)
        error_markers = (
            "不能为空",
            "请上传",
            "请选择",
            "上传失败",
            "提交失败",
            "申诉失败",
        )
        errors = [marker for marker in error_markers if marker in modal_text]
        detail = "、".join(errors) if errors else "提交后表单仍然可见"
        raise RuntimeError(
            f"订单 {order_no} 未确认提交成功：{detail}。"
        ) from exc
    print(f"订单 {order_no} 的售后申诉已真实提交。")


def fill_and_submit_aftersale_form(
    detail_page: Page,
    task: dict[str, str],
    refund_application: Path,
    chat_evidence: list[Path],
    classification: str,
    compact_chat: dict | None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, Path, str, str]:
    """完整填写并校验表单，在安全停止检查通过后真实提交。"""
    if len(chat_evidence) > 2:
        raise RuntimeError("聊天凭证最多两张，须先完成截图选择。")
    order_no = task["order_no"]
    record_key = task["record_key"]
    modal = open_aftersale_appeal_modal(detail_page)
    business_pause(detail_page, reading=True)
    verify_modal_context(modal, task)
    maximum = ""
    reasons = []
    evidence_sections = []
    # 按页面顺序完成一整组后再向下操作，避免来回跳转各组字段。
    for evidence_index, item in _ordered_appeal_sections(modal, task["appeal_item"]):
        print(f"填写第{evidence_index + 1}组：{item}")
        if modal.locator(f'[id="subInfoVOS[{evidence_index}].reason"]').count():
            reasons.append(select_appeal_reason(
                detail_page, modal, task["appeal_item"],
                section_index=evidence_index, section_item=item,
            ))
        if modal.locator(f'[id="subInfoVOS[{evidence_index}].amount"]').count():
            amount = fill_maximum_amount(modal, page=detail_page, index=evidence_index)
            if not maximum:
                maximum = amount
        if not modal.locator(
            f'[id="subInfoVOS[{evidence_index}].mustImageUrls"], '
            f'[id="subInfoVOS[{evidence_index}].mustVoucherUrls"]'
        ).count():
            continue
        required_files = _upload_images(
            modal,
            f"subInfoVOS[{evidence_index}].mustImageUrls",
            [refund_application],
            required=True,
        )
        optional_files = _upload_images(
            modal,
            f"subInfoVOS[{evidence_index}].optionalImageUrls",
            chat_evidence,
            required=False,
        )
        evidence_sections.append((required_files, optional_files))
        wait_for_uploads(detail_page, modal)
        if len(required_files) != 1 or len(optional_files) > 2:
            raise RuntimeError(f"第{evidence_index + 1}组凭证数量校验失败，停止填写后续组。")
    appeal_reason = "；".join(dict.fromkeys(reasons))
    appeal_text = generate_aftersale_description(
        compact_chat, classification, task["appeal_item"], appeal_reason,
    )
    fill_description(detail_page, modal, appeal_text)

    # 最终动作前做最后一次关键字段校验。
    if not evidence_sections:
        raise RuntimeError("未找到任何必填凭证区，停止提交。")
    for evidence_index, (required_files, optional_files) in enumerate(evidence_sections):
        if len(required_files) != 1:
            raise RuntimeError(f"第{evidence_index + 1}组必填凭证数量校验失败。")
        if len(optional_files) > 2:
            raise RuntimeError(f"第{evidence_index + 1}组聊天凭证最多上传两张。")
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    form_path = DEBUG_DIR / (
        f"aftersale_form_filled_{safe_filename(order_no)}_"
        f"{safe_filename(record_key)}.png"
    )
    save_natural_content_screenshot(detail_page, modal, form_path)

    if should_stop is not None and should_stop():
        cancel = modal.get_by_role("button", name="取消", exact=True)
        if cancel.count() != 1:
            raise RuntimeError(
                f"停止时售后申诉取消按钮数量异常：{cancel.count()}"
            )
        stealth_safe_click(detail_page, cancel, "停止前取消售后申诉")
        modal.wait_for(state="hidden", timeout=10_000)
        raise AftersaleAppealStopped("提交前")

    submit_aftersale_form(detail_page, modal, order_no)
    return maximum, form_path, appeal_reason, appeal_text


@paced_business_operations
def process_aftersale_task(
    context: BrowserContext,
    list_page: Page,
    chat_page: Page,
    task: dict[str, str],
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """处理一条售后申诉；异常落库并向调用方返回，不影响后续订单。"""
    shop = SHOP_ID or "default"
    order_no = task["order_no"]
    record_key = task["record_key"]
    detail_page: Page | None = None
    discovered = False
    try:
        _raise_if_stopped(should_stop, "检查售后申诉按钮前")
        row = _find_row(list_page, task)
        if row is not None and not can_start_aftersale_appeal(row):
            print(f"售后申诉订单 {order_no}：发起申诉按钮不可点击，跳过。")
            return {
                "order_no": order_no,
                "record_key": record_key,
                "result": _UNAVAILABLE_APPEAL_RESULT,
            }
        db.record_aftersale_appeal_discovered(shop, **task)
        discovered = True
        if row is None:
            raise RuntimeError("当前列表页找不到对应售后申诉行。")
        detail_page = open_aftersale_detail(context, list_page, row, order_no)
        aftersale_id = extract_aftersale_id(detail_page)
        refund_application = save_refund_application_screenshot(
            detail_page, order_no, record_key, aftersale_id
        )
        if not _supported_appeal_item(task["appeal_item"]):
            raise RuntimeError(
                f"暂不支持的售后申诉项：{task['appeal_item']!r}"
            )
        classification, messages, chat_evidence, compact_chat = (
            analyze_aftersale_chat(chat_page, order_no)
        )
        db.update_aftersale_appeal_status(
            shop,
            order_no,
            record_key,
            "材料已准备",
            aftersale_id=aftersale_id,
            classification=classification,
            messages=messages,
            message_count=len(messages),
            refund_application_path=to_relative_evidence_path(refund_application),
            chat_evidence_paths=[
                to_relative_evidence_path(path) for path in chat_evidence
            ],
        )
        _raise_if_stopped(should_stop, "售后申诉打开表单前")
        appeal_amount, form_path, appeal_reason, appeal_text = fill_and_submit_aftersale_form(
            detail_page,
            task,
            refund_application,
            chat_evidence,
            classification,
            compact_chat,
            should_stop=should_stop,
        )
        db.update_aftersale_appeal_status(
            shop,
            order_no,
            record_key,
            "已提交",
            appeal_reason=appeal_reason,
            appeal_amount=appeal_amount,
            appeal_text=appeal_text,
            form_screenshot_path=to_relative_evidence_path(form_path),
        )
        return {
            "order_no": order_no,
            "record_key": record_key,
            "result": "已提交",
        }
    except AftersaleAppealStopped:
        raise
    except Exception as exc:  # noqa: BLE001 - 单单隔离，失败后继续下一单
        if not discovered:
            db.record_aftersale_appeal_discovered(shop, **task)
        error_page = detail_page if detail_page is not None and not detail_page.is_closed() else list_page
        shot = save_error_screenshot(error_page, f"aftersale_{order_no}")
        db.update_aftersale_appeal_status(
            shop,
            order_no,
            record_key,
            "失败",
            error_message=str(exc),
            error_screenshot_path=(
                to_relative_evidence_path(shot) if shot is not None else None
            ),
        )
        return {"order_no": order_no, "record_key": record_key, "result": f"失败: {exc}"}
    finally:
        if detail_page is not None and not detail_page.is_closed():
            try:
                detail_page.close()
            except Exception as close_error:  # noqa: BLE001
                print(f"关闭售后详情标签页失败：{close_error}")


class AftersaleAppealSession:
    """单店铺常驻会话；每次 ``process_next`` 严格处理一张订单。"""

    def __init__(
        self,
        context: BrowserContext,
        list_page: Page,
        chat_page: Page,
        *,
        max_orders: int,
        max_pages: int = 200,
        should_stop: Callable[[], bool] | None = None,
    ) -> None:
        if max_orders <= 0:
            raise ValueError("售后申诉每店处理上限必须为正整数。")
        self.context = context
        self.list_page = list_page
        self.chat_page = chat_page
        self.max_orders = max_orders
        self.max_pages = max_pages
        self.should_stop = should_stop
        self.processed = 0
        self.page_number = 1
        self.tasks: list[dict[str, str]] = []
        self.task_index = 0
        self.visited_signatures: set[str] = set()
        self.initialized = False
        self.done = False

    def initialize(self) -> int:
        _raise_if_stopped(self.should_stop, "售后申诉初始化前")
        open_aftersale_appeal_page(self.list_page)
        self.initialized = True
        if not self._load_current_page():
            self.done = True
            return 0
        # 后续页只有在当前页逐单处理完成后才翻，避免 init 扫描改变任务行所在页。
        return self.max_orders

    def _save_list_error(self, name: str, message: str) -> RuntimeError:
        shot = save_error_screenshot(self.list_page, name)
        suffix = (
            f"，现场截图：{to_relative_evidence_path(shot)}"
            if shot is not None
            else ""
        )
        return RuntimeError(message + suffix)

    def checkpoint(self) -> dict:
        return {key: getattr(self,key) for key in
                ('processed','page_number','tasks','task_index','done')} | {
                    'visited_signatures': list(self.visited_signatures)}

    def restore(self, state: dict) -> int:
        """Restore the exact unfinished queue; refuse changed pagination rather than rescan."""
        open_aftersale_appeal_page(self.list_page)
        wait_for_aftersale_list_ready(self.list_page)
        for _ in range(1,state['page_number']):
            _raise_if_stopped(self.should_stop, '恢复售后列表')
            clear_blockers(self.list_page)
            old = self.list_page.locator('#mf-mms-orders-container [data-testid="beast-core-table"]').inner_text().strip()
            if not click_next_page(self.list_page, root_selector='#mf-mms-orders-container').get('clicked'):
                raise RuntimeError('内存回收后售后列表页数发生变化，已停止，未重试任何订单。')
            self.list_page.wait_for_function(
                "old => (document.querySelector('#mf-mms-orders-container [data-testid=beast-core-table]')?.innerText || '').trim() !== old",
                arg=old, timeout=20_000,
            ).dispose()
            wait_for_aftersale_list_ready(self.list_page)
        available = {item['record_key'] for item in extract_aftersale_rows(self.list_page)}
        expected = {item['record_key'] for item in state['tasks'][state['task_index']:]}
        if not expected.issubset(available):
            raise RuntimeError('内存回收后剩余售后记录不在原列表页，已停止以避免错单。')
        for key in ('processed','page_number','tasks','task_index','done'):
            setattr(self,key,state[key])
        self.visited_signatures=set(state['visited_signatures'])
        self.initialized=True
        return max(0,self.max_orders-self.processed)

    def _load_current_page(self) -> bool:
        _raise_if_stopped(self.should_stop, "读取售后申诉列表")
        list_state = wait_for_aftersale_list_ready(self.list_page)
        if list_state == "empty":
            self.tasks = []
            self.task_index = 0
            return False

        tasks = extract_aftersale_rows(self.list_page)
        signature = "|".join(task["record_key"] for task in tasks)
        if not signature:
            raise self._save_list_error(
                f"aftersale_rows_unreadable_{SHOP_ID or 'default'}",
                f"售后申诉列表第 {self.page_number} 页已出现订单行，"
                "但未能提取出有效业务字段，为避免漏单已停止",
            )
        if signature in self.visited_signatures:
            raise self._save_list_error(
                f"aftersale_repeated_page_{SHOP_ID or 'default'}",
                "售后申诉翻页后仍是重复列表，为避免漏单已停止",
            )
        self.visited_signatures.add(signature)
        self.tasks = tasks
        self.task_index = 0
        return True

    def _advance_page(self) -> bool:
        if self.page_number >= self.max_pages:
            raise RuntimeError(
                f"售后申诉列表翻页超过安全上限 {self.max_pages} 页，已停止。"
            )
        clear_blockers(self.list_page)
        old_table_text = _clean_text(
            self.list_page.locator('#mf-mms-orders-container [data-testid="beast-core-table"]')
        )
        next_result = click_next_page(self.list_page, root_selector='#mf-mms-orders-container')
        if not next_result.get("clicked"):
            return False
        try:
            self.list_page.wait_for_function(
                """
                previous => {
                    const table = document.querySelector('#mf-mms-orders-container [data-testid="beast-core-table"]');
                    const current = (table?.innerText || '').replace(/\\s+/g, ' ').trim();
                    return current && current !== previous;
                }
                """,
                arg=old_table_text,
                timeout=20_000,
            ).dispose()
        except PlaywrightTimeoutError:
            jitter_wait(self.list_page, 1_000)
        self.page_number += 1
        return self._load_current_page()

    def _ensure_task(self) -> bool:
        if self.done or self.processed >= self.max_orders:
            self.done = True
            return False
        if self.task_index < len(self.tasks):
            return True
        if not self._advance_page():
            self.done = True
            return False
        return self.task_index < len(self.tasks)

    def process_next(self) -> tuple[dict[str, str] | None, int]:
        if not self.initialized:
            raise RuntimeError("售后申诉会话尚未初始化。")
        _raise_if_stopped(self.should_stop, "处理下一张售后申诉前")
        # 保留全部列表行用于翻页签名和恢复校验，执行时重新检查按钮。
        # 跳过的行不占处理名额，整页不可申诉时继续翻页寻找可处理订单。
        while self._ensure_task():
            _raise_if_stopped(self.should_stop, "检查下一张售后申诉前")
            task = self.tasks[self.task_index]
            self.task_index += 1
            outcome = process_aftersale_task(
                self.context,
                self.list_page,
                self.chat_page,
                task,
                should_stop=self.should_stop,
            )
            if outcome["result"] == _UNAVAILABLE_APPEAL_RESULT:
                continue
            self.processed += 1

            if self.processed >= self.max_orders:
                self.done = True
            elif not self._ensure_task():
                self.done = True
            remaining = 0 if self.done else self.max_orders - self.processed
            return outcome, remaining
        return None, 0


def run_aftersale_appeals(
    context: BrowserContext,
    list_page: Page,
    chat_page: Page,
    *,
    max_orders: int | None = None,
    max_pages: int = 200,
    on_progress: Callable[[int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """逐页真实提交售后申诉；主要供非 GUI 的一次性 CLI 调用。"""
    try:
        _raise_if_stopped(should_stop, "开始前")
    except AftersaleAppealStopped:
        return [], True
    open_aftersale_appeal_page(list_page)
    outcomes: list[dict[str, str]] = []
    visited_signatures: set[str] = set()

    for page_number in range(1, max_pages + 1):
        try:
            _raise_if_stopped(should_stop, "读取售后申诉列表")
        except AftersaleAppealStopped:
            return outcomes, True
        list_state = wait_for_aftersale_list_ready(list_page)
        if list_state == "empty":
            print(f"售后申诉列表第 {page_number} 页明确显示暂无可处理订单。")
            break
        tasks = extract_aftersale_rows(list_page)
        signature = "|".join(task["record_key"] for task in tasks)
        if not signature:
            shot = save_error_screenshot(
                list_page,
                f"aftersale_rows_unreadable_{SHOP_ID or 'default'}",
            )
            suffix = (
                f"，现场截图：{to_relative_evidence_path(shot)}"
                if shot is not None
                else ""
            )
            raise RuntimeError(
                f"售后申诉列表第 {page_number} 页已出现订单行，"
                "但未能提取出有效业务字段，为避免漏单已停止" + suffix
            )
        if signature in visited_signatures:
            print("售后申诉列表出现重复页，停止以避免循环。")
            break
        visited_signatures.add(signature)

        for task in tasks:
            if max_orders is not None and len(outcomes) >= max_orders:
                return outcomes, False
            try:
                _raise_if_stopped(should_stop, "处理售后申诉订单")
            except AftersaleAppealStopped:
                return outcomes, True
            try:
                outcome = process_aftersale_task(
                    context,
                    list_page,
                    chat_page,
                    task,
                    should_stop=should_stop,
                )
            except AftersaleAppealStopped:
                return outcomes, True
            if outcome["result"] == _UNAVAILABLE_APPEAL_RESULT:
                continue
            outcomes.append(outcome)
            if on_progress is not None:
                on_progress(len(outcomes), len(outcomes))
            human_delay(700, 1_800)

        try:
            _raise_if_stopped(should_stop, "售后申诉翻页前")
        except AftersaleAppealStopped:
            return outcomes, True

        clear_blockers(list_page)
        old_table_text = _clean_text(
            list_page.locator('#mf-mms-orders-container [data-testid="beast-core-table"]')
        )
        next_result = click_next_page(list_page, root_selector='#mf-mms-orders-container')
        if not next_result.get("clicked"):
            break
        try:
            list_page.wait_for_function(
                """
                previous => {
                    const table = document.querySelector('#mf-mms-orders-container [data-testid="beast-core-table"]');
                    const current = (table?.innerText || '').replace(/\\s+/g, ' ').trim();
                    return current && current !== previous;
                }
                """,
                arg=old_table_text,
                timeout=20_000,
            ).dispose()
        except PlaywrightTimeoutError:
            # 页签名在下一轮还有重复保护；这里给前端一次额外稳定时间。
            jitter_wait(list_page, 1_000)

    return outcomes, False
