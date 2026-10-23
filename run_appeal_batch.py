from __future__ import annotations


def _bootstrap_shop_env() -> None:
    """从 sys.argv 抓 --shop 写入 os.environ；见 main.py 同名函数说明。"""
    import os
    import sys

    if "--shop" in sys.argv:
        i = sys.argv.index("--shop")
        if i + 1 < len(sys.argv):
            os.environ.setdefault("PDDCHAT_SHOP", sys.argv[i + 1])


_bootstrap_shop_env()


import re

from playwright.sync_api import (
    Locator,
    Page,
    sync_playwright,
)
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
)

import db
from appeal_classifier import (
    NO_CHAT_RECORD_LABEL,
    classify_appeal_evidence,
    compact_chat_json,
    generate_appeal_text,
    get_no_chat_appeal_text,
)
from blockers import clear_blockers, safe_click
from login import open_chat_page, wait_for_login
from paths import PROFILE_DIR, SHOP_ID
from pdd_chat import (
    build_chat_record_dict,
    click_next_page,
    query_chat_records,
)
from screenshots import save_error_screenshot, to_relative_evidence_path
from stealth import create_stealth_context, human_delay, jitter_wait

# ============================================================
# 基础配置
# ============================================================

APPEAL_LIST_URL = (
    "https://mms.pinduoduo.com/aftersales/"
    "customer_complain_appeal?from=list"
)

APPEALABLE_ORDERS_ENTRY_PATTERN = re.compile(
    r"^\s*(?:查看可申诉订单|立即申诉)\s*$"
)
ZERO_APPEALABLE_ORDERS_PATTERN = re.compile(
    r"可申诉订单数\s*0\s*单"
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
    """获取可申诉订单入口，兼容“查看可申诉订单”和“立即申诉”。"""

    return page.locator(
        'a[data-testid="beast-core-button-link"]'
    ).filter(
        has_text=APPEALABLE_ORDERS_ENTRY_PATTERN
    )


def page_reports_zero_appealable_orders(page: Page) -> bool:
    """页面顶部统计区是否明确显示“可申诉订单数 0 单”。

    只认平台明确给出的数字，不把入口缺失、表格空白或页面加载异常擅自
    当成 0 单，避免选择器失效时静默漏掉真实订单。
    """
    try:
        body_text = page.locator("body").inner_text(timeout=5_000)
    except PlaywrightTimeoutError:
        return False
    return ZERO_APPEALABLE_ORDERS_PATTERN.search(body_text) is not None


def open_appealable_orders_table(page: Page) -> bool:
    """点击可申诉订单入口；明确为 0 单时返回 False，否则成功返回 True。"""

    if page_reports_zero_appealable_orders(page):
        print("页面显示可申诉订单数为 0，按暂无可处理订单正常结束。")
        return False

    link = get_view_appealable_orders_link(page)

    try:
        link.wait_for(
            state="visible",
            timeout=30_000,
        )
    except PlaywrightTimeoutError:
        # 统计卡可能比页面主体稍晚渲染；入口等待结束后再确认一次。只有
        # 平台明确给出 0 单才转为空队列，其余情况继续抛出原异常。
        if page_reports_zero_appealable_orders(page):
            print("页面显示可申诉订单数为 0，按暂无可处理订单正常结束。")
            return False
        raise

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
            description="查看可申诉订单/立即申诉",
        )

        table.wait_for(
            state="visible",
            timeout=30_000,
        )

        if wait_for_orders_table_updated(
            page=page,
            previous_signature=before_signature,
        ):
            print("已点击可申诉订单入口，表格数据已确认更新。")
            return True

        print(
            f"第 {attempt} 次点击可申诉订单入口后，"
            "表格内容 20 秒内没有变化。"
        )

    print(
        "点击可申诉订单入口后表格内容始终未发生变化，"
        "可能点击未生效，也可能当前确实没有可申诉订单，"
        "继续尝试采集订单编号。"
    )
    return True


# ============================================================
# 订单编号提取（支持翻页）
# ============================================================

# 订单问题类型常量
ISSUE_TYPE_GOODS = "商品问题"
ISSUE_TYPE_SERVICE = "服务问题"


def extract_order_info_from_current_page(page: Page) -> list[dict[str, str]]:
    """提取当前页订单表格中的订单及商品、补偿、创建时间信息。

    列结构来自可申诉订单表格：订单编号、商品信息、问题类型、补偿金额、
    扣款进度、创建时间、处理状态、操作。选择器优先使用稳定的 data-testid
    和 CSS Modules 类名前缀，不依赖每次发布都可能变化的哈希后缀。
    """

    rows = page.locator(
        'tbody[data-testid="beast-core-table-middle-tbody"] tr'
    )

    if rows.count() == 0:
        return []

    return rows.evaluate_all(
        r"""
        (trs) => {
            return trs.map((tr) => {
                const cells = tr.querySelectorAll(
                    'td[data-testid="beast-core-table-td"]'
                );

                const orderNo = (cells[0]?.textContent || "").trim();
                const goodsCell = cells[1];
                const issueText = (cells[2]?.textContent || "").trim();
                const issueTypeMatch = issueText.match(/商品问题|服务问题/);
                const issueType = issueTypeMatch ? issueTypeMatch[0] : issueText;
                const goodsTitle = (
                    goodsCell?.querySelector('[class*="elli_outerWrapper_"]')
                        ?.textContent || ""
                ).trim();
                const goodsIdText = (
                    goodsCell?.querySelector('[class*="GoodInfo_id__"]')
                        ?.textContent || ""
                ).trim();
                const goodsIdMatch = goodsIdText.match(/\d+/);
                const goodsImageUrl = (
                    goodsCell?.querySelector('img')?.getAttribute('src') || ""
                ).trim();
                const compensationAmount = (
                    cells[3]?.textContent || ""
                ).trim();
                const orderCreatedAt = (
                    cells[5]?.textContent || ""
                ).trim();

                return {
                    order_no: orderNo,
                    issue_type: issueType,
                    goods_title: goodsTitle,
                    goods_id: goodsIdMatch ? goodsIdMatch[0] : "",
                    goods_image_url: goodsImageUrl,
                    compensation_amount: compensationAmount,
                    order_created_at: orderCreatedAt,
                };
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
        ).dispose()

        jitter_wait(page, 500)

        return True

    except PlaywrightTimeoutError:
        return False


def collect_all_appealable_order_numbers(
    page: Page,
    max_pages: int = 1,
    max_orders: int | None = None,
) -> tuple[list[str], dict[str, str], dict[str, dict[str, str]]]:
    """收集可申诉订单编号及问题类型，默认只收集第一页（max_pages=1）。

    每天只处理当天新出现在第一页的订单即可，不用每次都翻遍所有历史
    积压页——想恢复"翻页收集全部"的老行为，调用方显式传大一点的
    max_pages（比如原来的 50）即可，这里不删这个参数，只改默认值。

    max_orders：按订单数量而不是页数设置上限（None 表示不限制，只受
    max_pages 约束）——店铺在数据库里配置了自定义"单次最多处理订单数"
    时，main.py 的 run_phase1() 会把这个数字传进来，同时也会相应放宽
    max_pages（不然 max_orders 再大，翻页也会先被 max_pages=1 的默认值
    拦住，见 run_phase1() 的说明）。两个参数同时生效时谁先触发都会停止
    翻页，行为上取两者中更严格的那个。

    返回：
        order_numbers — 订单号列表（保持原有顺序）
                         （max_orders 生效时精确截断到这个数量，不会因为
                         "最后一页翻多了"而超出，见函数体内注释）
        order_issues  — order_no → issue_type 映射（"商品问题"/"服务问题"）
                         （这个字典不受 max_orders 截断，可能比返回的
                         order_numbers 多几个 key——信息性字段，多几条
                         无害，见函数体内注释）
        order_details — order_no → 商品标题/ID/图片、补偿金额、创建时间映射
    """

    all_order_numbers: list[str] = []
    all_order_issues: dict[str, str] = {}
    all_order_details: dict[str, dict[str, str]] = {}
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

        # 收集问题类型映射——同一订单号在拼多多那边可能对应多条独立的
        # "消费者负向体验"记录（商品问题一条、服务问题一条），出现不同
        # 问题类型时拼接而不是覆盖，避免丢信息。这个字典现在只用于
        # Phase 1 汇总打印和 orders 表 issue_type 这个信息性字段——提交
        # 阶段每条记录真正的问题类型改成直接从搜索结果行里读（见
        # appeal_submit.py 的 get_row_issue_type），不再依赖这个字典做
        # 正确性判断，这里只需要"不丢信息"，不需要精确到能唯一定位每条。
        for info in page_infos:
            oid = info["order_no"]
            itype = info["issue_type"]
            if not itype:
                continue
            existing = all_order_issues.get(oid)
            if not existing:
                all_order_issues[oid] = itype
            elif itype not in existing.split("、"):
                all_order_issues[oid] = f"{existing}、{itype}"

        # orders 表以订单号为粒度。同一订单号在列表出现多条负向体验记录时，
        # 商品通常一致；保留首次出现的详情，并用后续行补齐首次未渲染出的
        # 空字段，避免用空值或较晚记录覆盖前一条信息。
        detail_keys = (
            "goods_title",
            "goods_id",
            "goods_image_url",
            "compensation_amount",
            "order_created_at",
        )
        for info in page_infos:
            oid = info["order_no"]
            details = all_order_details.setdefault(oid, {})
            for key in detail_keys:
                if not details.get(key) and info.get(key):
                    details[key] = info[key]

        print(
            f"第 {current_page} 页发现 "
            f"{len(page_order_numbers)} 个可申诉订单。"
        )

        all_order_numbers.extend(page_order_numbers)

        # 攒够 max_orders 就不用再翻下一页——跟下面 max_pages 的检查一样，
        # 在点击翻页之前判断，不是翻完页再发现超限（那样会有一次浪费的
        # 点击/等待）。max_orders=None（默认）时这个分支永远不触发，对
        # 没配置过的店铺零行为变化。
        if max_orders is not None and len(all_order_numbers) >= max_orders:
            print(
                f"已采集到 {len(all_order_numbers)} 个可申诉订单，"
                f"达到本次处理上限 {max_orders}，停止翻页。"
            )
            break

        # 已经到本次采集的页数上限——在点"下一页"之前就停下来，
        # max_pages=1（默认）时完全不会触发翻页点击/导航，纯粹只读
        # 当前这一页，比"翻完再发现超限"更干脆、也少一次没必要的操作。
        if current_page >= max_pages:
            print(f"已达到本次采集页数上限 {max_pages} 页，停止翻页。")
            break

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

    # max_orders 是"订单数量"而 max_pages 是"页数"，两者粒度不同——翻页
    # 只能整页收，最后一页很可能比 max_orders 多出几个（比如 max_orders=45、
    # 每页 20 条，翻到第 3 页凑够 45 个之前已经把这一页剩下的也读完了，
    # 累计会到 60）。这里精确截断，调用方看到的返回值数量跟配置的数字
    # 完全对得上，不会出现"配置 45 实际处理 60"这种超出预期的行为。
    # all_order_issues 不做同步截断——多几条不会被处理到的订单信息无害，
    # 没必要为了几个 key 专门再筛一遍（这个字典只用于汇总打印/issue_type
    # 这个信息性字段，见函数注释）。
    if max_orders is not None and len(all_order_numbers) > max_orders:
        all_order_numbers = all_order_numbers[:max_orders]

    print(f"共发现 {len(all_order_numbers)} 个可申诉订单。")

    return all_order_numbers, all_order_issues, all_order_details


def record_collected_orders(
    order_numbers: list[str],
    order_issues: dict[str, str],
    order_details: dict[str, dict[str, str]] | None = None,
) -> None:
    """把本次发现的可申诉订单编号及问题类型记录到数据库。

    原来叫 save_appealable_order_numbers，写
    output/appealable_orders_<run_id>.json；现在改成对每个订单调
    db.record_collected，不再需要 run_id 参数——采集时间由数据库自己的
    collected_at 字段记录（只在订单首次出现时写入，重复采集到同一订单
    不会覆盖它已有的状态，见 db.record_collected 的说明）。
    """
    shop = SHOP_ID or "default"
    order_details = order_details or {}
    for order_no in order_numbers:
        details = order_details.get(order_no, {})
        db.record_collected(
            shop,
            order_no,
            issue_type=order_issues.get(order_no, ""),
            goods_title=details.get("goods_title", ""),
            goods_id=details.get("goods_id", ""),
            goods_image_url=details.get("goods_image_url", ""),
            compensation_amount=details.get("compensation_amount", ""),
            order_created_at=details.get("order_created_at", ""),
        )


# ============================================================
# 单个订单处理
# ============================================================

def process_single_order(
    page: Page,
    order_no: str,
    issue_type: str = "",
) -> tuple[str, str]:
    """
    处理单个订单：查询聊天记录、判断申诉凭证类型、生成申诉文案，写入数据库。

    返回值用于最后打印汇总表：(申诉凭证分类结果, 申诉文案)。
    没有聊天记录时，不经过大模型判断，固定返回
    (NO_CHAT_RECORD_LABEL, NO_CHAT_RECORD_APPEAL_TEXT)。

    issue_type 为 "商品问题" 或 "服务问题"，写入数据库供后续步骤使用。

    分析失败（大模型调用异常等）时直接向上抛异常，不在这里捕获——
    跟原来的行为一致，由调用方（main.py 的 run_phase2/
    process_and_submit_one_order/run_serve_mode）负责截图、记录失败状态。
    """
    shop = SHOP_ID or "default"

    open_chat_page(page)

    records, no_chat_screenshot = query_chat_records(
        page=page,
        order_no=order_no,
    )

    data = build_chat_record_dict(
        order_no=order_no,
        records=records,
        no_chat_record_screenshot=no_chat_screenshot,
        issue_type=issue_type,
    )

    print(f"订单 {order_no} 聊天记录已抓取：{data['message_count']} 条消息")

    if not records:
        no_chat_text = get_no_chat_appeal_text(order_no)
        print(
            f"订单 {order_no} 没有聊天记录，"
            "不经过大模型判断，使用固定申诉文案。"
        )

        db.record_analyzed(
            shop, order_no,
            classification=NO_CHAT_RECORD_LABEL,
            appeal_text=no_chat_text,
            messages=data["messages"],
            message_count=data["message_count"],
            evidence_paths=data["evidence_screenshots"],
            issue_type=issue_type,
        )

        print(
            f"订单 {order_no} 分类结果：{NO_CHAT_RECORD_LABEL}"
            "（无聊天记录，未经过AI判断）"
        )
        print(f"订单 {order_no} 申诉文案：{no_chat_text}")

        return NO_CHAT_RECORD_LABEL, no_chat_text

    compact_data = compact_chat_json(data)

    label = classify_appeal_evidence(compact_data)
    appeal_text = generate_appeal_text(compact_data, label)

    db.record_analyzed(
        shop, order_no,
        classification=label,
        appeal_text=appeal_text,
        messages=data["messages"],
        message_count=data["message_count"],
        evidence_paths=data["evidence_screenshots"],
        issue_type=issue_type,
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
            if not open_appealable_orders_table(page):
                print("没有发现可申诉的订单，结束。")
                return

            order_numbers, order_issues, order_details = (
                collect_all_appealable_order_numbers(page)
            )

            record_collected_orders(
                order_numbers=order_numbers,
                order_issues=order_issues,
                order_details=order_details,
            )

            print(f"{len(order_numbers)} 个可申诉订单编号已记录到数据库（state/pddchat.db）")

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

                    shot = None
                    if page is not None and not page.is_closed():
                        shot = save_error_screenshot(
                            page=page,
                            order_no=order_no,
                        )

                    db.record_analysis_failed(
                        SHOP_ID or "default", order_no, error,
                        screenshot_path=to_relative_evidence_path(shot) if shot else None,
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
