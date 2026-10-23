"""消费者负向体验申诉的复议扫描、材料复用与逐单提交。"""

from __future__ import annotations

import random
import re
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from playwright.sync_api import Locator, Page
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import db
from appeal_classifier import compact_chat_json
from appeal_wording import generate_reconsideration_description
from appeal_submit import collect_evidence_files, wait_for_submit_captcha
from blockers import clear_blockers, safe_click, stealth_safe_click
from paths import BASE_DIR, DEBUG_DIR
from run_appeal_batch import process_single_order
from screenshots import (
    safe_filename,
    save_error_screenshot,
    save_natural_content_screenshot,
    to_relative_evidence_path,
)
from stealth import (
    business_pause,
    business_read_page,
    paced_business_operations,
    human_click,
    human_delay,
    human_scroll,
    human_type,
    paste_text,
    jitter_wait,
)

RECONSIDERATION_URL = (
    "https://mms.pinduoduo.com/orders/appeals/negativeExperienceReconsider"
    "?reconsiderSecondaryTabKey=negativeExperience"
)

ALLOWED_REASONS = (
    "商品无问题，消费者误解",
    "消费者未表达商品问题（含售后详情内未描述问题等）",
    "其他",
)
EXCLUDED_REASONS = {
    "商品有问题，但已与消费者达成一致其他售后方案",
    "商品有问题，但消费者主动表示无需处理",
}

CLASSIFICATION_REASON_MAP = {
    "消费者所述问题经核验不存在": ALLOWED_REASONS[0],
    "消费者误解商品信息": ALLOWED_REASONS[0],
    "消费者未表达商品问题": ALLOWED_REASONS[1],
}

_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}")
_AMOUNT_RE = re.compile(r"\d+(?:\.\d{1,2})?")
_ORDER_RE = re.compile(r"\d{6}-\d+")

RECONSIDERATION_FINISHED_STATUSES = {
    "已提交复议",
    "复议提交确认中",
    "复议结果待确认",
    "待处理",
    "审核中",
    "申诉成功",
    "申诉失败",
}


class ReconsiderationStopped(Exception):
    """用户请求在当前安全边界停止复议。"""


_SUCCESS_MODAL_HIDE_TIMEOUT_MS = 3_000


def _raise_if_stopped(
    should_stop: Callable[[], bool] | None,
    stage: str,
) -> None:
    if should_stop is not None and should_stop():
        raise ReconsiderationStopped(stage)


def _close_reconsideration_success_modal(
    page: Page,
    success_modal: Locator,
    order_no: str,
) -> None:
    """点击成功弹窗内部的“继续复议”，快速确认弹窗已经消失。

    这里必须用 stealth_safe_click：safe_click 会在点击前调用 clear_blockers，
    把当前成功弹窗本身先关掉，随后对已经隐藏的按钮重试 3 次、每次最多等
    8 秒，正是此前“点完继续复议像卡住二十多秒”的原因。
    """
    try:
        continue_button = success_modal.get_by_role(
            "button", name="继续复议", exact=True
        )
        if continue_button.count() == 1:
            stealth_safe_click(
                page,
                continue_button,
                "提交成功后继续复议",
                timeout=3_000,
                retries=1,
            )
        else:
            # 页面文案/按钮结构小幅变化时，成功状态已经确认，使用右上角关闭
            # 图标兜底；绝不点击“查看复议详情”，避免离开当前复议列表标签页。
            close_icon = success_modal.locator(
                '[data-testid="beast-core-modal-icon-close"]'
            )
            if close_icon.count():
                stealth_safe_click(
                    page,
                    close_icon.first,
                    "关闭复议成功弹窗",
                    timeout=3_000,
                    retries=1,
                )

        try:
            success_modal.wait_for(
                state="hidden",
                timeout=_SUCCESS_MODAL_HIDE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            close_icon = success_modal.locator(
                '[data-testid="beast-core-modal-icon-close"]'
            )
            if close_icon.count() and close_icon.first.is_visible():
                stealth_safe_click(
                    page,
                    close_icon.first,
                    "关闭未自动消失的复议成功弹窗",
                    timeout=3_000,
                    retries=1,
                )
                success_modal.wait_for(
                    state="hidden",
                    timeout=_SUCCESS_MODAL_HIDE_TIMEOUT_MS,
                )
    except Exception as exc:
        # 成功弹窗已经是平台明确回执，关闭失败不能把数据库状态倒退成
        # “复议提交失败”，否则下次可能重复提交；只记录警告，下一单开始
        # 时 clear_blockers 仍会继续尝试清理遮挡层。
        print(f"订单 {order_no} 已确认提交复议成功，但关闭成功弹窗失败：{exc}")


def choose_reconsider_reason(classification: str) -> str:
    """只从用户确认的三个原因中选择；未知/被排除项统一落到“其他”。"""
    reason = CLASSIFICATION_REASON_MAP.get(classification, classification.strip())
    if reason in EXCLUDED_REASONS or reason not in ALLOWED_REASONS:
        return "其他"
    return reason


def countdown_to_deadline(text: str, observed_at: datetime | None = None) -> str:
    """把页面倒计时转换成可排序的本地 ISO 截止时间；无法解析返回空串。"""
    compact = re.sub(r"\s+", "", text)
    values: dict[str, int] = {}
    for name, suffix in (
        ("days", "天"),
        ("hours", "时"),
        ("minutes", "分"),
        ("seconds", "秒"),
    ):
        match = re.search(rf"(\d+){suffix}", compact)
        values[name] = int(match.group(1)) if match else 0
    if not any(values.values()):
        return ""
    base = observed_at or datetime.now().astimezone()
    return (
        base
        + timedelta(
            days=values["days"],
            hours=values["hours"],
            minutes=values["minutes"],
            seconds=values["seconds"],
        )
    ).isoformat(timespec="seconds")


def make_record_key(order_no: str, punished_at: str, issue_type: str) -> str:
    """同一订单可能存在多条处罚记录，处罚时间和问题类型共同区分。"""
    return f"{punished_at or '未知处罚时间'}|{issue_type or '未知问题类型'}"


def _clean_text(locator: Locator) -> str:
    try:
        return re.sub(r"\s+", " ", locator.inner_text()).strip()
    except Exception:
        return ""


def extract_reconsideration_rows(page: Page) -> list[dict[str, str]]:
    """读取当前已经渲染出的所有可复议行，忽略末尾加载提示行。"""
    result: list[dict[str, str]] = []
    rows = page.locator('tr[data-testid="beast-core-table-body-tr"]')
    for index in range(rows.count()):
        row = rows.nth(index)
        cells = row.locator('td[data-testid="beast-core-table-td"]')
        if cells.count() < 7:
            continue

        order_link = row.locator('[data-tracking-viewid="order_en_shared"]')
        order_text = (
            _clean_text(order_link.first)
            if order_link.count()
            else _clean_text(cells.nth(0))
        )
        order_match = _ORDER_RE.search(order_text)
        if order_match is None:
            continue
        order_no = order_match.group(0)

        goods_cell = cells.nth(1)
        goods_title = _clean_text(goods_cell)
        image = goods_cell.locator("img")
        image_url = image.first.get_attribute("src") or "" if image.count() else ""
        issue_type = _clean_text(cells.nth(2))
        amount_text = _clean_text(cells.nth(3))
        amount_match = _AMOUNT_RE.search(amount_text)
        punished_text = _clean_text(cells.nth(4))
        punished_match = _TIMESTAMP_RE.search(punished_text)
        countdown_text = _clean_text(cells.nth(5))

        punished_at = punished_match.group(0) if punished_match else punished_text
        item = {
            "order_no": order_no,
            "issue_type": issue_type,
            "goods_title": goods_title,
            "goods_image_url": image_url,
            "penalty_amount": amount_match.group(0) if amount_match else "",
            "punished_at": punished_at,
            "countdown_text": countdown_text,
            "deadline_at": countdown_to_deadline(countdown_text),
        }
        item["record_key"] = make_record_key(order_no, punished_at, issue_type)
        result.append(item)
    return result


def open_reconsideration_page(page: Page) -> None:
    # 复议任务会长期保留这一标签页。已经在目标页时只校验页面，不再为
    # 每个订单重复 goto；页面被登录页/风控页带走时才重新导航恢复。
    navigated = "negativeExperienceReconsider" not in page.url
    if navigated:
        page.goto(RECONSIDERATION_URL, wait_until="domcontentloaded", timeout=60_000)
    clear_blockers(page)
    page.locator('[data-testid="beast-core-table"]').wait_for(
        state="visible", timeout=30_000
    )
    if navigated:
        business_read_page(page)


def _scroll_reconsideration_container(page: Page) -> dict:
    """把鼠标移到真实滚动容器，再使用分段滚轮事件向下浏览。"""
    info = page.evaluate(
        """
        () => {
            document.querySelectorAll('[data-pdd-reconsider-scroller]')
                .forEach(node => node.removeAttribute(
                    'data-pdd-reconsider-scroller'
                ));
            const body = document.querySelector(
                '[data-testid="beast-core-table-middle-body"]'
            );
            let node = body;
            let target = null;
            while (node) {
                const style = getComputedStyle(node);
                if (node.scrollHeight > node.clientHeight + 4
                    && ['auto', 'scroll', 'overlay'].includes(style.overflowY)) {
                    target = node;
                    break;
                }
                node = node.parentElement;
            }
            target = target || document.scrollingElement || document.documentElement;
            const before = target.scrollTop;
            target.setAttribute('data-pdd-reconsider-scroller', 'true');
            return {
                before,
                clientHeight: target.clientHeight,
                scrollHeight: target.scrollHeight,
            };
        }
        """
    )
    target = page.locator('[data-pdd-reconsider-scroller="true"]')
    box = target.bounding_box()
    if box:
        viewport = page.viewport_size or {
            "width": int(page.evaluate("window.innerWidth")),
            "height": int(page.evaluate("window.innerHeight")),
        }
        target_x = box["x"] + box["width"] * random.uniform(0.35, 0.65)
        target_y = box["y"] + box["height"] * random.uniform(0.35, 0.65)
        page.mouse.move(
            min(max(8, target_x), viewport["width"] - 8),
            min(max(8, target_y), viewport["height"] - 8),
        )
    distance = max(240, int(info["clientHeight"] * random.uniform(0.68, 0.84)))
    human_scroll(page, distance, 4, 9)
    human_delay(350, 950)
    info["after"] = page.evaluate(
        "document.querySelector('[data-pdd-reconsider-scroller]').scrollTop"
    )
    target.evaluate(
        "node => node.removeAttribute('data-pdd-reconsider-scroller')"
    )
    return info


def scan_all_reconsiderable_orders(
    page: Page,
    shop: str,
    on_progress: Callable[[int, int], None] | None = None,
    max_rounds: int = 300,
    should_stop: Callable[[], bool] | None = None,
    early_stop_count: int | None = None,
    excluded_keys: set[tuple[str, str]] | None = None,
    allow_partial_at_limit: bool = False,
) -> list[dict[str, str]]:
    """滚动扫描；候选池足够时可提前停止，无需加载完整历史列表。"""
    _raise_if_stopped(should_stop, "开始扫描前")
    open_reconsideration_page(page)
    seen: dict[tuple[str, str], dict[str, str]] = {}
    excluded_keys = excluded_keys or set()
    no_progress_rounds = 0

    for round_no in range(1, max_rounds + 1):
        _raise_if_stopped(should_stop, "扫描可复议列表")
        before = len(seen)
        current_rows = extract_reconsideration_rows(page)
        for item in current_rows:
            key = (item["order_no"], item["record_key"])
            seen[key] = item
            db.record_reconsideration_discovered(shop, **item)

        if on_progress is not None:
            on_progress(round_no, len(seen))

        eligible_count = sum(key not in excluded_keys for key in seen)
        if early_stop_count is not None and eligible_count >= early_stop_count:
            print(
                f"可复议候选池已达到 {eligible_count} 条，"
                f"满足提前停止阈值 {early_stop_count} 条，不再扫描完整列表。"
            )
            return list(seen.values())

        if (
            not current_rows
            and page.get_by_text("暂无可复议订单", exact=True).count() > 0
        ):
            print(f"可复议列表明确显示暂无可复议订单，扫描完成，共 {len(seen)} 条。")
            return list(seen.values())

        if page.get_by_text("没有更多了", exact=True).count() > 0:
            print(f"可复议列表扫描完成，共 {len(seen)} 条，页面已显示“没有更多了”。")
            return list(seen.values())

        if len(seen) == before:
            no_progress_rounds += 1
        else:
            no_progress_rounds = 0

        clear_blockers(page)
        _scroll_reconsideration_container(page)
        jitter_wait(page, 1300, 0.45)
        _raise_if_stopped(should_stop, "滚动加载可复议列表")

        # 列表通过滚动自动加载；加载提示会随异步刷新消失，不能把它当
        # 按钮点击。下一轮重新读取订单，并继续检查是否已到列表末尾。

        # 网络或前端卡住时不能把“没变化”误判为“没有更多”。多给几次带
        # 随机停顿的重试，仍无进展就留现场、抛错，等待下次任务重新扫描。
        if no_progress_rounds >= 5:
            shot = save_error_screenshot(page, f"reconsider_scan_{shop}")
            suffix = f"，现场截图：{to_relative_evidence_path(shot)}" if shot else ""
            raise RuntimeError(
                "复议列表连续多次没有新增订单，且页面没有出现“没有更多了”" + suffix
            )

    if allow_partial_at_limit:
        print(
            f"复议列表已完成有限扫描 {max_rounds} 轮，"
            f"使用当前发现的 {len(seen)} 条记录建立候选队列。"
        )
        return list(seen.values())
    raise RuntimeError(f"复议列表滚动超过安全上限 {max_rounds} 轮，已停止。")


def _find_row_for_task(page: Page, task: dict) -> Locator | None:
    rows = page.locator('tr[data-testid="beast-core-table-body-tr"]')
    fallback = None
    for index in range(rows.count()):
        row = rows.nth(index)
        text = _clean_text(row)
        if task["order_no"] not in text:
            continue
        if fallback is None:
            fallback = row
        punished_at = task.get("punished_at") or ""
        issue_type = task.get("issue_type") or ""
        if (not punished_at or punished_at in text) and (
            not issue_type or issue_type in text
        ):
            return row
    return fallback


def search_reconsideration_order(page: Page, task: dict) -> Locator | None:
    open_reconsideration_page(page)
    search_input = page.locator(
        'input[data-testid="beast-core-input-htmlInput"][placeholder="请输入订单编号"]'
    )
    search_input.wait_for(state="visible", timeout=15_000)
    safe_click(page, search_input, "复议订单编号输入框")
    human_delay(120, 320)
    search_input.press("Control+A")
    human_delay(80, 220)
    search_input.press("Backspace")
    human_delay(100, 260)

    # React 受控输入框偶尔会漏掉模拟快捷键产生的更新；先检查实际 value，
    # 未清空时再用 fill 兜底，并且在输入新订单号前强制校验为空。
    if (search_input.input_value() or "").strip():
        search_input.fill("")
    if (search_input.input_value() or "").strip():
        raise RuntimeError("复议订单编号输入框清空失败，已停止本单查询。")

    target_order_no = task["order_no"]
    human_type(
        page,
        search_input,
        target_order_no,
        click_first=False,
        min_delay=55,
        max_delay=140,
    )
    actual_order_no = (search_input.input_value() or "").strip()
    if actual_order_no != target_order_no:
        raise RuntimeError(
            "复议订单编号输入异常："
            f"期望={target_order_no!r}，实际={actual_order_no!r}。"
        )

    query = page.get_by_text("查询", exact=True)
    if query.count():
        safe_click(page, query.last, "查询可复议订单")
    else:
        search_input.press("Enter")
    jitter_wait(page, 1300, 0.35)
    return _find_row_for_task(page, task)


def _select_reason(page: Page, modal: Locator, reason: str) -> None:
    if reason not in ALLOWED_REASONS or reason in EXCLUDED_REASONS:
        raise ValueError(f"不允许使用的复议原因：{reason}")
    reason_input = modal.locator('input[data-testid="beast-core-cascader-htmlInput"]')
    reason_input.wait_for(state="visible", timeout=10_000)
    stealth_safe_click(page, reason_input, "复议原因下拉框")

    dropdown = page.locator('[data-testid="beast-core-cascader-dropdown-contentRoot"]')
    dropdown.wait_for(state="visible", timeout=8_000)
    options = dropdown.locator('[data-testid="beast-core-cascader-list-item"]')
    target = None
    for index in range(options.count()):
        option = options.nth(index)
        if _clean_text(option) == reason:
            target = option
            break
    if target is None:
        available = [_clean_text(options.nth(i)) for i in range(options.count())]
        raise RuntimeError(f"页面找不到复议原因 {reason!r}；当前选项：{available}")
    stealth_safe_click(page, target, f"复议原因：{reason}")

    selected = (reason_input.input_value() or "").strip()
    if selected != reason:
        raise RuntimeError(
            f"复议原因点击后未正确选中：期望={reason!r}，实际={selected!r}"
        )


def _upload_original_evidence(
    page: Page, modal: Locator, files: list[Path]
) -> list[Path]:
    if not files:
        raise RuntimeError("没有可复用的首次申诉凭证图片。")
    selected = files[:3]
    file_input = modal.locator(
        "#reconsiderVoucher "
        'input[data-testid="beast-core-upload-input"][accept*=".jpg"]'
    )
    file_input.wait_for(state="attached", timeout=10_000)
    if file_input.count() != 1:
        raise RuntimeError(f"复议图片上传控件数量异常：{file_input.count()}")

    business_pause(page, reading=True)
    resolved = [str(path.resolve()) for path in selected]
    if file_input.get_attribute("multiple") is not None:
        file_input.set_input_files(resolved)
    else:
        for path in resolved:
            business_pause(page)
            file_input.set_input_files(path)
            jitter_wait(page, 650, 0.35)
    jitter_wait(page, 2200, 0.35)
    return selected


def _save_form_screenshot(
    page: Page, modal: Locator, order_no: str, record_key: str
) -> str | None:
    stamp = safe_filename(record_key)[-40:]
    path = DEBUG_DIR / f"reconsider_form_filled_{safe_filename(order_no)}_{stamp}.png"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        save_natural_content_screenshot(page, modal, path)
        return to_relative_evidence_path(path)
    except Exception:
        return None


def prepare_reconsideration_material(
    page: Page,
    shop: str,
    task: dict,
    *,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[str, str, list[Path]]:
    """复用原始聊天和凭证，按复议专用风格生成文案。"""
    _raise_if_stopped(should_stop, "准备复议材料前")
    order_no = task["order_no"]
    order = db.get_order(shop, order_no)
    files = collect_evidence_files(order_no)
    if (
        not order
        or not order.get("classification")
        or not order.get("appeal_text")
        or order.get("messages") is None
        or not files
    ):
        db.update_reconsideration_status(
            shop,
            order_no,
            task["record_key"],
            "待准备材料",
            error_message=None,
        )
        db.record_collected(
            shop,
            order_no,
            issue_type=task.get("issue_type") or "",
            goods_title=task.get("goods_title") or "",
            goods_image_url=task.get("goods_image_url") or "",
            compensation_amount=task.get("penalty_amount") or "",
        )
        _raise_if_stopped(should_stop, "采集聊天记录前")
        page.bring_to_front()
        process_single_order(page, order_no, task.get("issue_type") or "")
        order = db.get_order(shop, order_no)
        files = collect_evidence_files(order_no)

    if not order or not order.get("classification") or not order.get("appeal_text"):
        raise RuntimeError("首次申诉材料生成后仍缺少分类或申诉文案。")
    if not files:
        raise RuntimeError("首次申诉材料生成后仍没有有效凭证图片。")

    reason = choose_reconsider_reason(str(order["classification"]))
    if order.get("messages") is None:
        raise RuntimeError("缺少原始聊天记录，不能根据旧文案推断复议事实。")
    text = generate_reconsideration_description(
        compact_chat_json({"order_no": order_no, "messages": order["messages"]}),
        reason,
    )
    selected_files = files[:2]
    relative_paths = [
        str(path.resolve().relative_to(BASE_DIR.resolve())).replace("\\", "/")
        for path in selected_files
    ]
    db.update_reconsideration_status(
        shop,
        order_no,
        task["record_key"],
        "待发起复议",
        reconsider_reason=reason,
        reconsider_text=text,
        evidence_paths=relative_paths,
        error_message=None,
    )
    # 生成中的外部请求不能安全地从另一个线程强行打断；返回后先完整保存
    # 已生成的材料，再响应停止，避免下次启动重复采集和生成。
    _raise_if_stopped(should_stop, "复议材料准备完成")
    return reason, text, selected_files


def _stop_before_submit(
    page: Page,
    modal: Locator,
    shop: str,
    order_no: str,
    record_key: str,
) -> None:
    """提交按钮尚未点击时，关闭弹窗并把订单恢复为可继续状态。"""
    try:
        cancel = modal.get_by_role("button", name="取消", exact=True)
        if cancel.count() and cancel.first.is_visible():
            stealth_safe_click(page, cancel.first, "停止复议并关闭填写弹窗")
    except Exception:
        pass
    db.update_reconsideration_status(
        shop,
        order_no,
        record_key,
        "待发起复议",
        error_message=None,
    )
    raise ReconsiderationStopped("提交复议前")


@paced_business_operations
def submit_reconsideration_task(
    page: Page,
    shop: str,
    task: dict,
    reason: str,
    text: str,
    files: list[Path],
    *,
    confirm: bool,
    should_stop: Callable[[], bool] | None = None,
) -> str:
    """搜索并处理一条复议；confirm=False 只填表截图后取消。"""
    order_no = task["order_no"]
    record_key = task["record_key"]
    _raise_if_stopped(should_stop, "查询复议订单前")
    page.bring_to_front()
    row = search_reconsideration_order(page, task)
    _raise_if_stopped(should_stop, "查询复议订单后")
    if row is None:
        raise RuntimeError("扫描后再次按订单编号查询时，页面已找不到该可复议记录。")

    launch = row.locator('[data-tracking-viewid="launch_appeal_shared"]')
    if not launch.count():
        launch = row.get_by_text("发起复议", exact=True)
    if launch.count() != 1:
        raise RuntimeError(f"发起复议按钮定位异常：匹配数量={launch.count()}")

    db.update_reconsideration_status(shop, order_no, record_key, "复议填写中")
    safe_click(page, launch, f"订单 {order_no} 发起复议")
    modal = (
        page.locator('[data-testid="beast-core-modal-innerWrapper"]')
        .filter(has_text="发起复议")
        .last
    )
    modal.wait_for(state="visible", timeout=15_000)
    business_pause(page, reading=True)
    modal_text = _clean_text(modal)
    if order_no not in modal_text:
        raise RuntimeError(f"复议弹窗订单校验失败：目标订单 {order_no} 不在弹窗中。")

    if should_stop is not None and should_stop():
        _stop_before_submit(page, modal, shop, order_no, record_key)

    _select_reason(page, modal, reason)
    if should_stop is not None and should_stop():
        _stop_before_submit(page, modal, shop, order_no, record_key)
    uploaded = _upload_original_evidence(page, modal, files)
    if should_stop is not None and should_stop():
        _stop_before_submit(page, modal, shop, order_no, record_key)
    textarea = modal.locator(
        '#reconsiderDesc textarea[data-testid="beast-core-textArea-htmlInput"]'
    )
    if not textarea.count():
        textarea = modal.locator(
            'textarea[data-testid="beast-core-textArea-htmlInput"]'
        )
    textarea.wait_for(state="visible", timeout=10_000)
    paste_text(page, textarea, text[:300])

    if should_stop is not None and should_stop():
        _stop_before_submit(page, modal, shop, order_no, record_key)

    form_shot = _save_form_screenshot(page, modal, order_no, record_key)
    relative_files = [
        str(path.resolve().relative_to(BASE_DIR.resolve())).replace("\\", "/")
        for path in uploaded
    ]

    if not confirm:
        cancel = modal.get_by_role("button", name="取消", exact=True)
        stealth_safe_click(page, cancel, "取消复议模拟提交")
        db.update_reconsideration_status(
            shop,
            order_no,
            record_key,
            "待发起复议",
            reconsider_reason=reason,
            reconsider_text=text,
            evidence_paths=relative_files,
            form_screenshot_path=form_shot,
        )
        return "模拟填写完成"

    if should_stop is not None and should_stop():
        _stop_before_submit(page, modal, shop, order_no, record_key)

    business_pause(page, reading=True)
    if should_stop is not None and should_stop():
        _stop_before_submit(page, modal, shop, order_no, record_key)
    submit = modal.get_by_role("button", name="提交复议", exact=True)
    if submit.count() != 1:
        raise RuntimeError(f"提交复议按钮定位异常：匹配数量={submit.count()}")
    # 先落“已点击前的最后安全边界”状态。即使进程在点击后、收到页面结果前
    # 崩溃，下次运行也不会把这条订单自动再点一遍。
    db.update_reconsideration_status(
        shop,
        order_no,
        record_key,
        "复议提交确认中",
        reconsider_reason=reason,
        reconsider_text=text,
        evidence_paths=relative_files,
        form_screenshot_path=form_shot,
    )
    stealth_safe_click(page, submit, "提交复议")
    success_modal = (
        page.locator('[data-testid="beast-core-modal-innerWrapper"]')
        .filter(has_text="提交复议成功")
        .last
    )
    # 同时观察验证码和成功弹窗。成功弹窗先出现时立即继续，不再固定空等
    # 10秒；验证码先出现时仍复用首次申诉的自动处理流程。
    success_visible = wait_for_submit_captcha(
        page,
        success_locator=success_modal,
    )
    if not success_visible:
        try:
            success_modal.wait_for(state="visible", timeout=30_000)
        except PlaywrightTimeoutError:
            db.update_reconsideration_status(
                shop,
                order_no,
                record_key,
                "复议结果待确认",
                reconsider_reason=reason,
                reconsider_text=text,
                evidence_paths=relative_files,
                form_screenshot_path=form_shot,
                error_message="点击提交复议后未看到“提交复议成功”，未自动重复提交。",
            )
            return "复议结果待确认"

    # 只有明确看到平台的成功弹窗，才落“已提交复议”。先写库再关闭弹窗，
    # 即便关闭动作意外失败，也不会在下次运行时把已成功的订单重复提交。
    db.update_reconsideration_status(
        shop,
        order_no,
        record_key,
        "已提交复议",
        reconsider_reason=reason,
        reconsider_text=text,
        evidence_paths=relative_files,
        form_screenshot_path=form_shot,
        error_message=None,
    )

    _close_reconsideration_success_modal(page, success_modal, order_no)

    return "已提交复议"


def collect_pending_reconsideration_tasks(
    reconsideration_page: Page,
    shop: str,
    *,
    max_orders: int = 20,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> list[dict[str, str]]:
    """扫描并建立本次待复议队列，按截止时间优先且应用单店数量上限。"""
    if max_orders <= 0:
        raise ValueError("每家店铺的复议数量必须是正整数。")
    reconsideration_page.bring_to_front()
    existing_records = {
        (row["order_no"], row["record_key"]): row
        for row in db.list_reconsideration_records(shop)
    }
    excluded_keys = {
        key
        for key, record in existing_records.items()
        if record.get("status") in RECONSIDERATION_FINISHED_STATUSES
    }
    # 多扫描一倍候选，为截止时间排序和页面偶发重复/失效记录留余量。
    # 默认最多滚动 8 轮，防止数百条历史记录让扫描耗时无限增长。
    candidate_target = max(max_orders * 2, max_orders + 20)
    scanned = scan_all_reconsiderable_orders(
        reconsideration_page,
        shop,
        on_progress=(
            (lambda round_no, count: on_progress("扫描", round_no, count))
            if on_progress
            else None
        ),
        should_stop=should_stop,
        early_stop_count=candidate_target,
        excluded_keys=excluded_keys,
        max_rounds=8,
        allow_partial_at_limit=True,
    )
    scanned.sort(key=lambda item: item.get("deadline_at") or "9999")
    pending_tasks = [
        task
        for task in scanned
        if existing_records.get(
            (task["order_no"], task["record_key"]), {}
        ).get("status")
        not in RECONSIDERATION_FINISHED_STATUSES
    ][:max_orders]
    print(
        f"店铺 {shop} 本次最多处理 {max_orders} 条复议；"
        f"当前选中 {len(pending_tasks)} 条待处理记录。"
    )
    return pending_tasks


@paced_business_operations
def process_reconsideration_task(
    reconsideration_page: Page,
    chat_page: Page,
    shop: str,
    task: dict[str, str],
    *,
    confirm: bool,
    current: int,
    total: int,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> dict[str, str]:
    """只处理一条复议记录，供一次性流程和多店铺轮转协议共同复用。"""
    _raise_if_stopped(should_stop, "开始处理复议订单前")
    if on_progress:
        on_progress("处理", current, total)

    try:
        reason, text, files = prepare_reconsideration_material(
            chat_page,
            shop,
            task,
            should_stop=should_stop,
        )
        result = submit_reconsideration_task(
            reconsideration_page,
            shop,
            task,
            reason,
            text,
            files,
            confirm=confirm,
            should_stop=should_stop,
        )
        return {"order_no": task["order_no"], "result": result}
    except ReconsiderationStopped:
        raise
    except Exception as exc:
        error_shot = None
        try:
            active_page = (
                reconsideration_page
                if "negativeExperienceReconsider" in reconsideration_page.url
                else chat_page
            )
            shot = save_error_screenshot(
                active_page, f"reconsider_{task['order_no']}"
            )
            error_shot = to_relative_evidence_path(shot) if shot else None
        except Exception:
            pass
        db.update_reconsideration_status(
            shop,
            task["order_no"],
            task["record_key"],
            "复议提交失败",
            error_message=str(exc),
            error_screenshot_path=error_shot,
        )
        return {"order_no": task["order_no"], "result": f"失败：{exc}"}


def run_reconsiderations(
    reconsideration_page: Page,
    chat_page: Page,
    shop: str,
    *,
    confirm: bool,
    max_orders: int = 20,
    on_progress: Callable[[str, int, int], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
) -> tuple[list[dict[str, str]], bool]:
    """扫描后按截止时间逐单处理；订单间加入较长随机间隔。"""
    try:
        pending_tasks = collect_pending_reconsideration_tasks(
            reconsideration_page,
            shop,
            max_orders=max_orders,
            on_progress=on_progress,
            should_stop=should_stop,
        )
    except ReconsiderationStopped:
        return [], True

    outcomes: list[dict[str, str]] = []

    for index, task in enumerate(pending_tasks, start=1):
        if should_stop is not None and should_stop():
            return outcomes, True

        if index > 1:
            human_delay(3500, 7500)
            if should_stop is not None and should_stop():
                return outcomes, True

        try:
            outcome = process_reconsideration_task(
                reconsideration_page,
                chat_page,
                shop,
                task,
                confirm=confirm,
                current=index,
                total=len(pending_tasks),
                on_progress=on_progress,
                should_stop=should_stop,
            )
            outcomes.append(outcome)
            # 点击提交之后必须先完成验证码与页面确认。确认完成后才响应
            # 停止，不会中断在“平台可能已经收单、本地却不知道”的状态。
            if should_stop is not None and should_stop():
                return outcomes, True
        except ReconsiderationStopped:
            return outcomes, True

    return outcomes, False
