"""客诉申诉结果查询：保留申诉列表页默认日期区间，把已出结果的记录回写
数据库。

具体记录复用 db.record_appeal_result() + db._SUBMIT_STATUS_MAP，跟 main.py
提交流程共用同一套内部状态枚举；orders 表的汇总状态则写中文。但这是一条完全独立
的、只读列表页驱动的代码路径——不碰 appeal_submit.py 的 TERMINAL_STATUS_
MARKERS/detect_detail_page_status，那套是详情页驱动、在main.py *重新
提交* 某个订单时顺带识别终态的机制，跟这里"周期性查一遍结果"是两件不
相关的事，只是最终都落到同一张 orders/appeal_records 表、同一套 status
枚举上，保持口径一致。

结果表格列顺序、处理状态文案全集均已根据真实页面（用户提供的 HTML/截图，
2026-08-20）核实，见 _RESULT_TEXT_CELL_INDEX/_ISSUE_TYPE_CELL_INDEX/
APPEAL_RESULT_TEXT_MAP 各自的说明。查询时保留页面默认日期区间，不操作
日期选择器。还没有实机跑过完整流程（登录 + 真实点击 + 真实翻页），如果
拼多多后台前端有后续改版，选择器可能需要重新核对。
"""

from __future__ import annotations


def _bootstrap_shop_env() -> None:
    """从 sys.argv 抓 --shop 写入 os.environ，供 paths.py import 时读取。

    必须在任何 from paths / from login 等 import 之前执行——跟 main.py/
    run_appeal_batch.py 顶部同名函数是同一份样板（paths.py 在 import 时
    就固化路径，晚了就来不及）。
    """
    import os
    import sys

    if "--shop" in sys.argv:
        i = sys.argv.index("--shop")
        if i + 1 < len(sys.argv):
            os.environ.setdefault("PDDCHAT_SHOP", sys.argv[i + 1])


_bootstrap_shop_env()


import re
from datetime import date
from typing import Callable
from urllib.parse import urlencode

from playwright.sync_api import Locator, Page, sync_playwright
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import db
import result_query_settings
from appeal_submit import (
    get_search_result_signature,
    open_appeal_search_page,
    wait_for_search_result_updated,
)
from blockers import clear_blockers, safe_click
from login import (
    LoginRequiredError, get_query_button, raise_if_login_page,
    wait_for_content_or_login, wait_for_login,
)
from paths import PROFILE_DIR, SHOP_ID
from pdd_chat import click_next_page
from screenshots import save_error_screenshot, to_relative_evidence_path
from stealth import create_stealth_context, jitter_wait

# ============================================================
# 本地状态：哪些订单值得在结果页里找
# ============================================================

# 只查"已经真实提交、还没有终审结果"的订单：
# - submitted：真实提交成功（main.py --confirm 模式），确定发生过一次
#   真实申诉，等结果。
# - already_appealed：详情页曾检测到"已申诉"（appeal_submit.py 的
#   TERMINAL_STATUS_MARKERS 路径写入），同样是"真实申诉存在，结果未知"。
# - review_unknown：上一次查询看到了结果列有文字，但没认出是什么意思
#   （见 APPEAL_RESULT_TEXT_MAP 的 fallback），文案可能后续被我们认出来，
#   值得再查一次。
# - 已申诉 / 结果待确认：结果查询流程写回 orders 的中文待查询状态；前者
#   表示页面明确仍在审核，后者表示上次未能得到唯一、可靠的终态。
# 故意不含 "simulated"——那是 main.py 非 --confirm 模式下点"取消"的
# 结果，压根没有真的提交给拼多多，没有真实审核结果可查。也不含
# "rejected"/"expired"/"approved"——这三个默认当终态处理，不再复查
# （如果拼多多的审核结论会被后续翻案，把对应状态加进这个列表即可）。
CHECK_RESULT_STATUSES = [
    "submitted",
    "already_appealed",
    "review_unknown",  # 兼容旧数据
    "已申诉",
    "结果待确认",
]

APPEAL_DETAIL_URL = (
    "https://mms.pinduoduo.com/aftersales/"
    "customer_complain_appeal/detail"
)
_DETAIL_FINAL_STATUSES = {
    "申诉通过",
    "申诉未通过已处罚",
    "超时未申诉已处罚",
}
_DETAIL_WAITING_STATUS = "已申诉"

# 逐单详情查询的拟人化节奏。每次实际等待都会在基础值上下随机浮动 45%，
# 避免按固定周期机械访问；每完成 5 个订单再加入一次更长停顿。
_DETAIL_PRE_NAV_WAIT_MS = 2_500
_DETAIL_READ_WAIT_MS = 1_800
_DETAIL_BETWEEN_ORDER_WAIT_MS = 3_500
_DETAIL_LONG_BREAK_MS = 8_000
_DETAIL_LONG_BREAK_EVERY = 5
_DETAIL_WAIT_JITTER_RATIO = 0.45


# ============================================================
# 结果文案翻译表（列表页原始文案 -> 落库用的 result_text）
# ============================================================

# 值为 None：已知的非终态（比如仍在审核中），不写 appeal_records；调用方
# 仍会把明确的中文状态或待确认原因写回 orders。
# 值为字符串：必须是 db._SUBMIT_STATUS_MAP 里已登记的 key，才会被正确
# 映射成一个具体的 status；未登记的字符串会落到 db.py 的
# fallback_status="review_unknown" 兜底分支，原始文案保留在 error_message
# 里，不会被静默丢弃。
#
# 用户在真实页面上确认过"处理状态"列一共会出现这 5 种文案（2026-08-20）：
# 可申诉 / 已申诉 / 超时未申诉已处罚 / 申诉通过 / 申诉未通过已处罚。
# 后两个"已处罚"结尾的是终态，映射到 db._SUBMIT_STATUS_MAP 里已有的
# "已超时-跳过"（expired）/"已驳回-跳过"（rejected）——这两个 key 原本是
# main.py 提交流程那条路（详情页文案 -> appeal_submit.TERMINAL_STATUS_
# MARKERS -> main._TERMINAL_STATUSES）在用的，这里直接复用而不是另起
# 新词，保证不管从哪条路径查到"超时"/"驳回"，落库的 status 是同一个值。
# "申诉通过"是这个功能特有的新终态，映射到"已通过"（db.py 那边新增的
# _SUBMIT_STATUS_MAP key，见 db.py 对应注释）。
APPEAL_RESULT_TEXT_MAP: dict[str, str | None] = {
    "可申诉": None,           # 还没申诉——正常不该出现在我们关心的订单里
                              # （我们只查本地状态是 submitted/already_
                              # appealed/review_unknown 的订单，见
                              # CHECK_RESULT_STATUSES），出现了大概率是
                              # 本地记录跟拼多多不同步，先不落库、不改动
                              # 本地状态，留到后面用 error_message/日志
                              # 排查，不在这里静默处理成任何终态。
    "已申诉": None,           # 已提交，审核结果还没出来，orders 写中文状态
    "超时未申诉已处罚": "已超时-跳过",
    "申诉通过": "已通过",
    "申诉未通过已处罚": "已驳回-跳过",
}


def _resolve_result_text(raw_text: str) -> str | None:
    """把结果列原始文案翻译成落库用的 result_text；None 表示"已知非终态，
    不用落库"。未登记的文案不当空/丢弃处理——原样返回，交给调用方以
    fallback_status="review_unknown" 落库，避免新出现的终态文案被永久
    静默忽略。
    """
    if raw_text in APPEAL_RESULT_TEXT_MAP:
        return APPEAL_RESULT_TEXT_MAP[raw_text]
    return raw_text


def _merge_result_texts(outcomes: list[str]) -> str:
    """把同一订单号名下多条 appeal_records 的当前结果合并成一句话，写回
    orders.status——跟 main.py 的 _summarize_results 是同一个思路的独立
    实现（不能 import main.py：main.py 是导入这个模块的一方，反向 import
    会循环引用）。只有一条记录、或者所有记录结果一致时原样返回；不一致时
    列出明细。跟 main.py 的版本相比省掉了"失败:"前缀逻辑——这里的每一条
    outcome 都是"成功读到了一个结果"，不存在"读取本身失败"的语义。
    """
    if len(set(outcomes)) == 1:
        return outcomes[0]
    return f"{len(outcomes)} 条记录: {'、'.join(outcomes)}"


_ORDER_STATUS_TEXT_MAP = {
    "未找到申诉记录": "未找到申诉记录",
    "已申诉-跳过": "已申诉",
    "已超时-跳过": "超时未申诉已处罚",
    "已驳回-跳过": "申诉未通过已处罚",
    "已提交": "已提交",
    "成功": "模拟成功",
    "已通过": "申诉通过",
}
_WAITING_RECORD_STATUSES = {"submitted", "already_appealed", "review_unknown"}


def _summarize_order_review_status(records: list[dict]) -> tuple[str, str | None]:
    """把 appeal_records 的内部状态汇总成 orders 表使用的中文状态。"""
    outcome_texts: list[str] = []
    still_waiting = False

    for record in records:
        internal_status = record.get("status") or ""
        if internal_status in _WAITING_RECORD_STATUSES:
            still_waiting = True

        result_text = record.get("error_message") or db.result_text_for_status(
            internal_status
        )
        outcome_texts.append(_ORDER_STATUS_TEXT_MAP.get(result_text, result_text))

    merged = _merge_result_texts(outcome_texts)
    if still_waiting:
        return "结果待确认", merged
    return merged, None


def extract_appeal_status_from_detail_page(page: Page) -> str:
    """从申诉详情页的“处理状态”区块读取状态名称。

    前端类名带构建哈希，因此只匹配稳定的 CSS Modules 类名前缀；同时用
    “处理状态”标题确认区块，避免误读页面里其他名称相近的状态组件。
    """
    status_blocks = page.locator('[class*="detail_status__"]')
    wait_for_content_or_login(page, status_blocks.first, timeout=20_000)
    status_text = status_blocks.evaluate_all(
        """
        (blocks) => {
            const statusBlock = blocks.find((block) => {
                const title = block.querySelector(
                    '[class*="detail_subTitle__"]'
                );
                return (title?.textContent || "").trim() === "处理状态";
            });
            const statusName = statusBlock?.querySelector(
                '[class*="detail_statusName__"]'
            );
            if (!statusName) return "";

            const knownStatuses = [
                "申诉未通过已处罚",
                "超时未申诉已处罚",
                "申诉通过",
                "已申诉",
            ];
            const walker = document.createTreeWalker(
                statusName,
                NodeFilter.SHOW_TEXT
            );
            const textNodes = [];
            while (walker.nextNode()) {
                const text = (walker.currentNode.textContent || "").trim();
                if (text) textNodes.push(text);
            }

            // 新版详情页会把客服入口、判决原因按钮等组件嵌进状态容器；
            // 真实状态仍是其中一个独立文本节点，例如“已申诉”。
            const exactStatus = knownStatuses.find(
                (status) => textNodes.includes(status)
            );
            if (exactStatus) return exactStatus;

            // 兼容状态文字被前端额外包裹或与其他短文本合并的版本。只有
            // 唯一命中标准状态时才提取，避免把歧义页面误判成终态。
            const fullText = (statusName.textContent || "").trim();
            const includedStatuses = knownStatuses.filter(
                (status) => fullText.includes(status)
            );
            return includedStatuses.length === 1
                ? includedStatuses[0]
                : fullText;
        }
        """
    )
    if not status_text:
        raise RuntimeError("详情页未找到“处理状态”对应的状态名称")
    return str(status_text).strip()


def _summarize_detail_statuses(statuses: list[str]) -> tuple[str, str | None]:
    """把同一订单多个 ticketSn 的详情状态汇总成 orders 的中文状态。"""
    normalized = [status.strip() for status in statuses if status.strip()]
    if not normalized:
        return "结果待确认", "没有读取到任何详情状态"

    merged = _merge_result_texts(normalized)
    unknown = [
        status
        for status in normalized
        if status not in _DETAIL_FINAL_STATUSES | {_DETAIL_WAITING_STATUS}
    ]
    if unknown:
        return "结果待确认", f"详情页出现未知处理状态：{merged}"

    if _DETAIL_WAITING_STATUS in normalized:
        if set(normalized) == {_DETAIL_WAITING_STATUS}:
            return _DETAIL_WAITING_STATUS, None
        return "结果待确认", f"同一订单仍有申诉处理中：{merged}"

    return merged, None


def _ticket_sns_from_order(order: dict) -> list[str]:
    """拆分 orders.ticket_sn；兼容历史数据中可能出现的常见分隔符。"""
    raw_value = str(order.get("ticket_sn") or "")
    return list(
        dict.fromkeys(
            value
            for value in re.split(r"[、,，;；\s]+", raw_value)
            if value
        )
    )


def query_missing_orders_by_ticket_sn(
    page: Page,
    shop: str,
    order_local_info: dict[str, dict],
    missing_order_numbers: list[str],
    *,
    direct: bool = False,
    on_order_progress: Callable[[int, int], None] | None = None,
) -> list[dict]:
    """逐个打开未出现在列表页的订单详情，避免受默认日期范围影响而漏查。"""
    if not missing_order_numbers:
        return []

    if direct:
        print(f"[负向结果] 按提交时间选出 {len(missing_order_numbers)} 个订单，直接按 ticketSn 查询详情，避免扫描无关列表页。")
    else:
        print(f"列表页未出现 {len(missing_order_numbers)} 个本地待查询订单，开始按 ticketSn 逐单查询详情。")
    print(
        "逐单查询已启用随机拟人等待，并且每查询 "
        f"{_DETAIL_LONG_BREAK_EVERY} 个订单会额外放慢一次。"
    )
    updated: list[dict] = []
    orders_with_ticket_sn: list[str] = []
    ticket_sns_by_order: dict[str, list[str]] = {}
    for index, order_no in enumerate(missing_order_numbers, start=1):
        ticket_sns = _ticket_sns_from_order(order_local_info[order_no])
        if ticket_sns:
            orders_with_ticket_sn.append(order_no)
            ticket_sns_by_order[order_no] = ticket_sns
            continue

        reason = "本地订单缺少 ticketSn，无法打开申诉详情页逐单查询"
        db.record_order_review_status(shop, order_no, "结果待确认", reason)
        updated.append(
            {
                "order_no": order_no,
                "record_key": "ticketSn:缺失",
                "result_text": "结果待确认",
            }
        )
        print(
            f"逐单查询 {index}/{len(missing_order_numbers)}："
            f"订单 {order_no} 跳过，{reason}。"
        )

    if not orders_with_ticket_sn:
        return updated

    try:
        detail_page = page.context.new_page()
    except Exception as error:
        reason = f"无法创建详情页：{type(error).__name__}: {error}"
        for order_no in orders_with_ticket_sn:
            db.record_order_review_status(
                shop, order_no, "结果待确认", reason
            )
            updated.append(
                {
                    "order_no": order_no,
                    "record_key": "ticketSn:查询失败",
                    "result_text": "结果待确认",
                }
            )
        print(f"逐单查询无法启动，所有漏查订单保留待确认：{reason}")
        return updated

    try:
        for index, order_no in enumerate(orders_with_ticket_sn, start=1):
            if index > 1:
                completed_count = index - 1
                between_order_wait = (
                    _DETAIL_LONG_BREAK_MS
                    if completed_count % _DETAIL_LONG_BREAK_EVERY == 0
                    else _DETAIL_BETWEEN_ORDER_WAIT_MS
                )
                jitter_wait(
                    detail_page,
                    between_order_wait,
                    _DETAIL_WAIT_JITTER_RATIO,
                )

            ticket_sns = ticket_sns_by_order[order_no]
            statuses: list[str] = []
            failures: list[str] = []
            for ticket_sn in ticket_sns:
                detail_url = f"{APPEAL_DETAIL_URL}?{urlencode({'ticketSn': ticket_sn})}"
                try:
                    # 像人工从订单信息判断后再打开详情，不连续瞬时跳转。
                    jitter_wait(
                        detail_page,
                        _DETAIL_PRE_NAV_WAIT_MS,
                        _DETAIL_WAIT_JITTER_RATIO,
                    )
                    try:
                        detail_page.goto(
                            detail_url,
                            wait_until="domcontentloaded",
                            timeout=60_000,
                        )
                    except PlaywrightTimeoutError:
                        # 部分静态资源长时间挂起时 goto 可能超时，但核心详情
                        # DOM 已经渲染出来；继续尝试读取，真正没有状态节点时
                        # 再由下方的显式等待给出查询失败原因。
                        print(
                            f"订单 {order_no}（{ticket_sn}）详情页加载超时，"
                            "继续尝试读取已渲染内容。"
                        )
                    clear_blockers(detail_page)
                    # 页面出现后保留阅读时间，再提取处理状态。
                    jitter_wait(
                        detail_page,
                        _DETAIL_READ_WAIT_MS,
                        _DETAIL_WAIT_JITTER_RATIO,
                    )
                    status_text = extract_appeal_status_from_detail_page(detail_page)
                    statuses.append(status_text)
                    updated.append(
                        {
                            "order_no": order_no,
                            "record_key": f"ticketSn:{ticket_sn}",
                            "result_text": status_text,
                        }
                    )
                    print(
                        f"逐单详情 {index}/{len(orders_with_ticket_sn)}："
                        f"订单 {order_no}（{ticket_sn}）={status_text}"
                    )
                except LoginRequiredError:
                    raise
                except Exception as error:
                    failures.append(
                        f"ticketSn={ticket_sn}：{type(error).__name__}: {error}"
                    )
                    updated.append(
                        {
                            "order_no": order_no,
                            "record_key": f"ticketSn:{ticket_sn}",
                            "result_text": "结果待确认",
                        }
                    )

            if failures:
                detail_parts = []
                if statuses:
                    detail_parts.append(f"已读取状态：{_merge_result_texts(statuses)}")
                detail_parts.append("；".join(failures))
                detail = "；".join(detail_parts)
                db.record_order_review_status(
                    shop, order_no, "结果待确认", detail
                )
                print(f"订单 {order_no} 的部分或全部详情查询失败：{detail}")
                if on_order_progress:
                    on_order_progress(index, len(orders_with_ticket_sn))
                continue

            status_text, detail = _summarize_detail_statuses(statuses)
            db.record_order_review_status(
                shop, order_no, status_text, detail
            )
            if on_order_progress:
                on_order_progress(index, len(orders_with_ticket_sn))
    finally:
        if not detail_page.is_closed():
            try:
                detail_page.close()
            except Exception as error:
                print(f"关闭逐单查询详情页失败（不影响已保存结果）：{error}")

    return updated


# ============================================================
# 结果表格：翻页 + 逐页提取
# ============================================================

# 跟 appeal_submit.py 的 _TIMESTAMP_PATTERN 同一个正则、同一个用途（从行
# 文本里提取申诉记录"创建时间"当 record_key）。这里就地保留一份而不是
# 跨模块 import 那个下划线开头的私有属性——两边如果哪天格式变了要记得
# 一起改，是本项目里已有的、可接受的小范围重复（run_appeal_batch.py/
# appeal_submit.py 各自维护一份"等待表格签名变化"的逻辑也是同样的取舍）。
_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")

# 用户在真实页面上核对过的表头顺序（2026-08-20）：订单编号 / 商品信息 /
# 问题类型 / 补偿金额（元） / 扣款进度 / 创建时间 / 处理状态 / 操作。
# "处理状态"列就是这个功能要读的"申诉结果"。
_RESULT_TEXT_CELL_INDEX = 6   # 处理状态
_ISSUE_TYPE_CELL_INDEX = 2    # 问题类型——跟 run_appeal_batch.py 那张"可
                              # 申诉订单"列表页恰好是同一个列号，但这是
                              # 两张不同的表格，位置是分别核实过的，不是
                              # 假设两边一致才写成一样的数字。
_GOODS_CELL_INDEX = 1
_COMPENSATION_CELL_INDEX = 3
_CREATED_AT_CELL_INDEX = 5


def extract_appeal_result_rows_from_current_page(page: Page) -> list[dict[str, str]]:
    """提取当前页每一行的订单编号/申诉结果文案/问题类型/整行文本。

    第 1 列（cells[0]）= 订单编号，是这个项目里所有 beast-core-table
    结果表格已验证过的稳定约定（run_appeal_batch.py 的
    extract_order_info_from_current_page、appeal_submit.py 的
    get_search_result_signature 都基于这条约定），这张"处理状态"结果
    表格同样符合。

    row_text 是整行 innerText，用于 Python 侧正则提取 record_key
    （_TIMESTAMP_PATTERN，实际就是"创建时间"那一列的值），不在 JS 里
    重复实现同一个正则。
    """
    rows = page.locator('tbody[data-testid="beast-core-table-middle-tbody"] tr')
    if rows.count() == 0:
        return []

    return rows.evaluate_all(
        r"""
        (trs, indices) => {
            return trs.map((tr) => {
                const cells = tr.querySelectorAll(
                    'td[data-testid="beast-core-table-td"]'
                );
                const orderNo = (cells[0]?.textContent || "").trim();
                const goodsCell = cells[indices.goods];
                const resultText = (cells[indices.result]?.textContent || "").trim();
                const issueType = (cells[indices.issue]?.textContent || "").trim();
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
                    cells[indices.compensation]?.textContent || ""
                ).trim();
                const orderCreatedAt = (
                    cells[indices.createdAt]?.textContent || ""
                ).trim();
                const rowText = (tr.innerText || "").trim();
                return {
                    order_no: orderNo,
                    result_text: resultText,
                    issue_type: issueType,
                    goods_title: goodsTitle,
                    goods_id: goodsIdMatch ? goodsIdMatch[0] : "",
                    goods_image_url: goodsImageUrl,
                    compensation_amount: compensationAmount,
                    order_created_at: orderCreatedAt,
                    row_text: rowText,
                };
            });
        }
        """,
        {
            "result": _RESULT_TEXT_CELL_INDEX,
            "issue": _ISSUE_TYPE_CELL_INDEX,
            "goods": _GOODS_CELL_INDEX,
            "compensation": _COMPENSATION_CELL_INDEX,
            "createdAt": _CREATED_AT_CELL_INDEX,
        },
    )


def _record_key_from_row_text(text: str) -> str:
    match = _TIMESTAMP_PATTERN.search(text)
    return match.group(0) if match else text.strip()


def _backfill_order_details_from_result_row(
    shop: str,
    row: dict[str, str],
) -> None:
    """把结果列表里可重新获得的订单字段补回 orders，已有值保持不变。"""
    db.backfill_order_list_details(
        shop,
        row["order_no"],
        goods_title=row.get("goods_title", ""),
        goods_id=row.get("goods_id", ""),
        goods_image_url=row.get("goods_image_url", ""),
        compensation_amount=row.get("compensation_amount", ""),
        order_created_at=row.get("order_created_at", ""),
    )


# ============================================================
# 主流程
# ============================================================

def prepare_result_query(shop: str, submitted_since: str | None = None) -> tuple[list[dict], str]:
    """在打开浏览器之前确定本地待查范围，并说明没有目标的原因。"""
    submitted_since = result_query_settings.normalize_since(submitted_since)
    pending = db.list_orders(shop, statuses=CHECK_RESULT_STATUSES)
    targets = result_query_settings.filter_records(pending, submitted_since, "submitted_at")
    if targets:
        return targets, ""
    if pending:
        since_text = result_query_settings.parse_time(submitted_since).strftime("%Y-%m-%d %H:%M:%S")
        return [], (
            f"本地有 {len(pending)} 条待回查记录，但提交时间均早于 {since_text}（北京时间）。"
            "请调整结果回查的提交时间起点，或取消时间限制后重试。"
        )
    return [], (
        "本地没有该店铺已提交且等待结果的负向申诉订单。"
        "此入口仅回查本地已记录的申诉，不会自动导入平台历史订单。"
    )


def check_appeal_results(
    page: Page,
    shop: str,
    since_date: date | None = None,
    until_date: date | None = None,
    on_progress: Callable[[int, int, int], None] | None = None,
    max_pages: int = 50,
    submitted_since: str | None = None,
    target_orders: list[dict] | None = None,
) -> list[dict]:
    """使用页面默认日期区间查询申诉结果，匹配数据库里
    “已提交、还没有终审结果”的订单，把查到的结果写回数据库。

    since_date/until_date 为向后兼容保留的可选参数；当前流程不会操作页面
    日期选择器，也不会使用这两个值改变查询范围。

    返回本次实际写入的记录列表：
        [{"order_no": ..., "record_key": ..., "result_text": ...}, ...]
    只包含本次运行新落库的行，不包含"结果页上出现但本地根本不关心"或
    "还在审核中、跳过不写"的行。

    on_progress(checked_total, matched_total, page_no)：每翻完一页调用
    一次，供调用方（main.py 的 run_check_appeal_results_mode）转成协议
    消息上报进度。

    不做"匹配到就提前结束翻页"的优化——同一订单号可能有多条独立的申诉
    记录（商品问题一条/服务问题一条），可能分布在结果表格的不同行甚至
    不同页；只要本地还有任何订单在关心结果，就必须把页面默认区间内的
    结果扫完，否则可能漏掉同一订单号的第二条记录。
    """
    submitted_since = result_query_settings.normalize_since(submitted_since)
    if target_orders is None:
        local_records, empty_reason = prepare_result_query(shop, submitted_since)
    else:
        local_records, empty_reason = target_orders, "本批次没有待回查订单。"
    order_local_info = {
        row["order_no"]: row
        for row in local_records
    }
    if not order_local_info:
        print(f"店铺 {shop} 无需查询：{empty_reason}")
        return []

    print(
        f"店铺 {shop}：{len(order_local_info)} 个订单等待申诉结果，"
        "使用页面默认日期区间。"
    )

    direct_updates = []
    if submitted_since is not None:
        direct_orders = {
            order_no: record for order_no, record in order_local_info.items()
            if _ticket_sns_from_order(record)
        }
        direct_updates = query_missing_orders_by_ticket_sn(
            page, shop, direct_orders, list(direct_orders), direct=True,
            on_order_progress=(lambda current, total: on_progress(current, total, 0)) if on_progress else None,
        )
        order_local_info = {
            order_no: record for order_no, record in order_local_info.items()
            if order_no not in direct_orders
        }
        if not order_local_info:
            return direct_updates
        print(f"[负向结果] {len(order_local_info)} 个目标订单缺少 ticketSn，继续通过列表查询；无法定位的订单保留待确认。")

    try:
        open_appeal_search_page(page)  # 复用：导航 + clear_blockers + 基本就绪检查
    except PlaywrightTimeoutError:
        raise_if_login_page(page)
        raise
    raise_if_login_page(page)

    query_button = get_query_button(page)
    query_button.wait_for(state="visible", timeout=20_000)

    previous_signature = get_search_result_signature(page)
    safe_click(page=page, locator=query_button, description="查询按钮")
    clear_blockers(page)
    wait_for_search_result_updated(page=page, previous_signature=previous_signature)
    jitter_wait(page, 500)

    updated: list[dict] = list(direct_updates)
    order_outcomes: dict[str, list[str]] = {}  # 本次运行新落库的 order_no -> [result_text,...]
    page_statuses: dict[str, list[str]] = {}
    checked_total = 0
    matched_total = 0
    visited_signatures: set[str] = set()
    seen_page_order_numbers: set[str] = set()

    for page_no in result_query_settings.page_numbers(max_pages, submitted_since):
        rows = extract_appeal_result_rows_from_current_page(page)
        if not rows:
            print(f"第 {page_no} 页没有结果行，停止翻页。")
            break

        signature = "|".join(r["order_no"] for r in rows)
        page_identity = "|".join(
            r.get("row_text") or f"{r['order_no']}#{r.get('issue_type', '')}#{r.get('result_text', '')}"
            for r in rows
        )
        if page_identity in visited_signatures:
            if submitted_since is not None:
                raise RuntimeError("负向申诉结果出现重复页面，查询未完成，请核对未查订单。")
            print("检测到重复页面，停止翻页，避免死循环。")
            break
        visited_signatures.add(page_identity)
        checked_total += len(rows)

        for row in rows:
            order_no = row["order_no"]
            if order_no:
                seen_page_order_numbers.add(order_no)
            if order_no not in order_local_info:
                continue

            _backfill_order_details_from_result_row(shop, row)
            raw_result_text = row["result_text"]
            page_statuses.setdefault(order_no, []).append(raw_result_text)
            resolved = _resolve_result_text(raw_result_text)
            if resolved is None:
                # “已申诉”是明确的中文业务状态；“可申诉”与本地待查询
                # 状态冲突，保留为待确认并在明细中说明，二者都不能静默跳过。
                status_text = (
                    "已申诉" if raw_result_text == "已申诉" else "结果待确认"
                )
                detail = (
                    None
                    if raw_result_text == "已申诉"
                    else f"列表页处理状态为：{raw_result_text}"
                )
                db.record_order_review_status(shop, order_no, status_text, detail)
                record_key = _record_key_from_row_text(row.get("row_text", ""))
                updated.append(
                    {
                        "order_no": order_no,
                        "record_key": record_key,
                        "result_text": status_text,
                    }
                )
                continue

            matched_total += 1
            issue_type = (
                row.get("issue_type")
                or order_local_info[order_no].get("issue_type")
                or ""
            )
            record_key = _record_key_from_row_text(row.get("row_text", ""))

            db.record_appeal_result(
                shop, order_no, record_key, issue_type, resolved,
                fallback_status="review_unknown",
            )
            order_outcomes.setdefault(order_no, []).append(resolved)
            updated.append(
                {"order_no": order_no, "record_key": record_key, "result_text": resolved}
            )

        if on_progress is not None:
            on_progress(checked_total, matched_total, page_no)

        clear_blockers(page)
        previous_body = (
            page.locator('tbody[data-testid="beast-core-table-middle-tbody"]').inner_text().strip()
            if submitted_since is not None else None
        )
        next_result = click_next_page(page)
        if not next_result.get("clicked"):
            print(f"停止翻页：{next_result.get('reason', '未知原因')}")
            break
        if submitted_since is not None:
            try:
                page.wait_for_function(
                    "previous => (document.querySelector('tbody[data-testid=\"beast-core-table-middle-tbody\"]')?.innerText || '').trim() !== previous",
                    arg=previous_body, timeout=20_000,
                ).dispose()
            except PlaywrightTimeoutError as exc:
                raise RuntimeError("负向申诉结果翻页后表格未更新，查询未完成。") from exc
        elif not wait_for_search_result_updated(page=page, previous_signature=signature):
            print("点击下一页后结果表格没有变化，停止翻页。")
            break

    # 按订单号把 appeal_records 现有的全部结果（含本次之前已经落库的）
    # 合并成一句话，写回 orders.status——只处理本次至少更新过一条记录的
    # 订单号，没变化的订单不需要重新汇总。这一步是"GUI 报表页能看到结果"
    # 这条需求成立的关键：报表页只读 orders 表，不读 appeal_records。
    for order_no in order_outcomes:
        records = db.list_appeal_records(shop, order_no)
        status_text, detail = _summarize_order_review_status(records)
        db.record_order_review_status(shop, order_no, status_text, detail)

    # 同一订单可能在列表页有多条申诉记录。如果其中一条已有终态、另一条仍
    # 显示“已申诉”，不能让上面的终态汇总覆盖掉仍在处理中的那一条。
    for order_no, statuses in page_statuses.items():
        if "可申诉" in statuses:
            db.record_order_review_status(
                shop,
                order_no,
                "结果待确认",
                f"列表页存在可申诉记录：{_merge_result_texts(statuses)}",
            )
        elif "已申诉" in statuses:
            status_text, detail = _summarize_detail_statuses(statuses)
            db.record_order_review_status(shop, order_no, status_text, detail)

    missing_order_numbers = [
        order_no
        for order_no in order_local_info
        if order_no not in seen_page_order_numbers
    ]
    updated.extend(
        query_missing_orders_by_ticket_sn(
            page,
            shop,
            order_local_info,
            missing_order_numbers,
        )
    )

    print(f"店铺 {shop}：本次更新 {len(updated)} 条订单结果。")
    return updated


# ============================================================
# 单模块调试入口
# ============================================================

def main() -> None:
    """独立调试入口：单店铺、交互式登录、跑一次查询，打印结果后等回车关闭。

    跟 appeal_submit.py/run_appeal_batch.py 底部的调试 main() 是同一种
    风格——用于验证登录、导航、查询、翻页和结果回写的完整链路。
    """
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
        page = None
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page = wait_for_login(context=context, page=page)

            updated = check_appeal_results(
                page, SHOP_ID or "default",
                on_progress=lambda checked, matched, page_no: print(
                    f"第 {page_no} 页：已检查 {checked}，匹配 {matched}"
                ),
            )

            print()
            print("=" * 60)
            print(f"共更新 {len(updated)} 条订单结果：")
            for item in updated:
                print(f"  {item['order_no']}（{item['record_key']}）: {item['result_text']}")

        except Exception as error:
            print(f"运行失败：{type(error).__name__}: {error}")
            if page is not None and not page.is_closed():
                shot = save_error_screenshot(page, "check_appeal_results_debug")
                if shot is not None:
                    print(f"现场截图：{to_relative_evidence_path(shot)}")

        input("操作完成，按回车关闭浏览器：")
        context.close()


if __name__ == "__main__":
    main()
