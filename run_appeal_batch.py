from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from stealth import create_stealth_context, human_delay, jitter_wait

from appeal_classifier import (
    NO_CHAT_RECORD_APPEAL_TEXT,
    NO_CHAT_RECORD_LABEL,
    classify_appeal_evidence,
    compact_chat_json,
    generate_appeal_text,
)
from blockers import clear_blockers, safe_click
from login import open_chat_page, wait_for_login
from paths import OUTPUT_DIR, PROFILE_DIR
from pdd_chat import (
    click_next_page,
    query_chat_records,
    save_chat_records,
)
from screenshots import save_error_screenshot


# ============================================================
# 基础配置
# ============================================================

APPEAL_LIST_URL = (
    "https://mms.pinduoduo.com/aftersales/"
    "customer_complain_appeal?from=list"
)


# ============================================================
# 申诉列表页
# ============================================================

def open_appeal_list_page(page: Page) -> None:
    """进入拼多多客诉申诉列表页。"""

    print("正在进入客诉申诉列表页……")

    try:
        page.goto(
            APPEAL_LIST_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )


    except PlaywrightTimeoutError:
        print("页面导航超过 60 秒，继续检查页面内容。")

    # 给风控弹窗留出注入时间再检查
    jitter_wait(page, 1_500)

    clear_blockers(page)


def get_view_appealable_orders_link(page: Page) -> Locator:
    """获取"查看可申诉订单"链接。"""

    return page.locator(
        'a[data-testid="beast-core-button-link"]'
    ).filter(
        has_text="查看可申诉订单"
    )


def open_appealable_orders_table(page: Page) -> None:
    """点击"查看可申诉订单"，确保点击真正生效、订单表格数据渲染出来后再返回。"""

    link = get_view_appealable_orders_link(page)

    link.wait_for(
        state="visible",
        timeout=30_000,
    )

    # 点击前先记下当前表格内容签名。仅凭点击后"表格容器可见"来判断
    # 点击是否生效并不可靠：点击可能被浮层/动画吞掉，而页面上恰好
    # 已经有一个匹配同样选择器的（旧）表格。这里改成确认表格内容
    # 确实发生了变化，才认为点击真正生效，不生效则重试一次。
    before_signature = "|".join(
        extract_order_numbers_from_current_page(page)
    )

    table = page.locator(
        '[data-testid="beast-core-table"]'
    )

    for attempt in (1, 2):
        safe_click(
            page=page,
            locator=link,
            description="查看可申诉订单",
        )

        table.wait_for(
            state="visible",
            timeout=30_000,
        )

        if wait_for_orders_table_updated(
            page=page,
            previous_signature=before_signature,
        ):
            print('已点击"查看可申诉订单"，表格数据已确认更新。')
            return

        print(
            f'第 {attempt} 次点击"查看可申诉订单"后，'
            "表格内容 20 秒内没有变化。"
        )

    print(
        '点击"查看可申诉订单"后表格内容始终未发生变化，'
        "可能点击未生效，也可能当前确实没有可申诉订单，"
        "继续尝试采集订单编号。"
    )


# ============================================================
# 订单编号提取（支持翻页）
# ============================================================

# 订单问题类型常量
ISSUE_TYPE_GOODS = "商品问题"
ISSUE_TYPE_SERVICE = "服务问题"


def extract_order_info_from_current_page(page: Page) -> list[dict[str, str]]:
    """提取当前这一页表格里的订单编号（第一列）和问题类型（第三列）。

    返回形如 [{"order_no": "260805-...", "issue_type": "服务问题"}, ...] 的列表。
    issue_type 值为 "商品问题" / "服务问题" / ""（识别不到时空字符串）。
    """

    rows = page.locator(
        'tbody[data-testid="beast-core-table-middle-tbody"] tr'
    )

    if rows.count() == 0:
        return []

    return rows.evaluate_all(
        """
        (trs) => {
            return trs.map((tr) => {
                const cells = tr.querySelectorAll(
                    'td[data-testid="beast-core-table-td"]'
                );

                const orderNo = (cells[0]?.textContent || "").trim();
                const issueType = (cells[2]?.textContent || "").trim();

                return { order_no: orderNo, issue_type: issueType };
            });
        }
        """
    )


def extract_order_numbers_from_current_page(page: Page) -> list[str]:
    """向后兼容：提取当前页订单编号（仅第一列）。

    推荐使用 extract_order_info_from_current_page() 以同时获取问题类型。
    """
    return [info["order_no"] for info in extract_order_info_from_current_page(page)]


def wait_for_orders_table_updated(
    page: Page,
    previous_signature: str,
) -> bool:
    """等待订单表格内容相对 previous_signature 发生变化。

    用于确认"点击生效"：无论是首次点开可申诉订单表格，
    还是翻页，点击后表格内容都应该和点击前不一样。
    """

    try:
        page.wait_for_function(
            """
            (previousSignature) => {
                const rows = Array.from(
                    document.querySelectorAll(
                        'tbody[data-testid="beast-core-table-middle-tbody"] tr'
                    )
                );

                if (rows.length === 0) {
                    return false;
                }

                const currentSignature = rows
                    .map((tr) => {
                        const firstCell = tr.querySelector(
                            'td[data-testid="beast-core-table-td"]'
                        );

                        return (firstCell?.textContent || "").trim();
                    })
                    .join("|");

                return currentSignature !== previousSignature;
            }
            """,
            arg=previous_signature,
            timeout=20_000,
        )

        jitter_wait(page, 500)

        return True

    except PlaywrightTimeoutError:
        return False


def collect_all_appealable_order_numbers(
    page: Page,
    max_pages: int = 50,
) -> tuple[list[str], dict[str, str]]:
    """翻页收集所有可申诉订单编号及问题类型。

    返回：
        order_numbers — 订单号列表（保持原有顺序）
        order_issues  — order_no → issue_type 映射（"商品问题"/"服务问题"）
    """

    all_order_numbers: list[str] = []
    all_order_issues: dict[str, str] = {}
    visited_signatures: set[str] = set()

    current_page = 1

    while current_page <= max_pages:
        page_infos = extract_order_info_from_current_page(page)
        page_order_numbers = [info["order_no"] for info in page_infos]

        current_signature = "|".join(page_order_numbers)

        if not current_signature:
            print(
                f"第 {current_page} 页没有获取到订单编号，停止翻页。"
            )
            break

        if current_signature in visited_signatures:
            print("检测到重复页面，停止翻页，避免死循环。")
            break

        visited_signatures.add(current_signature)

        # 收集问题类型映射
        for info in page_infos:
            oid = info["order_no"]
            itype = info["issue_type"]
            if itype:
                all_order_issues[oid] = itype

        print(
            f"第 {current_page} 页发现 "
            f"{len(page_order_numbers)} 个可申诉订单。"
        )

        all_order_numbers.extend(page_order_numbers)

        # 先清障再翻页：翻页点击在 JS 内部完成，不会因遮挡而报错，
        # 但翻页后的数据刷新会被风控拦住。
        clear_blockers(page)

        next_result = click_next_page(page)

        if not next_result.get("clicked"):
            print(
                "停止翻页："
                f'{next_result.get("reason", "未知原因")}'
            )
            break

        if not wait_for_orders_table_updated(
            page=page,
            previous_signature=current_signature,
        ):
            print(
                "点击下一页后订单表格没有发生变化，停止翻页。"
            )
            break

        current_page += 1

    else:
        print(
            f"已经达到最大翻页限制 {max_pages} 页，停止继续采集。"
        )

    print(f"共发现 {len(all_order_numbers)} 个可申诉订单。")

    return all_order_numbers, all_order_issues


def save_appealable_order_numbers(
    order_numbers: list[str],
    order_issues: dict[str, str],
    run_id: str,
) -> Path:
    """把本次发现的可申诉订单编号及问题类型存档。"""

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    output_file = (
        OUTPUT_DIR / f"appealable_orders_{run_id}.json"
    )

    result = {
        "run_id": run_id,
        "order_count": len(order_numbers),
        "order_numbers": order_numbers,
        "order_issues": order_issues,
    }

    output_file.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return output_file


# ============================================================
# 单个订单处理
# ============================================================

def process_single_order(
    page: Page,
    order_no: str,
    issue_type: str = "",
) -> tuple[str, str]:
    """
    处理单个订单：查询聊天记录、保存、判断申诉凭证类型、生成申诉文案。

    返回值用于最后打印汇总表：(申诉凭证分类结果, 申诉文案)。
    没有聊天记录时，不经过大模型判断，固定返回
    (NO_CHAT_RECORD_LABEL, NO_CHAT_RECORD_APPEAL_TEXT)。

    issue_type 为 "商品问题" 或 "服务问题"，将被写入 chat JSON 供后续步骤使用。
    """

    open_chat_page(page)

    records, no_chat_screenshot = query_chat_records(
        page=page,
        order_no=order_no,
    )

    output_file = save_chat_records(
        order_no=order_no,
        records=records,
        no_chat_record_screenshot=no_chat_screenshot,
        issue_type=issue_type,
    )

    print(f"订单 {order_no} 聊天记录已保存到：{output_file}")

    if not records:
        print(
            f"订单 {order_no} 没有聊天记录，"
            "不经过大模型判断，使用固定申诉文案。"
        )

        data = json.loads(
            output_file.read_text(encoding="utf-8")
        )

        data["appeal_evidence_classification"] = NO_CHAT_RECORD_LABEL
        data["appeal_text"] = NO_CHAT_RECORD_APPEAL_TEXT

        output_file.write_text(
            json.dumps(
                data,
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        print(
            f"订单 {order_no} 分类结果：{NO_CHAT_RECORD_LABEL}"
            "（无聊天记录，未经过AI判断）"
        )
        print(f"订单 {order_no} 申诉文案：{NO_CHAT_RECORD_APPEAL_TEXT}")

        return NO_CHAT_RECORD_LABEL, NO_CHAT_RECORD_APPEAL_TEXT

    data = json.loads(
        output_file.read_text(encoding="utf-8")
    )

    compact_data = compact_chat_json(data)

    label = classify_appeal_evidence(compact_data)

    data["appeal_evidence_classification"] = label

    appeal_text = generate_appeal_text(compact_data, label)

    data["appeal_text"] = appeal_text

    output_file.write_text(
        json.dumps(
            data,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(f"订单 {order_no} 分类结果：{label}")
    print(f"订单 {order_no} 申诉文案：{appeal_text}")

    return label, appeal_text


# ============================================================
# 批量入口
# ============================================================

def main() -> None:
    PROFILE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    with sync_playwright() as playwright:
        context = create_stealth_context(
            playwright,
            user_data_dir=str(PROFILE_DIR),
        )

        page: Page | None = None
        results: list[tuple[str, str, str]] = []

        try:
            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            page = wait_for_login(
                context=context,
                page=page,
            )

            open_appeal_list_page(page)
            open_appealable_orders_table(page)

            order_numbers, order_issues = (
                collect_all_appealable_order_numbers(page)
            )

            saved_file = save_appealable_order_numbers(
                order_numbers=order_numbers,
                order_issues=order_issues,
                run_id=run_id,
            )

            print(f"可申诉订单编号已存档到：{saved_file}")

            if not order_numbers:
                print("没有发现可申诉的订单，结束。")
                return

            for order_no in order_numbers:
                # 批量化订单间加入随机的思考/等待间隔（1~3 秒），
                # 避免连续机械查询被风控标记
                if order_no != order_numbers[0]:
                    human_delay(1000, 3000)

                print()
                print("=" * 70)
                print(f"开始处理订单：{order_no}")
                print("=" * 70)

                try:
                    label, appeal_text = process_single_order(
                        page=page,
                        order_no=order_no,
                        issue_type=order_issues.get(order_no, ""),
                    )

                    results.append((order_no, label, appeal_text))

                except Exception as error:
                    print(
                        f"订单 {order_no} 处理失败："
                        f"{type(error).__name__}: {error}"
                    )

                    if page is not None and not page.is_closed():
                        save_error_screenshot(
                            page=page,
                            order_no=order_no,
                        )

                    results.append(
                        (order_no, f"失败：{error}", "")
                    )

            print()
            print("=" * 70)
            print("批量处理结果汇总")
            print("=" * 70)

            for order_no, label, appeal_text in results:
                if appeal_text:
                    print(f"{order_no}：{label} | {appeal_text}")
                else:
                    print(f"{order_no}：{label}")

            input("操作完成，按回车关闭浏览器：")

        finally:
            context.close()


if __name__ == "__main__":
    main()
