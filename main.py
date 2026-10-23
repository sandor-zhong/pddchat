"""
全流程批量申诉模拟流水线。

默认（不带 --collect/--analyze/--submit 任一阶段参数）走"逐单模式"：
  Phase 1 — 一次性采集可申诉订单号（结构上无法拆成逐单，保持批量；默认
             只读第一页，见 run_appeal_batch.py 的
             collect_all_appealable_order_numbers）
  之后逐单循环：分析这一单（查聊天+AI生成申诉文案，已分析过的订单默认跳过
  重新分析，除非加 --reanalyze）→ 立刻提交/取消这一单 → 再处理下一单。
  这样"提交"的最小单位就是一个订单，便于以后接多店铺轮流提交的调度器。

显式指定 --collect / --analyze / --submit 中任意一个时，走原来的分阶段批量
流程（该阶段内部仍是"全部订单跑完"再进入下一阶段），用于分步调试：
  Phase 1 — 从申诉列表采集可申诉订单号（默认只第一页）
  Phase 2 — 逐单查聊天记录并用 AI 分析生成申诉文案（全部订单跑完）
  Phase 3 — 逐单打开申诉详情页，根据 A/B/C 状态自动填表上传凭证，
             最后点"取消"关闭表单（不真实提交，全部订单跑完）

订单状态、聊天记录、AI 分析结果统一记在数据库 state/pddchat.db（见
db.py），不再落 output/ 下的 chat_<订单号>.json / appealable_orders_
<run_id>.json——用 --status 随时查进度，不用翻文件。

用法：
  python main.py                    逐单模式（分析完一单立刻处理提交，填完点取消）
  python main.py --confirm          逐单模式 + 真实提交
  python main.py --confirm --manual-captcha  逐单模式 + 真实提交 + 人工滑验证码
  python main.py --reanalyze --confirm       逐单模式，强制重新分析已分析过的订单
  python main.py --collect          仅 Phase 1（采集订单号，批量）
  python main.py --analyze          仅 Phase 2（查聊天+AI分析，批量，从数据库获取订单号）
  python main.py --submit           仅 Phase 3（填表+取消，批量）
  python main.py --submit --confirm 仅 Phase 3 + 真实提交（批量）
  python main.py --collect --analyze Phase 1 + Phase 2（批量）
  python main.py --analyze --submit  Phase 2 + Phase 3（批量，非逐单交织）
  python main.py --status           不开浏览器，打印数据库订单状态汇总
  python main.py --status --shop <店铺>  只看这一家店的状态汇总
"""

from __future__ import annotations


def _bootstrap_shop_env() -> None:
    """从 sys.argv 抓 --shop 写入 os.environ，供 paths.py import 时读取。

    必须在任何 from paths / from pdd_chat 等 import 之前执行——
    paths.py 在 import 时就固化路径，argparse 里再传就晚了。
    setdefault 保证如果 env var 已经被外部设置（例如 shell 里
    `PDDCHAT_SHOP=xxx python ...`）不会被 argv 覆盖。
    """
    import os
    import sys

    if "--shop" in sys.argv:
        i = sys.argv.index("--shop")
        if i + 1 < len(sys.argv):
            os.environ.setdefault("PDDCHAT_SHOP", sys.argv[i + 1])


_bootstrap_shop_env()


import argparse
import json
import queue
import sys
import threading
from datetime import datetime
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, sync_playwright

import aftersale_appeal
import aftersale_result_settings
import check_aftersale_appeal_results
import check_appeal_results
import check_reconsideration_results
import db
import result_query_settings
import reconsideration
from appeal_submit import (
    collect_evidence_files,
    click_appeal_form_cancel,
    click_appeal_form_submit,
    detect_detail_page_status,
    fill_appeal_modal,
    get_result_rows,
    get_row_detail_link,
    get_row_issue_type,
    get_row_record_key,
    load_order_classification,
    open_appeal_search_page,
    open_row_detail_page,
    search_and_get_rows,
)
from captcha import set_auto_solve_captcha, set_give_up_after_auto_solve_fails
from login import (
    AccountLoginError,
    account_login,
    wait_for_login,
    wait_for_login_polling,
)
from paths import DEBUG_DIR, OUTPUT_DIR, PROFILE_DIR, SHOP_ID
from run_appeal_batch import (
    collect_all_appealable_order_numbers,
    open_appeal_list_page,
    open_appealable_orders_table,
    process_single_order,
    record_collected_orders,
)
from screenshots import safe_filename, save_error_screenshot, to_relative_evidence_path
from stealth import create_stealth_context, human_delay, paced_business_operations, business_read_page

# ============================================================
# 辅助函数
# ============================================================


_CHAT_PAGE_FRAGMENT = "/mms-chat/search"
_APPEAL_PAGE_FRAGMENT = "/aftersales/customer_complain_appeal"


def _get_or_create_role_page(
    context: BrowserContext,
    current_page: Page | None,
    url_fragment: str,
    *,
    excluded_pages: tuple[Page | None, ...] = (),
) -> Page:
    """取得某个固定用途的标签页，优先复用同一店铺会话中的现有页面。

    每家店铺拥有独立 BrowserContext，因此这里只在当前 context 内查找，
    不会跨店铺复用登录态。传入的 current_page 仍有效时优先保留它；页面
    被用户关闭或因异常失效时，再按 URL 找同用途页面，最后才新建。
    """
    excluded_ids = {
        id(candidate)
        for candidate in excluded_pages
        if candidate is not None
    }

    if (
        current_page is not None
        and not current_page.is_closed()
        and id(current_page) not in excluded_ids
    ):
        return current_page

    for candidate in reversed(context.pages):
        if candidate.is_closed() or id(candidate) in excluded_ids:
            continue
        if url_fragment in candidate.url:
            print(f"复用店铺浏览器中已打开的标签页：{candidate.url}")
            return candidate

    return context.new_page()


def _close_pages_opened_since(
    context: BrowserContext,
    existing_page_ids: set[int],
    *,
    protected_pages: tuple[Page | None, ...] = (),
) -> None:
    """仅关闭本次操作新开的临时页，保留聊天页、申诉页等固定页。"""
    protected_ids = {
        id(candidate)
        for candidate in protected_pages
        if candidate is not None
    }
    for candidate in list(context.pages):
        if (
            candidate.is_closed()
            or id(candidate) in existing_page_ids
            or id(candidate) in protected_ids
        ):
            continue
        try:
            candidate.close()
        except Exception:
            pass


def _discover_analyzed_orders(possible: list[str]) -> list[str]:
    """在 possible 中过滤出数据库里已有 AI 分析结果的订单号。"""
    shop = SHOP_ID or "default"
    return [order_no for order_no in possible if db.has_analysis(shop, order_no)]


def _order_has_analysis(order_no: str) -> bool:
    """已有分析且存在可用的一至两张聊天凭证，才可复用。"""
    return db.has_analysis(SHOP_ID or "default", order_no) and bool(collect_evidence_files(order_no))


def _print_status_report(shop: str | None) -> None:
    """--status：不开浏览器，直接查数据库打印订单状态汇总。

    shop=None（未传 --shop）时跨店铺汇总；传了 --shop 只看那一家。
    """
    counts = db.status_counts(shop)
    if not counts:
        if shop:
            print(f"数据库里还没有店铺 {shop} 的任何订单记录。")
        else:
            print("数据库里还没有任何订单记录。")
        return

    print("=" * 60)
    print(f"订单状态汇总（{shop or '所有店铺'}）")
    print("=" * 60)

    current_shop: str | None = None
    for row_shop, status, count in counts:
        if row_shop != current_shop:
            if current_shop is not None:
                print()
            print(f"[{row_shop}]")
            current_shop = row_shop
        print(f"  {status}: {count}")

    _print_daily_summary(shop)
    _print_daily_schedule_status(shop)


def _print_daily_summary(shop: str | None) -> None:
    """--status 报表的"今日处理情况"部分：一眼看出今天每家店处理了多少、
    失败多少、还有没有没处理完的——不新建表，直接查 orders 现算。"""
    rows = db.daily_shop_summary(shop)
    if not rows:
        return

    today_local = datetime.now().strftime("%Y-%m-%d")

    print()
    print("=" * 60)
    print(f"今日处理情况（本地日期 {today_local}）")
    print("=" * 60)

    for row in rows:
        if row["touched_today"]:
            detail = (
                f"提交 {row['submitted_today']}"
                f"，失败 {row['failed_today']}"
                f"，跳过 {row['skipped_today']}"
            )
            touched_text = f"今日处理: {row['touched_today']}（{detail}）"
        else:
            touched_text = "今日处理: 0"

        if row["pending_remaining"] == 0:
            pending_text = "剩余待处理: 0 ✓ 已清空"
        else:
            pending_text = f"剩余待处理: {row['pending_remaining']}"

        print(f"[{row['shop']}]")
        print(f"  {touched_text}  {pending_text}")


def _print_daily_schedule_status(shop: str | None) -> None:
    """--status 报表的"今日调度状态"部分：跟 run_shops_interleaved.py
    判断"要不要为一家店铺开浏览器"用的是同一份数据（db.shop_daily_status），
    摆出来方便人看——哪家店今天完成了、哪家还在等重试、哪家压根还没跑。
    """
    status_rows = db.list_shop_daily_status()
    if shop is not None:
        status_rows = [r for r in status_rows if r["shop"] == shop]
    status_by_shop = {r["shop"]: r for r in status_rows}

    # "尚未开始"的店铺不会出现在 shop_daily_status 里，得从有订单记录的
    # 店铺集合里找出差集——数据库里完全没出现过的店铺（比如刚 --shop 登录、
    # 一次都没跑过采集的）看不出来，这里能展示的上限就是"数据库认识的店铺"。
    known_shops = {row["shop"] for row in db.daily_shop_summary(shop)}
    all_shops = sorted(known_shops | set(status_by_shop))
    if not all_shops:
        return

    today_local = datetime.now().strftime("%Y-%m-%d")

    print()
    print("=" * 60)
    print(f"今日调度状态（本地日期 {today_local}）")
    print("=" * 60)

    for shop_name in all_shops:
        record = status_by_shop.get(shop_name)
        if record is None:
            print(f"[{shop_name}] 尚未开始")
        elif record["status"] == "done":
            print(f"[{shop_name}] 完成 ✓")
        elif record["status"] == "skipped":
            detail = record.get("detail") or "并发名额不足"
            print(f"[{shop_name}] 排队中（{detail}）——下一轮会优先给它补位")
        else:
            detail = record.get("detail") or "原因未知"
            print(f"[{shop_name}] 失败：{detail}——下次会自动重试")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="全流程批量申诉模拟流水线",
    )
    parser.add_argument(
        "--collect",
        action="store_true",
        help="Phase 1: 采集可申诉订单号",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Phase 2: 查聊天记录 + AI 分析",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Phase 3: 模拟申诉填表（默认点取消，加 --confirm 则真提交）",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help=(
            '配合 --submit/--reconsider 使用时启用真实提交；'
            '--aftersale-appeal 必须搭配本参数，售后申诉不再提供模拟模式'
        ),
    )
    parser.add_argument(
        "--reanalyze",
        action="store_true",
        help="逐单模式下强制重新分析已有 chat_*.json 的订单（默认跳过已分析订单，直接进入提交）",
    )
    parser.add_argument(
        "--max-orders",
        type=int,
        default=None,
        help=(
            "本次运行 Phase 1 最多采集/处理的可申诉订单数量；复议模式下表示"
            "该店铺本次最多处理的复议数量。覆盖这家店铺在数据库里"
            "配置的处理上限（仅本次运行生效，不写回数据库）；不传则使用数据库里的配置"
            "（也没配置时维持只读第一页的默认行为）。正式的按店铺常设配置请用 GUI"
            "「店铺管理」页的「设置处理上限」。"
        ),
    )
    parser.add_argument(
        "--manual-captcha",
        action="store_true",
        help="跳过自动破解验证码，直接等待人工在浏览器中手动滑动验证码",
    )
    parser.add_argument(
        "--order-file",
        help="指定订单号 JSON 文件（用于 Phase 2，跳过 Phase 1 时可选）",
    )
    parser.add_argument(
        "--orders",
        nargs="+",
        help="直接指定订单号，如 --orders 260803-xxx 260802-xxx（用于 Phase 2 或 Phase 3）",
    )
    parser.add_argument(
        "--shop",
        help=(
            "店铺 ID。传入后所有目录切到 state/profiles/<shop>/、"
            "output/<shop>/、debug/<shop>/、evidence/<shop>/；"
            "不传走单店老路径。实际值已在 import 前被抓走，此处仅供 --help 展示。"
        ),
    )
    parser.add_argument(
        "--serve",
        action="store_true",
        help=(
            "常驻模式：不自己跑完整流程，改为在 stdin/stdout 上跑一个 "
            "JSON Lines 指令协议（init/next/stop），供 run_shops_interleaved.py "
            "之类的外部调度器逐单点名调用。与 --collect/--analyze/--submit/"
            "--orders/--order-file 互斥（会被忽略），可与 --confirm/"
            "--reanalyze/--manual-captcha/--shop 组合；与 --reconsider 组合时"
            "改为复议逐单长驻协议；与 --aftersale-appeal 组合时启用 GUI 的"
            "协作式停止协议。"
        ),
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="不开浏览器，直接查数据库打印订单状态汇总（不加 --shop 时跨店铺汇总）",
    )
    parser.add_argument("--check-login", action="store_true", help="无头浏览器检测店铺登录状态后退出")
    parser.add_argument(
        "--login-only",
        action="store_true",
        help=(
            "轻量登录模式：只开浏览器、轮询检测登录状态（不阻塞等终端回车），"
            "不跑任何采集/分析/提交阶段。供 gui/ 子项目的\"新增店铺/重新登录\""
            "功能调用——在 stdin/stdout 上跑一个极简 JSON Lines 协议，"
            "检测到登录完成后上报 {\"type\": \"login_result\", \"success\": "
            "true} 并保持浏览器打开，直到收到 {\"cmd\": \"close\"} 才关闭退出。"
            "必须搭配 --shop 使用；与 --collect/--analyze/--submit/--serve 互斥"
            "（会被忽略）。"
        ),
    )
    parser.add_argument(
        "--account-login",
        action="store_true",
        help=(
            "账号密码自动登录模式。必须搭配 --shop；账号密码只通过 stdin 的 "
            "JSON 指令传入，不写入命令行或日志。用于店铺管理页的 Excel 批量登录。"
        ),
    )
    parser.add_argument(
        "--check-appeal-results",
        action="store_true",
        help=(
            "查询申诉结果：进入客诉申诉列表页，按日期区间筛选，把已出结果"
            "（审核通过/驳回等）的记录回写数据库（复用 db.record_submit_result/"
            "record_appeal_result）。在 stdin/stdout 上跑一个极简 JSON Lines "
            "协议（跟 --login-only 相同的 real_stdout 用法），跑完一次就退出，"
            "不需要额外指令，不用等 {\"cmd\": \"close\"}。必须搭配 --shop 使用；"
            "与 --collect/--analyze/--submit/--serve/--login-only 互斥"
            "（会被忽略）。"
        ),
    )
    parser.add_argument(
        "--check-reconsideration-results",
        action="store_true",
        help=(
            "查询复议结果：遍历复议记录全部分页，按订单编号及复议时间等"
            "信息匹配本地复议任务并更新独立的复议状态。必须搭配 --shop 使用。"
        ),
    )
    parser.add_argument(
        "--reconsider",
        action="store_true",
        help=(
            "复议模式：扫描全部可复议订单，材料齐全时复用首次申诉原因、"
            "文案和凭证；材料缺失时先重新采集聊天并生成材料。默认只填写"
            "后取消，搭配 --confirm 才真实提交。必须搭配 --shop 使用。"
        ),
    )
    parser.add_argument(
        "--check-aftersale-results",
        action="store_true",
        help=(
            "查询售后申诉结果：遍历售后维权申诉记录页，回写审核状态、"
            "平台申诉时间和权威申诉金额。必须搭配 --shop 使用。"
        ),
    )
    parser.add_argument(
        "--aftersale-query-interval",
        type=aftersale_result_settings.validate_query_interval,
        default=aftersale_result_settings.load_query_interval(),
        metavar="秒",
        help="售后结果查询翻页、打开详情前的等待间隔（1～60 秒，默认 3 秒或已保存值）。",
    )
    parser.add_argument(
        "--aftersale-appeal",
        action="store_true",
        help=(
            "售后申诉：逐单保存退款申请单、查询并分析聊天、按最多金额"
            "填写并真实提交维权申诉表单。必须搭配 --confirm；与 --serve "
            "组合时使用 init/next/stop 协议供 GUI 多店铺逐单轮转。"
        ),
    )
    parser.add_argument(
        "--since-days",
        type=int,
        default=3,
        help=(
            "兼容旧调用保留的参数；当前 --check-appeal-results 会直接使用"
            "页面默认日期区间，不再修改开始和结束日期。"
        ),
    )
    parser.add_argument(
        "--result-submitted-since", type=result_query_settings.normalize_since,
        default=result_query_settings.active_since(), metavar="提交时间",
        help="三类结果回查只查此北京时间及之后的本地提交记录；all 表示不限制。",
    )
    return parser.parse_args()


def _resolve_order_numbers(
    args: argparse.Namespace,
) -> tuple[list[str], dict[str, str]]:
    """按优先级解析订单号：--orders > --order-file > 数据库里这家店的全部订单。

    返回 (order_numbers, order_issues)。
    """
    if args.orders:
        return args.orders, {}

    if args.order_file:
        # --order-file 读的是用户自己指定的任意 JSON 文件（手工整理的订单
        # 清单、旧版本留下的存档……都算），跟数据库无关，原样保留。
        # 绝对路径直接用；否则视为 OUTPUT_DIR 下的相对文件名。
        # 用 Path.is_absolute() 而不是判断 "/" —— 后者在 Windows 上会漏掉反斜杠。
        candidate = Path(args.order_file)
        path = candidate if candidate.is_absolute() else OUTPUT_DIR / args.order_file
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("order_numbers", []), data.get("order_issues", {})

    shop = SHOP_ID or "default"
    rows = db.list_orders(shop)
    if not rows:
        raise FileNotFoundError(
            f"数据库里没有店铺 {shop} 的任何订单记录。\n"
            "请先运行 Phase 1: python main.py --collect"
        )
    order_numbers = [row["order_no"] for row in rows]
    order_issues = {row["order_no"]: row["issue_type"] or "" for row in rows}
    return order_numbers, order_issues


# ============================================================
# 各阶段独立函数
# ============================================================


# 配置了 max_orders 时，翻页仍然受 max_pages 约束——给一个足够大的页数
# 上限，参考 pdd_chat.py 的 collect_all_chat_records(max_pages=200) 同类
# "让它多翻但仍有上限"场景已经验证过的量级，不用自己另起一个偏小的数字。
# 循环内部还有三层独立的自然终止条件（空页/重复页签名/翻页点击失败），
# 这个常量真正起作用的场景很少，主要是兜底，不是性能敏感路径。
_MAX_ORDERS_PAGE_SAFETY_NET = 200


def run_phase1(
    page,
    max_orders_override: int | None = None,
) -> tuple[list[str], dict[str, str]]:
    """Phase 1: 采集可申诉订单号及问题类型，记录到数据库。

    max_orders_override：命令行 --max-orders 传下来的一次性覆盖值。
    None（未传该参数）时改用这家店铺在数据库里配置的 max_orders
    （db.get_shop_max_orders()，同样可能是 None——表示这家店铺没配置过，
    维持这次改动上线前的默认行为：只读申诉列表第一页）。命令行传了就
    以命令行为准，且只影响这一次运行，不会写回数据库——持久化配置只能
    通过 GUI「店铺管理」页的「设置处理上限」对话框（最终也是调用
    db.set_shop_max_orders()）完成，两条路径共享同一份存储。

    返回 (order_numbers, order_issues)。
    """
    print()
    print("=" * 60)
    print("Phase 1: 采集可申诉订单号")
    print("=" * 60)

    open_appeal_list_page(page)
    if not open_appealable_orders_table(page):
        print("未发现可申诉订单，退出。")
        return [], {}

    shop = SHOP_ID or "default"
    if max_orders_override is not None:
        max_orders, max_orders_source = max_orders_override, "命令行 --max-orders 覆盖"
    else:
        max_orders, max_orders_source = db.get_shop_max_orders(shop), "店铺配置"

    if max_orders is None:
        # 没有任何覆盖/配置——调用方式跟这次改动上线前完全一样，不传
        # max_pages/max_orders，用 collect_all_appealable_order_numbers()
        # 自己的默认值（只读第一页），对这家店铺零行为变化。
        order_numbers, order_issues, order_details = (
            collect_all_appealable_order_numbers(page)
        )
    else:
        print(f"本次采集上限：{max_orders} 个订单（{max_orders_source}）")
        order_numbers, order_issues, order_details = (
            collect_all_appealable_order_numbers(
                page,
                max_pages=_MAX_ORDERS_PAGE_SAFETY_NET,
                max_orders=max_orders,
            )
        )

    record_collected_orders(order_numbers, order_issues, order_details)

    if not order_numbers:
        print("未发现可申诉订单，退出。")
    else:
        print(f"共采集到 {len(order_numbers)} 个可申诉订单号")
        # 用 in 而不是 == ：同一订单号可能对应多条记录，问题类型被拼接成
        # "商品问题、服务问题" 这种组合字符串（见 run_appeal_batch.py 的
        # collect_all_appealable_order_numbers），两类都算上才准确。
        goods_count = sum(1 for t in order_issues.values() if "商品问题" in t)
        service_count = sum(1 for t in order_issues.values() if "服务问题" in t)
        print(f"  商品问题: {goods_count}  服务问题: {service_count}")

    return order_numbers, order_issues


def run_phase2(
    page,
    order_numbers: list[str],
    order_issues: dict[str, str],
) -> list[str]:
    """Phase 2: 逐单查聊天记录 + AI 分析。返回成功分析的订单号列表。"""
    print()
    print("=" * 60)
    print("Phase 2: 查聊天记录 + AI 分析")
    print("=" * 60)

    if not order_numbers:
        print("没有订单号可分析，跳过。")
        return []

    analyzed_orders: list[str] = []

    for i, order_no in enumerate(order_numbers):
        print(f"\n[{i + 1}/{len(order_numbers)}] 正在分析订单 {order_no}")
        if i > 0:
            human_delay(1000, 3000)

        try:
            label, _ = process_single_order(
                page, order_no,
                issue_type=order_issues.get(order_no, ""),
            )
            analyzed_orders.append(order_no)
            print(f"  订单 {order_no} 分析完成 → {label}")
        except Exception as exc:
            print(f"  订单 {order_no} Phase 2 失败: {exc}")
            shot = save_error_screenshot(page, order_no)
            db.record_analysis_failed(
                SHOP_ID or "default", order_no, exc,
                screenshot_path=to_relative_evidence_path(shot) if shot else None,
            )

    print(f"\nPhase 2 完成: {len(analyzed_orders)}/{len(order_numbers)} 个订单分析成功")
    return analyzed_orders


def _summarize_results(record_outcomes: list[str]) -> str:
    """把一个订单号名下多条申诉记录各自的结果合并成一句话。

    只有一条记录、或者多条记录结果完全一致时，原样返回那个结果字符串——
    这样绝大多数订单（还是只有一条记录的那些）行为跟这次改动前完全一样，
    也兼容所有依赖精确字符串匹配的现有统计逻辑（比如主流程汇总报告里
    `s in ("成功", "已提交")` 这种判断，以及 run_shops_interleaved.py 的
    连续失败计数）。

    结果不一致时才用"N 条记录: ..."这种带明细的格式；只要其中有一条是
    失败，就在最前面加"失败: "前缀，让 `s.startswith("失败")` 这类判断
    依然能正确识别"这个订单有问题"。
    """
    if not record_outcomes:
        return "未找到申诉记录"
    if len(set(record_outcomes)) == 1:
        return record_outcomes[0]

    detail = "、".join(record_outcomes)
    has_failure = any(
        r.startswith("失败") or r.startswith("分析失败") for r in record_outcomes
    )
    prefix = "失败: " if has_failure else ""
    return f"{prefix}{len(record_outcomes)} 条记录: {detail}"


# detect_detail_page_status 返回的终态状态码 -> (给人看的简短描述, 落库/
# 返回的结果字符串)。跟 appeal_submit.py 的 TERMINAL_STATUS_MARKERS 一一
# 对应——那边负责"从页面文本识别出是哪种终态"，这边负责"这种终态要不要
# 跳过、跳过时打印什么、落库成什么状态"。以后 appeal_submit.py 那边新增
# 一种终态标记，这里对应加一行就行，不用再改 _process_one_appeal_record
# 的分支逻辑。
_TERMINAL_STATUSES: dict[str, tuple[str, str]] = {
    "ALREADY_APPEALED": ("已有申诉记录", "已申诉-跳过"),
    "EXPIRED": ("申诉窗口已过期、已被处罚", "已超时-跳过"),
    "REJECTED": ("申诉未通过、已被处罚", "已驳回-跳过"),
}


def _process_one_appeal_record(
    context, page, order_no: str,
    row, record_key: str, issue_type: str,
    order_issues: dict[str, str],
    confirm: bool,
) -> str:
    """处理订单号名下的**一条**具体申诉记录：点开这一行的详情→判定状态→
    填表→提交或取消。跟改动前 submit_single_order 单条记录那部分逻辑
    完全一致，只是操作对象从"隐含的唯一详情页"变成"这一行对应的详情页"，
    并且落库单位从"整个订单号"变成"这一条记录"（db.record_appeal_result，
    按 record_key 区分）。

    自己兜住异常（不向上抛），这样一个订单号名下某一条记录处理失败，不会
    连累同一订单号里其他记录不被处理——返回值就是这条记录的结果字符串，
    跟 submit_single_order 原来的返回值取值集合一致。
    """
    shop = SHOP_ID or "default"
    detail_page = None
    form_page = None
    existing_page_ids = {id(candidate) for candidate in context.pages}

    def _form_screenshot_path() -> str | None:
        """appeal_submit.py 的 fill_appeal_modal 填完表单后会尝试存一张
        DEBUG_DIR/appeal_form_filled_<单号>.png 截图——同一订单号的多条
        记录会共用这个文件名（后处理的记录会覆盖前一条的截图），这是
        目前的已知限制，不影响提交逻辑本身，只影响"事后回看填表截图对不
        对得上具体哪条记录"这件事。
        """
        p = DEBUG_DIR / f"appeal_form_filled_{safe_filename(order_no)}.png"
        return to_relative_evidence_path(p) if p.is_file() else None

    try:
        detail_link = get_row_detail_link(row)
        detail_page = open_row_detail_page(context, page, detail_link, order_no)
        business_read_page(detail_page)

        status = detect_detail_page_status(detail_page)
        print(f"  订单 {order_no}（记录 {record_key}）详情页状态: {status}")

        if status in _TERMINAL_STATUSES:
            description, result_text = _TERMINAL_STATUSES[status]
            detail_page.close()
            print(f"  订单 {order_no}（记录 {record_key}）{description}，跳过 ✓")
            db.record_appeal_result(shop, order_no, record_key, issue_type, result_text)
            return result_text

        classification, appeal_text, order_issue_type = load_order_classification(order_no)

        # issue_type 优先用这一行自己的问题类型（这才是权威来源），采集
        # 阶段传下来的 order_issues 字典、聊天 JSON 里的值依次作为备选。
        effective_issue = issue_type or order_issues.get(order_no) or order_issue_type

        form_page = fill_appeal_modal(
            detail_page, order_no, classification, appeal_text, context,
            issue_type=effective_issue,
        )

        if confirm:
            if form_page is not None:
                click_appeal_form_submit(form_page, "C")
                form_page.close()
            else:
                # form_page 为 None 说明表单在 detail_page 的模态框里
                # （商品问题 A/B + 服务问题）。提交按钮一律走模态框
                # 分支，不能沿用 detect_detail_page_status 的 status——
                # 服务问题详情页可能被误判为 "C"，导致去独立页里找
                # type="submit" 而找不到（服务问题提交按钮是 type="button"）。
                click_appeal_form_submit(detail_page, "A")
            result_text = "已提交"
        else:
            if form_page is not None:
                click_appeal_form_cancel(form_page, "C")
                form_page.close()
            else:
                click_appeal_form_cancel(detail_page, "A")
            result_text = "成功"

        detail_page.close()
        print(f"  订单 {order_no}（记录 {record_key}）模拟申诉完成 ✓")
        db.record_appeal_result(
            shop, order_no, record_key, effective_issue, result_text,
            form_screenshot_path=_form_screenshot_path(),
        )
        return result_text

    except Exception as exc:
        print(f"  订单 {order_no}（记录 {record_key}）处理失败: {exc}")

        _close_pages_opened_since(
            context,
            existing_page_ids,
            protected_pages=(page,),
        )

        error_screenshot = None
        try:
            shot = save_error_screenshot(page, order_no)
            if shot is not None:
                error_screenshot = to_relative_evidence_path(shot)
        except Exception:
            pass

        result_text = f"失败: {exc}"
        db.record_appeal_result(
            shop, order_no, record_key, issue_type, result_text,
            form_screenshot_path=_form_screenshot_path(),
            error_screenshot_path=error_screenshot,
        )
        return result_text


@paced_business_operations
def submit_single_order(
    context, page, order_no: str,
    order_issues: dict[str, str],
    confirm: bool = False,
) -> tuple[str, str]:
    """处理一个订单号名下的**所有**申诉记录：搜索一次 → 逐条处理（打开
    详情→判定状态→填表→提交或取消）→ 直到这次搜到的记录都处理完。

    一个订单号在拼多多那边可能对应不止一条独立的"消费者负向体验"记录
    （商品问题一条、服务问题一条，甚至同一问题类型也可能不止一条）——
    这个函数不再假设只有一条。逐条处理交给 _process_one_appeal_record，
    这里只负责"数一共有几条、按顺序把每条派发出去、汇总结果"。

    dry-run（不加 --confirm）模式下处理完一条不会让它从搜索结果里消失，
    所以不能靠"重新搜索、数量变少了"判断进度，得在本地记住这次循环里
    已经处理过哪些 record_key，直到处理数量达到一开始数到的总数（或者
    找不到更多未处理的行）才停止，另加一个安全上限防御任何意外情况。

    返回 (order_no, result_text)——对外接口跟改动前完全一样，见
    _summarize_results 如何把多条记录的结果合并成一个字符串。
    """
    shop = SHOP_ID or "default"
    existing_page_ids = {id(candidate) for candidate in context.pages}

    try:
        page.bring_to_front()
        open_appeal_search_page(page)
        rows = search_and_get_rows(page, order_no)
        initial_count = rows.count()

        if initial_count == 0:
            print(f"  订单 {order_no} 找不到申诉记录，跳过 ✓")
            db.record_submit_result(shop, order_no, "未找到申诉记录")
            return (order_no, "未找到申诉记录")

        if initial_count > 1:
            print(f"  订单 {order_no} 查到 {initial_count} 条记录，将逐条处理。")

        outcomes: list[str] = []
        processed_keys: set[str] = set()
        max_iterations = initial_count + 3  # 安全上限，防御表格状态跟预期不符导致死循环

        for _ in range(max_iterations):
            if len(processed_keys) >= initial_count:
                break

            current_rows = get_result_rows(page, order_no)  # 重新取，防御表格发生变化
            target_row = None
            target_key = None
            for i in range(current_rows.count()):
                row = current_rows.nth(i)
                key = get_row_record_key(row)
                if key not in processed_keys:
                    target_row = row
                    target_key = key
                    break

            if target_row is None:
                break  # 没有更多未处理的行了

            row_issue_type = get_row_issue_type(target_row)
            outcome = _process_one_appeal_record(
                context, page, order_no, target_row, target_key,
                row_issue_type, order_issues, confirm,
            )
            processed_keys.add(target_key)
            outcomes.append(outcome)

        result_text = _summarize_results(outcomes)
        db.record_submit_result(shop, order_no, result_text)
        return (order_no, result_text)

    except Exception as exc:
        print(f"  订单 {order_no} 模拟申诉失败: {exc}")

        _close_pages_opened_since(
            context,
            existing_page_ids,
            protected_pages=(page,),
        )

        error_screenshot = None
        try:
            shot = save_error_screenshot(page, order_no)
            if shot is not None:
                error_screenshot = to_relative_evidence_path(shot)
        except Exception:
            pass

        db.record_submit_result(
            shop, order_no, f"失败: {exc}",
            error_screenshot_path=error_screenshot,
        )
        return (order_no, f"失败: {exc}")


def run_phase3(
    context, page, order_numbers: list[str],
    order_issues: dict[str, str],
    confirm: bool = False,
) -> list[tuple[str, str]]:
    """Phase 3: 逐单模拟申诉填表（批量，全部订单跑完）。返回 (订单号, 结果) 列表。"""
    print()
    print("=" * 60)
    if confirm:
        print("Phase 3: 模拟申诉填表 + 提交（⚠ 会真实提交）")
    else:
        print("Phase 3: 模拟申诉填表 + 取消（不提交）")
    print("=" * 60)

    if not order_numbers:
        print("没有订单号可模拟，跳过。")
        return []

    results: list[tuple[str, str]] = []

    for i, order_no in enumerate(order_numbers):
        print(f"\n[{i + 1}/{len(order_numbers)}] 正在模拟申诉 {order_no}")
        if i > 0:
            human_delay(1000, 3000)

        results.append(submit_single_order(context, page, order_no, order_issues, confirm))

    return results


@paced_business_operations
def process_and_submit_one_order(
    context, chat_page, order_no: str,
    order_issues: dict[str, str],
    confirm: bool = False,
    reanalyze: bool = False,
    appeal_page=None,
) -> tuple[str, str]:
    """处理单个订单：如未分析过（或 reanalyze=True）则先分析，再提交/取消。

    从 run_per_order_pipeline 循环体中抽出，供它（批量逐单循环）和
    run_serve_mode（serve 模式响应单条 "next" 指令）共用，避免两处重复
    "先分析、再提交"的判断逻辑。

    默认跳过已经分析过的订单（数据库里已有 classification + appeal_text），
    直接进入提交。

    返回 (order_no, result_text)。分析阶段异常时 result_text 形如
    "分析失败: {exc}" 且不会进入提交；否则取值集合与 submit_single_order
    一致（"未找到申诉记录" / 终态跳过类（见 _TERMINAL_STATUSES，都以
    "-跳过" 结尾）/ "已提交" / "成功" / "失败: {exc}"，多条记录结果不
    一致时见 _summarize_results）。
    """
    if reanalyze or not _order_has_analysis(order_no):
        try:
            label, _ = process_single_order(
                chat_page, order_no,
                issue_type=order_issues.get(order_no, ""),
            )
            print(f"  订单 {order_no} 分析完成 → {label}")
        except Exception as exc:
            print(f"  订单 {order_no} 分析失败: {exc}，跳过本单提交")
            shot = save_error_screenshot(chat_page, order_no)
            db.record_analysis_failed(
                SHOP_ID or "default", order_no, exc,
                screenshot_path=to_relative_evidence_path(shot) if shot else None,
            )
            return (order_no, f"分析失败: {exc}")
    else:
        print(f"  订单 {order_no} 已有分析结果，跳过重新分析")

    return submit_single_order(
        context,
        appeal_page if appeal_page is not None else chat_page,
        order_no,
        order_issues,
        confirm,
    )
def run_per_order_pipeline(
    context, chat_page, order_numbers: list[str],
    order_issues: dict[str, str],
    confirm: bool = False,
    reanalyze: bool = False,
    appeal_page=None,
) -> list[tuple[str, str]]:
    """逐单模式：对每个订单，分析这一单 → 立刻提交/取消这一单 → 再处理下一单。

    与"先全量分析、再全量提交"的区别在于粒度——这里"提交"的最小单位就是
    一个订单，便于以后接多店铺轮流提交的调度器（提交完一单就能换店铺）。

    返回 (订单号, 结果) 列表，取值集合见 process_and_submit_one_order。
    """
    print()
    print("=" * 60)
    if confirm:
        print("逐单模式：分析 + 提交（⚠ 会真实提交）")
    else:
        print("逐单模式：分析 + 模拟提交（点取消，不真实提交）")
    print("=" * 60)

    if not order_numbers:
        print("没有订单号可处理，跳过。")
        return []

    results: list[tuple[str, str]] = []

    for i, order_no in enumerate(order_numbers):
        print(f"\n[{i + 1}/{len(order_numbers)}] 正在处理订单 {order_no}")
        if i > 0:
            human_delay(1000, 3000)

        if appeal_page is not None:
            chat_page = _get_or_create_role_page(
                context,
                chat_page,
                _CHAT_PAGE_FRAGMENT,
                excluded_pages=(appeal_page,),
            )
            appeal_page = _get_or_create_role_page(
                context,
                appeal_page,
                _APPEAL_PAGE_FRAGMENT,
                excluded_pages=(chat_page,),
            )

        results.append(
            process_and_submit_one_order(
                context,
                chat_page,
                order_no,
                order_issues,
                confirm,
                reanalyze,
                appeal_page=appeal_page,
            )
        )

    submit_success = sum(1 for _, r in results if r in ("成功", "已提交"))
    analyze_failed = sum(1 for _, r in results if r.startswith("分析失败"))
    print(
        f"\n逐单模式完成: 共处理 {len(order_numbers)} 个订单，"
        f"{submit_success} 个成功提交/模拟，{analyze_failed} 个分析失败跳过"
    )
    return results


def run_serve_mode(
    playwright, real_stdout,
    confirm: bool = False,
    reanalyze: bool = False,
    max_orders_override: int | None = None,
) -> None:
    """常驻模式：在 stdin/real_stdout 上跑 JSON Lines 指令协议，供外部调度器
    （如 run_shops_interleaved.py）逐单点名调用，实现"提交完一单换店铺"。

    调用前提：main() 已经把 sys.stdout 换成 sys.stderr，real_stdout 是原始
    stdout 的引用——本函数只通过 real_stdout 发协议消息；调用链上其余代码里
    所有既有 print()（process_single_order / submit_single_order /
    fill_appeal_modal ……）不用改一行，天然跟着走已切换的 sys.stdout（=
    stderr），两条通道不会混在一起，调度器那边 json.loads() 不会被人类可读
    日志干扰。

    浏览器（create_stealth_context + wait_for_login）故意不在这里提前开，
    而是延后到收到第一条 init 指令时才真正启动——调度器起 N 个店铺的子
    进程时几乎是同时 Popen 的，如果子进程一启动就自己去开浏览器、查登录态，
    那就变成 N 家店铺在几乎同一时刻各自连拼多多，跟"同一时刻只有一家在
    操作"这个初衷正好相反（店铺数量越多，这个瞬时并发窗口越明显）。延后到
    init 指令才启动，就能让浏览器操作从头到尾严格跟着调度器的串行节奏走。

    协议（每行一个 JSON 对象）：
      调度器 → 本进程（stdin）：
        {"cmd": "init"}  首次取得发言权时发，才真正开浏览器+登录检测，
                          再跑一次 Phase 1 采集，回 ready
        {"cmd": "next"}  处理队列里下一单，回 result / queue_empty / error
        {"cmd": "stop"}  关浏览器（如果已经开过），退出循环
      本进程 → 调度器（real_stdout）：
        {"type": "ready", "queue_size": N}
        {"type": "result", "order_no": "...", "result": "..."}
        {"type": "queue_empty"}
        {"type": "error", "stage": "init"|"order"|"protocol", "message": "...",
         "order_no": "..."（仅 stage=="order" 时有）}

    本函数不做"现在该不该轮到我"的判断——那是调度器的职责；这里只是"收到
    指令就干活、干完就回报"，天然保证浏览器操作只发生在收到 init/next 之后。
    """

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    context = None
    chat_page = None
    appeal_page = None
    order_numbers: list[str] = []
    order_issues: dict[str, str] = {}
    next_index = 0

    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            emit({"type": "error", "stage": "protocol", "message": f"无法解析指令: {raw_line!r}"})
            continue

        cmd = msg.get("cmd")

        if cmd == "init":
            try:
                if context is None:
                    # 第一次收到 init 才真正启动浏览器 + 登录检测，见函数
                    # 文档字符串里"延后启动"的说明。
                    context = create_stealth_context(
                        playwright, user_data_dir=str(PROFILE_DIR),
                        close_warn_center=True,
                    )
                    login_page = context.pages[0] if context.pages else context.new_page()
                    # interactive=False：登录失效/未登录直接抛异常，不等人工
                    # 按回车——serve 模式下没人守着终端，见 wait_for_login 文档。
                    chat_page = wait_for_login(
                        context=context,
                        page=login_page,
                        interactive=False,
                    )
                    appeal_page = _get_or_create_role_page(
                        context,
                        appeal_page,
                        _APPEAL_PAGE_FRAGMENT,
                        excluded_pages=(chat_page,),
                    )

                    # 登录成功后才打开"验证码自动破解 3 次都失败就直接放弃、
                    # 跳过当前任务"——serve 模式下没人盯着屏幕，登录之后的
                    # 采集/分析/提交这些逐单动作没必要为了等一个不存在的人工
                    # 干等 5 分钟；但登录本身还没走到这一步，仍然保留"等
                    # 人工"（见 captcha.set_give_up_after_auto_solve_fails
                    # 文档），只是 serve 模式下 interactive=False 让登录失败
                    # 走的是另一条"直接抛异常"的路，不经过这个开关。
                    set_give_up_after_auto_solve_fails(True)

                resume = msg.get('resume')
                if resume:
                    order_numbers = resume['order_numbers']
                    order_issues = resume['order_issues']
                else:
                    order_numbers, order_issues = run_phase1(
                        appeal_page, max_orders_override=max_orders_override,
                    )
                next_index = 0
                emit({"type": "ready", "queue_size": len(order_numbers)})
            except Exception as exc:
                emit({"type": "error", "stage": "init", "message": str(exc)})

        elif cmd == "next":
            if next_index >= len(order_numbers):
                emit({"type": "queue_empty"})
                continue
            order_no = order_numbers[next_index]
            next_index += 1
            if next_index > 1:
                human_delay(1000, 3000)
            try:
                # 固定页面被人工关闭或因页面异常失效时，只补建对应用途的
                # 标签页；另一张固定页和整个店铺登录会话都继续复用。
                chat_page = _get_or_create_role_page(
                    context,
                    chat_page,
                    _CHAT_PAGE_FRAGMENT,
                    excluded_pages=(appeal_page,),
                )
                appeal_page = _get_or_create_role_page(
                    context,
                    appeal_page,
                    _APPEAL_PAGE_FRAGMENT,
                    excluded_pages=(chat_page,),
                )
                chat_page.bring_to_front()
                emit({"type": "activity", "stage": "分析并提交申诉", "order_no": order_no})
                _, result_text = process_and_submit_one_order(
                    context,
                    chat_page,
                    order_no,
                    order_issues,
                    confirm,
                    reanalyze,
                    appeal_page=appeal_page,
                )
                emit({"type": "result", "order_no": order_no, "result": result_text,
                      "page_count": len(context.pages),
                      "_resume": ({"order_numbers": order_numbers[next_index:], "order_issues": order_issues}
                                  if next_index < len(order_numbers) else None)})
            except Exception as exc:
                emit({"type": "error", "stage": "order", "order_no": order_no, "message": str(exc)})

        elif cmd == "stop":
            break

        else:
            emit({"type": "error", "stage": "protocol", "message": f"未知指令: {cmd!r}"})

    if context is not None:
        try:
            context.close()
        except Exception:
            pass


def run_login_only_mode(playwright, real_stdout) -> None:
    """轻量登录模式：只开浏览器 + 轮询检测登录状态，不跑任何采集/分析/
    提交阶段。供 gui/ 子项目的"新增店铺/重新登录"功能调用。

    跟 --serve 的 init 指令刻意区分开，不复用它：
    - --serve 的 init 用 wait_for_login(interactive=False)——检测不到
      登录直接抛异常，不等待，这是给"调度器无人值守批量跑单"场景设计的，
      跟"新增店铺时就是需要等人工完成登录"这个语义相反；
    - 默认全流程（不带任何阶段参数）用 wait_for_login(interactive=True)——
      靠阻塞的 input() 等人工在终端按回车，GUI 场景没有终端可以让人在
      那按回车；
    - init 还会顺带跑一次 Phase 1 采集，这个模式只要登录，不需要这个
      副作用。

    协议（复用 run_serve_mode 那套"real_stdout 单独发协议消息、UTF-8
    编码钉死"的基础设施，指令集比 --serve 简单很多）：
      本进程 → 调用方（real_stdout）：
        {"type": "login_result", "success": true}
        {"type": "login_result", "success": false, "message": "..."}
      调用方 → 本进程（stdin，可选）：
        {"cmd": "close"}   收到后关闭 context 并退出；不发也没关系，
                            stdin 读到 EOF（调用方进程退出/管道关闭）
                            同样会触发关闭退出。

    检测成功后**不会立刻关闭浏览器**：cookie/localStorage 落盘依赖
    context 干净关闭，太早关掉可能让登录态没保存全，所以要等调用方明确
    发 close 指令（GUI 那边应该在收到 login_result 之后主动发 close）。

    create_stealth_context(...)（真正启动浏览器进程）跟后面的登录轮询
    一起包在同一个 try/except 里，结构上刻意跟 run_serve_mode 的 init
    分支（见上）对齐：浏览器启动失败（比如目标机器缺 Visual C++ 运行库、
    随包浏览器文件损坏等）也是一种"登录没成功"，同样要通过 login_result
    消息把具体错误文本带回调用方，不能是调用方什么协议消息都收不到、
    只能靠子进程退出码去猜原因的裸崩溃（这曾经是真实发生过的问题——
    浏览器启动一旦在这里抛异常，会一路不受控制地冒泡到进程顶层，子进程
    直接带着一个语焉不详的退出码崩溃退出）。
    """

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    context = None
    try:
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
        page = context.pages[0] if context.pages else context.new_page()
        success, message = wait_for_login_polling(context=context, page=page)
        emit({"type": "login_result", "success": success, "message": message})
    except Exception as exc:
        emit({"type": "login_result", "success": False, "message": str(exc)})

    # 保持 context 打开，等调用方发 close 指令（或 stdin EOF）才退出。
    # 上面 try 块失败时 context 可能仍是 None（比如 create_stealth_context
    # 自己就抛了）——login_result 已经把失败原因发出去了，调用方按协议
    # 应该会主动关闭 stdin/发 close，这里照样能从循环正常退出，不需要
    # 特殊处理。
    for raw_line in sys.stdin:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            msg = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if msg.get("cmd") == "close":
            break

    if context is not None:
        try:
            context.close()
        except Exception:
            pass


def run_account_login_mode(playwright, real_stdout) -> None:
    """Receive one credential pair over stdin and perform a bounded login."""

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    raw = sys.stdin.readline()
    try:
        request = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        emit({"type": "login_result", "success": False, "message": "未收到有效的登录账号数据。"})
        return
    username = str(request.get("username") or "")
    password = str(request.get("password") or "")
    if request.get("cmd") != "login" or not username or not password:
        emit({"type": "login_result", "success": False, "message": "登录账号或密码为空。"})
        return

    stop_event = threading.Event()

    def read_commands() -> None:
        for command_line in sys.stdin:
            try:
                command = json.loads(command_line)
            except json.JSONDecodeError:
                continue
            if command.get("cmd") in {"stop", "close"}:
                stop_event.set()
                return

    threading.Thread(target=read_commands, daemon=True).start()
    context = None
    success = False
    message = "账号登录操作未完成。"
    try:
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
        page = context.pages[0] if context.pages else context.new_page()
        success, message = account_login(
            context,
            page,
            username,
            password,
            stop_event,
            progress=lambda text: emit({"type": "login_progress", "message": text}),
        )
    except AccountLoginError as exc:
        message = str(exc)
    except Exception:  # noqa: BLE001 - return a credential-safe protocol message
        message = "浏览器启动或账号登录操作失败，请检查运行环境后重试。"
    finally:
        # Drop references before closing the persistent context. Neither value is logged.
        request.clear()
        username = ""
        password = ""
        if context is not None:
            try:
                context.close()
            except Exception:  # noqa: BLE001 - persistent context close is best effort
                if success:
                    success = False
                    message = "登录完成，但保存登录状态时关闭浏览器失败，请重试。"
    emit({"type": "login_result", "success": success, "message": message})


def run_check_appeal_results_mode(playwright, real_stdout, since_days: int, submitted_since: str | None = None) -> None:
    """查询申诉结果模式：先确定本地目标，再直接进入申诉列表或详情。
    没有符合条件的订单时明确上报原因，不打开浏览器。实际查询在业务页面
    检测登录失效，不再借聊天记录页检测登录状态。

    跟 --login-only 不同：这里不需要在成功后保持浏览器打开等
    {"cmd": "close"} 指令——整个检查动作是同步完成的，走到这里所有数据库
    写入都已经落地，没有"还有异步的人工操作没做完"这种需要延迟关闭的
    场景，直接在 finally 里关 context 即可（跟 run_appeal_batch.py 的
    main() 是同一种收尾方式）。

    协议（只有本进程 -> 调用方一个方向，调用方不需要发任何指令）：
      {"type": "progress", "page": N, "checked": N, "matched": M}
      {"type": "check_result", "success": true, "updated_count": K,
       "orders": [{"order_no": ..., "record_key": ..., "result_text": ...}, ...]}
      {"type": "check_result", "success": false, "message": "..."}
    """

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    context = None
    page = None
    try:
        targets, empty_reason = check_appeal_results.prepare_result_query(
            SHOP_ID or "default", submitted_since,
        )
        if not targets:
            emit({"type": "check_result", "success": True, "skipped": True,
                  "updated_count": 0, "orders": [], "message": empty_reason})
            return
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
        page = context.pages[0] if context.pages else context.new_page()

        set_give_up_after_auto_solve_fails(True)

        def on_progress(checked: int, matched: int, page_no: int) -> None:
            emit({"type": "progress", "page": page_no, "checked": checked, "matched": matched})

        updated = check_appeal_results.check_appeal_results(
            page, SHOP_ID or "default",
            on_progress=on_progress,
            submitted_since=submitted_since,
            target_orders=targets,
        )
        emit({"type": "check_result", "success": True, "updated_count": len(updated), "orders": updated})

    except Exception as exc:
        error_screenshot = None
        try:
            if page is not None and not page.is_closed():
                shot = save_error_screenshot(page, f"check_results_{SHOP_ID or 'default'}")
                if shot is not None:
                    error_screenshot = to_relative_evidence_path(shot)
        except Exception:
            pass
        message = str(exc)
        if error_screenshot:
            message += f"（现场截图：{error_screenshot}）"
        emit({"type": "check_result", "success": False, "message": message})

    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_check_reconsideration_results_mode(playwright, real_stdout, submitted_since: str | None = None) -> None:
    """一次性查询一家店铺的全部复议记录分页并通过 JSON Lines 汇报。"""
    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    context = None
    page = None
    try:
        targets, empty_reason = check_reconsideration_results.prepare_result_query(SHOP_ID or "default", submitted_since)
        if not targets:
            emit({"type": "reconsider_check_result", "success": True, "skipped": True,
                  "complete": True, "updated_count": 0, "updates": [], "message": empty_reason})
            return
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
        page = context.pages[0] if context.pages else context.new_page()
        set_give_up_after_auto_solve_fails(True)

        def on_progress(page_no: int, checked: int, matched: int, updated: int) -> None:
            emit({
                "type": "reconsider_check_progress",
                "page": page_no,
                "checked": checked,
                "matched": matched,
                "updated": updated,
            })

        result = check_reconsideration_results.check_reconsideration_results(
            page,
            SHOP_ID or "default",
            on_progress=on_progress,
            submitted_since=submitted_since,
            target_orders=targets,
        )
        updates = result.get("updates", [])
        emit({
            "type": "reconsider_check_result",
            "success": True,
            "updated_count": len(updates),
            **result,
        })
    except Exception as exc:
        error_screenshot = None
        try:
            if page is not None and not page.is_closed():
                shot = save_error_screenshot(
                    page, f"check_reconsider_results_{SHOP_ID or 'default'}"
                )
                if shot is not None:
                    error_screenshot = to_relative_evidence_path(shot)
        except Exception:
            pass
        message = str(exc)
        if error_screenshot:
            message += f"（现场截图：{error_screenshot}）"
        emit({
            "type": "reconsider_check_result",
            "success": False,
            "updated_count": 0,
            "message": message,
        })
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_reconsideration_serve_mode(
    playwright,
    real_stdout,
    confirm: bool,
    max_orders: int | None = None,
) -> None:
    """复议长驻模式：扫描一次后，每次 ``next`` 只处理一条复议记录。

    stdin 由后台线程统一读取，收到 ``stop`` 时会立刻设置 stop_event；因此
    主线程即便正在等待验证码或提交结果，也能在既有安全边界感知停止请求。
    其它命令进入队列，由 Playwright 所在主线程串行执行，绝不跨线程操作页面。
    """

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    command_queue: "queue.Queue[dict]" = queue.Queue()
    stop_event = threading.Event()

    def read_commands() -> None:
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except json.JSONDecodeError:
                    command_queue.put({
                        "cmd": "invalid",
                        "message": f"无法解析指令: {line.strip()!r}",
                    })
                    continue
                if command.get("cmd") == "stop":
                    stop_event.set()
                command_queue.put(command)
        except (OSError, ValueError):
            pass
        finally:
            # 父进程关闭管道时不要让本进程永久卡在 queue.get()。
            stop_event.set()
            command_queue.put({"cmd": "stop"})

    threading.Thread(target=read_commands, daemon=True).start()

    context = None
    chat_page = None
    reconsideration_page = None
    pending_tasks: list[dict[str, str]] | None = None
    next_index = 0

    try:
        while True:
            command = command_queue.get()
            cmd = command.get("cmd")

            if cmd == "stop":
                break

            if cmd == "invalid":
                emit({
                    "type": "reconsider_error",
                    "stage": "protocol",
                    "message": command.get("message") or "无法解析指令",
                })
                continue

            if cmd == "init":
                try:
                    if context is None:
                        context = create_stealth_context(
                            playwright, user_data_dir=str(PROFILE_DIR), close_warn_center=True,
                        )
                        page = (
                            context.pages[0]
                            if context.pages
                            else context.new_page()
                        )
                        chat_page = wait_for_login(
                            context=context,
                            page=page,
                            interactive=False,
                        )
                        reconsideration_page = next(
                            (
                                candidate
                                for candidate in reversed(context.pages)
                                if candidate is not chat_page
                                and not candidate.is_closed()
                                and "negativeExperienceReconsider" in candidate.url
                            ),
                            None,
                        )
                        if reconsideration_page is None:
                            reconsideration_page = context.new_page()
                        set_give_up_after_auto_solve_fails(True)

                    def on_progress(stage: str, current: int, total: int) -> None:
                        emit({
                            "type": "reconsider_progress",
                            "stage": stage,
                            "current": current,
                            "total": total,
                        })

                    resume = command.get('resume')
                    if resume:
                        pending_tasks = resume['pending_tasks']
                        next_index = resume['next_index']
                    else:
                        pending_tasks = reconsideration.collect_pending_reconsideration_tasks(
                            reconsideration_page,
                            SHOP_ID or "default",
                            max_orders=max_orders if max_orders is not None else 20,
                            on_progress=on_progress,
                            should_stop=stop_event.is_set,
                        )
                        next_index = 0
                    emit({
                        "type": "reconsider_ready",
                        "queue_size": len(pending_tasks) - next_index,
                    })
                except reconsideration.ReconsiderationStopped:
                    emit({"type": "reconsider_stopped", "stage": "init"})
                    break
                except Exception as exc:
                    emit({
                        "type": "reconsider_error",
                        "stage": "init",
                        "message": str(exc),
                    })
                    break
                continue

            if cmd == "next":
                if pending_tasks is None or chat_page is None or reconsideration_page is None:
                    emit({
                        "type": "reconsider_error",
                        "stage": "protocol",
                        "message": "尚未完成 init，不能处理下一单。",
                    })
                    continue
                if next_index >= len(pending_tasks):
                    emit({"type": "reconsider_queue_empty"})
                    continue

                current = next_index + 1
                task = pending_tasks[next_index]
                try:
                    reconsideration_page.bring_to_front()
                    emit({
                        "type": "activity",
                        "stage": "正在发起复议",
                        "order_no": str(task.get("order_no") or ""),
                    })
                    outcome = reconsideration.process_reconsideration_task(
                        reconsideration_page,
                        chat_page,
                        SHOP_ID or "default",
                        task,
                        confirm=confirm,
                        current=current,
                        total=len(pending_tasks),
                        on_progress=on_progress,
                        should_stop=stop_event.is_set,
                    )
                except reconsideration.ReconsiderationStopped:
                    emit({
                        "type": "reconsider_stopped",
                        "stage": "order",
                        "order_no": task.get("order_no", ""),
                    })
                    break

                next_index += 1
                emit({
                    "type": "reconsider_order_result",
                    "order_no": outcome["order_no"],
                    "result": outcome["result"],
                    "current": next_index,
                    "total": len(pending_tasks),
                    "remaining": len(pending_tasks) - next_index,
                    "stopped": stop_event.is_set(),
                    "page_count": len(context.pages),
                    "_resume": ({"pending_tasks": pending_tasks, "next_index": next_index}
                                if next_index < len(pending_tasks) else None),
                })
                if stop_event.is_set():
                    break
                continue

            emit({
                "type": "reconsider_error",
                "stage": "protocol",
                "message": f"未知指令: {cmd!r}",
            })
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_reconsideration_mode(
    playwright,
    real_stdout,
    confirm: bool,
    max_orders: int | None = None,
) -> None:
    """一次性复议模式，使用 JSON Lines 向 GUI 汇报扫描和逐单进度。"""
    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    stop_event = threading.Event()

    def listen_for_stop() -> None:
        """GUI 通过 stdin 请求协作式停止；线程只设置标记，不碰 Playwright。"""
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if command.get("cmd") == "stop":
                    stop_event.set()
                    return
        except (OSError, ValueError):
            return

    threading.Thread(target=listen_for_stop, daemon=True).start()

    context = None
    page = None
    reconsideration_page = None
    try:
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR), close_warn_center=True)
        page = context.pages[0] if context.pages else context.new_page()
        chat_page = wait_for_login(context=context, page=page, interactive=False)
        # 聊天查询与复议列表各占一个长期标签页。优先复用浏览器里已有的
        # 复议标签；没有才新建。整个店铺处理完之前都不关闭、不重复创建。
        reconsideration_page = next(
            (
                candidate
                for candidate in reversed(context.pages)
                if candidate is not chat_page
                and not candidate.is_closed()
                and "negativeExperienceReconsider" in candidate.url
            ),
            None,
        )
        if reconsideration_page is None:
            reconsideration_page = context.new_page()
        set_give_up_after_auto_solve_fails(True)

        def on_progress(stage: str, current: int, total: int) -> None:
            emit({
                "type": "reconsider_progress",
                "stage": stage,
                "current": current,
                "total": total,
            })

        outcomes, stopped = reconsideration.run_reconsiderations(
            reconsideration_page,
            chat_page,
            SHOP_ID or "default",
            confirm=confirm,
            max_orders=max_orders if max_orders is not None else 20,
            on_progress=on_progress,
            should_stop=stop_event.is_set,
        )
        submitted = sum(item["result"] == "已提交复议" for item in outcomes)
        pending_confirmation = sum(
            item["result"] == "复议结果待确认" for item in outcomes
        )
        failed = sum(item["result"].startswith("失败：") for item in outcomes)
        emit({
            "type": "reconsider_result",
            "success": failed == 0,
            "processed_count": len(outcomes),
            "submitted_count": submitted,
            "pending_confirmation_count": pending_confirmation,
            "failed_count": failed,
            "stopped": stopped,
            "orders": outcomes,
            "message": (
                "已按要求停止"
                if stopped
                else ("" if failed == 0 else f"有 {failed} 条复议处理失败")
            ),
        })
    except Exception as exc:
        error_screenshot = None
        try:
            error_page = reconsideration_page or page
            if error_page is not None and not error_page.is_closed():
                shot = save_error_screenshot(
                    error_page, f"reconsider_{SHOP_ID or 'default'}"
                )
                if shot is not None:
                    error_screenshot = to_relative_evidence_path(shot)
        except Exception:
            pass
        message = str(exc)
        if error_screenshot:
            message += f"（现场截图：{error_screenshot}）"
        emit({"type": "reconsider_result", "success": False, "message": message})
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_check_aftersale_results_mode(
    playwright, real_stdout,
    query_interval_seconds: int = aftersale_result_settings.DEFAULT_QUERY_INTERVAL_SECONDS,
    submitted_since: str | None = None,
) -> None:
    """一次性查询一家店铺的售后维权申诉审核结果。"""
    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    context = None
    page = None
    try:
        targets, empty_reason = check_aftersale_appeal_results.prepare_result_query(SHOP_ID or "default", submitted_since)
        if not targets:
            emit({"type": "aftersale_check_result", "success": True, "skipped": True,
                  "complete": True, "updated_count": 0, "updates": [], "message": empty_reason})
            return
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
        page = context.pages[0] if context.pages else context.new_page()
        # 售后结果查询直接进入结果列表，由列表加载情况校验会话。
        # wait_for_login 会强制打开聊天查询页，此模式不需要该页面。
        set_give_up_after_auto_solve_fails(True)

        def on_progress(page_no: int, checked: int, matched: int, updated: int) -> None:
            emit({
                "type": "aftersale_check_progress",
                "page": page_no,
                "checked": checked,
                "matched": matched,
                "updated": updated,
            })

        result = check_aftersale_appeal_results.check_aftersale_appeal_results(
            page,
            SHOP_ID or "default",
            on_progress=on_progress,
            query_interval_seconds=query_interval_seconds,
            submitted_since=submitted_since,
            target_orders=targets,
        )
        emit({
            "type": "aftersale_check_result",
            "success": True,
            "updated_count": len(result.get("updates", [])),
            **result,
        })
    except Exception as exc:
        error_screenshot = None
        try:
            if page is not None and not page.is_closed():
                shot = save_error_screenshot(
                    page, f"check_aftersale_results_{SHOP_ID or 'default'}"
                )
                if shot is not None:
                    error_screenshot = to_relative_evidence_path(shot)
        except Exception:
            pass
        message = str(exc)
        if error_screenshot:
            message += f"（现场截图：{error_screenshot}）"
        emit({
            "type": "aftersale_check_result",
            "success": False,
            "updated_count": 0,
            "message": message,
        })
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_aftersale_serve_mode(
    playwright,
    real_stdout,
    max_orders: int | None = None,
) -> None:
    """售后申诉长驻协议：init 初始化店铺，next 每次真实提交一单。"""

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    command_queue: queue.Queue[dict] = queue.Queue()
    stop_event = threading.Event()

    def read_commands() -> None:
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except json.JSONDecodeError:
                    command_queue.put({
                        "cmd": "invalid",
                        "message": f"无法解析指令: {line.strip()!r}",
                    })
                    continue
                if command.get("cmd") == "stop":
                    stop_event.set()
                command_queue.put(command)
        except (OSError, ValueError):
            pass
        finally:
            stop_event.set()
            command_queue.put({"cmd": "stop"})

    threading.Thread(target=read_commands, daemon=True).start()

    context = None
    list_page = None
    session: aftersale_appeal.AftersaleAppealSession | None = None
    try:
        while True:
            command = command_queue.get()
            cmd = command.get("cmd")
            if cmd == "stop":
                break
            if cmd == "invalid":
                emit({
                    "type": "aftersale_error",
                    "stage": "protocol",
                    "message": command.get("message") or "无法解析指令",
                })
                continue

            if cmd == "init":
                try:
                    if context is None:
                        context = create_stealth_context(
                            playwright,
                            user_data_dir=str(PROFILE_DIR),
                            close_warn_center=True,
                        )
                        login_page = (
                            context.pages[0]
                            if context.pages
                            else context.new_page()
                        )
                        chat_page = wait_for_login(
                            context=context,
                            page=login_page,
                            interactive=False,
                        )
                        list_page = next(
                            (
                                candidate
                                for candidate in reversed(context.pages)
                                if candidate is not chat_page
                                and not candidate.is_closed()
                                and "/orders/appeals/aftersale/order"
                                in candidate.url
                            ),
                            None,
                        )
                        if list_page is None:
                            list_page = context.new_page()
                        set_give_up_after_auto_solve_fails(True)
                        session = aftersale_appeal.AftersaleAppealSession(
                            context,
                            list_page,
                            chat_page,
                            max_orders=max_orders if max_orders is not None else 10,
                            should_stop=stop_event.is_set,
                        )
                    if command.get('resume'):
                        queue_size = session.restore(command['resume'])
                    else:
                        queue_size = session.initialize()
                    emit({
                        "type": "aftersale_ready",
                        "queue_size": queue_size,
                    })
                except aftersale_appeal.AftersaleAppealStopped:
                    emit({"type": "aftersale_stopped", "stage": "init"})
                    break
                except Exception as exc:
                    emit({
                        "type": "aftersale_error",
                        "stage": "init",
                        "message": str(exc),
                    })
                    break
                continue

            if cmd == "next":
                if session is None:
                    emit({
                        "type": "aftersale_error",
                        "stage": "protocol",
                        "message": "尚未完成 init，不能处理下一单。",
                    })
                    continue
                try:
                    list_page.bring_to_front()
                    next_order_no = ""
                    if (
                        isinstance(session.tasks, list)
                        and 0 <= session.task_index < len(session.tasks)
                    ):
                        next_order_no = str(
                            session.tasks[session.task_index].get("order_no") or ""
                        )
                    emit({
                        "type": "activity",
                        "stage": "正在提交售后申诉",
                        "order_no": next_order_no,
                    })
                    outcome, remaining = session.process_next()
                except aftersale_appeal.AftersaleAppealStopped:
                    emit({"type": "aftersale_stopped", "stage": "order"})
                    break
                except Exception as exc:
                    emit({
                        "type": "aftersale_error",
                        "stage": "order",
                        "message": str(exc),
                    })
                    break
                if outcome is None:
                    emit({"type": "aftersale_queue_empty"})
                    continue
                emit({
                    "type": "aftersale_order_result",
                    "order_no": outcome["order_no"],
                    "record_key": outcome["record_key"],
                    "result": outcome["result"],
                    "current": session.processed,
                    "remaining": remaining,
                    "stopped": stop_event.is_set(),
                    "page_count": len(context.pages),
                    "_resume": session.checkpoint() if remaining else None,
                })
                if stop_event.is_set():
                    break
                continue

            emit({
                "type": "aftersale_error",
                "stage": "protocol",
                "message": f"未知指令: {cmd!r}",
            })
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def run_aftersale_mode(
    playwright,
    real_stdout,
    max_orders: int | None = None,
) -> None:
    """非 GUI 的一次性售后申诉真实提交模式。"""

    def emit(obj: dict) -> None:
        real_stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        real_stdout.flush()

    context = None
    try:
        context = create_stealth_context(
            playwright,
            user_data_dir=str(PROFILE_DIR),
            close_warn_center=True,
        )
        login_page = context.pages[0] if context.pages else context.new_page()
        chat_page = wait_for_login(context=context, page=login_page)
        list_page = context.new_page()
        outcomes, stopped = aftersale_appeal.run_aftersale_appeals(
            context,
            list_page,
            chat_page,
            max_orders=max_orders,
        )
        submitted = sum(item["result"] == "已提交" for item in outcomes)
        failed = len(outcomes) - submitted
        emit({
            "type": "aftersale_result",
            "success": failed == 0,
            "empty": not outcomes,
            "processed": len(outcomes),
            "succeeded": submitted,
            "failed": failed,
            "stopped": stopped,
            "outcomes": outcomes,
            "message": (
                "平台列表明确显示暂无可处理的售后申诉订单。"
                if not outcomes
                else ""
            ),
        })
    except Exception as exc:
        emit({
            "type": "aftersale_result",
            "success": False,
            "empty": False,
            "processed": 0,
            "succeeded": 0,
            "failed": 1,
            "stopped": False,
            "outcomes": [],
            "message": str(exc),
        })
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


# ============================================================
# 主流程
# ============================================================


def main() -> None:
    args = parse_args()

    if args.max_orders is not None and args.max_orders <= 0:
        print("--max-orders 必须是正整数。", file=sys.stderr)
        sys.exit(1)

    if args.status:
        # 不开浏览器，查完数据库直接退出。
        _print_status_report(SHOP_ID)
        return

    if args.check_login:
        if not SHOP_ID:
            raise SystemExit("--check-login 必须搭配 --shop")
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        output = sys.stdout
        sys.stdout = sys.stderr
        result = {"type": "login_check_result", "success": None, "message": ""}
        context = None
        try:
            if not PROFILE_DIR.is_dir():
                result.update(success=False, message="没有保存的登录状态")
            else:
                from login import get_order_search_radio, LOGIN_PAGE_SELECTOR, is_login_page
                from paths import START_URL
                with sync_playwright() as playwright:
                    try:
                        context = create_stealth_context(playwright, str(PROFILE_DIR), headless=True,
                                                        viewport={"width": 1440, "height": 900}, timeout=30_000)
                        page = context.new_page()
                        page.goto(START_URL, wait_until="domcontentloaded", timeout=30_000)
                        get_order_search_radio(page).or_(page.locator(LOGIN_PAGE_SELECTOR)).first.wait_for(
                            state="visible", timeout=20_000)
                        success = not is_login_page(page)
                        result.update(success=success, message="登录已验证" if success else "登录已失效或未登录")
                    finally:
                        if context is not None:
                            context.close()
        except Exception as exc:
            result["message"] = f"检测异常（网络或浏览器被占用）：{exc}"
        print(json.dumps(result, ensure_ascii=False), file=output, flush=True)
        return

    if args.account_login:
        if not SHOP_ID:
            print("--account-login 必须搭配 --shop <店铺名> 使用，已退出。", file=sys.stderr)
            sys.exit(1)
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        print(f"[店铺] {SHOP_ID}（账号密码自动登录模式）")
        with sync_playwright() as playwright:
            run_account_login_mode(playwright, real_stdout)
        return

    if args.login_only:
        if not SHOP_ID:
            print("--login-only 必须搭配 --shop <店铺名> 使用，已退出。", file=sys.stderr)
            sys.exit(1)

        # 跟 --serve 一样：协议全是给调用方（gui/ 子项目）解析的 JSON，
        # stdout/stdin/stderr 编码显式钉死 UTF-8，且必须在任何 print() 之前把
        # sys.stdout 换成 sys.stderr——本函数调用链上的既有 print()（比如
        # create_stealth_context/wait_for_login_polling 内部的提示文字）
        # 不用改一行，天然跟着走已切换的 sys.stdout。
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        real_stdout = sys.stdout
        sys.stdout = sys.stderr

        print(f"[店铺] {SHOP_ID or '(单店铺模式)'}（--login-only 模式，只登录不跑业务阶段）")

        with sync_playwright() as playwright:
            run_login_only_mode(playwright, real_stdout)
        return

    if args.check_appeal_results:
        if not SHOP_ID:
            print("--check-appeal-results 必须搭配 --shop <店铺名> 使用，已退出。", file=sys.stderr)
            sys.exit(1)
        # 跟 --login-only/--serve 一样：协议全是给调用方（gui/ 子项目）解析
        # 的 JSON，stdout/stdin/stderr 编码显式钉死 UTF-8，且必须在任何
        # print() 之前把 sys.stdout 换成 sys.stderr。
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        real_stdout = sys.stdout
        sys.stdout = sys.stderr

        print(
            f"[店铺] {SHOP_ID}（--check-appeal-results 模式，"
            "使用页面默认日期区间）"
        )

        with sync_playwright() as playwright:
            run_check_appeal_results_mode(playwright, real_stdout, since_days=args.since_days, submitted_since=args.result_submitted_since)
        return

    if args.check_reconsideration_results:
        if not SHOP_ID:
            print(
                "--check-reconsideration-results 必须搭配 --shop <店铺名> 使用，已退出。",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        print(f"[店铺] {SHOP_ID}（--check-reconsideration-results 模式）")
        with sync_playwright() as playwright:
            run_check_reconsideration_results_mode(playwright, real_stdout, submitted_since=args.result_submitted_since)
        return

    if args.check_aftersale_results:
        if not SHOP_ID:
            print(
                "--check-aftersale-results 必须搭配 --shop <店铺名> 使用，已退出。",
                file=sys.stderr,
            )
            sys.exit(1)
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        print(f"[店铺] {SHOP_ID}（--check-aftersale-results 模式）")
        with sync_playwright() as playwright:
            run_check_aftersale_results_mode(
                playwright, real_stdout, query_interval_seconds=args.aftersale_query_interval,
                submitted_since=args.result_submitted_since,
            )
        return

    if args.reconsider:
        if not SHOP_ID:
            print("--reconsider 必须搭配 --shop <店铺名> 使用，已退出。", file=sys.stderr)
            sys.exit(1)
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        print(
            f"[店铺] {SHOP_ID}（--reconsider"
            f"{' --serve' if args.serve else ''} 模式，"
            f"{'真实提交' if args.confirm else '模拟填写后取消'}）"
        )
        set_auto_solve_captcha(not args.manual_captcha)
        with sync_playwright() as playwright:
            if args.serve:
                run_reconsideration_serve_mode(
                    playwright,
                    real_stdout,
                    confirm=args.confirm,
                    max_orders=args.max_orders,
                )
            else:
                run_reconsideration_mode(
                    playwright,
                    real_stdout,
                    confirm=args.confirm,
                    max_orders=args.max_orders,
                )
        return

    if args.aftersale_appeal:
        if not args.confirm:
            print(
                "--aftersale-appeal 仅支持真实提交，必须搭配 --confirm。",
                file=sys.stderr,
            )
            sys.exit(2)
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        real_stdout = sys.stdout
        sys.stdout = sys.stderr
        print(
            f"[店铺] {SHOP_ID or '(单店铺模式)'}"
            f"（售后申诉{'逐单长驻' if args.serve else '一次性'}真实提交）"
        )
        set_auto_solve_captcha(not args.manual_captcha)
        with sync_playwright() as playwright:
            if args.serve:
                run_aftersale_serve_mode(
                    playwright,
                    real_stdout,
                    max_orders=args.max_orders,
                )
            else:
                run_aftersale_mode(
                    playwright,
                    real_stdout,
                    max_orders=args.max_orders,
                )
        return

    if args.serve:
        # 协议消息和错误日志里都有中文，显式把 stdout/stdin/stderr
        # 编码钉死成 UTF-8，不吃系统 locale 默认值——调度器
        # （run_shops_interleaved.py）那边的 subprocess.Popen 管道也显式
        # 指定了 encoding="utf-8"，两边必须一致。不然只要有一处走了 locale
        # 默认值（Windows 常见 cp936），中文内容会在管道里悄悄错位/乱码但
        # 不报错——json.loads 可能"成功"解析出内容错误的字符串，调度器靠
        # 字符串前缀判断结果（比如"失败:"）的逻辑会全部失灵却毫无提示。
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

        # serve 模式下 stdout 是给外部调度器读的协议专用通道，必须在任何
        # print() 之前切换——包括下面这行店铺横幅，否则横幅文本会被调度器
        # 当协议 JSON 解析，直接崩掉。切换之后调用链上所有既有 print() 都会
        # 跟着走 sys.stdout（此时已经 == stderr），不用改代码。
        real_stdout = sys.stdout
        sys.stdout = sys.stderr

        print(f"[店铺] {SHOP_ID or '(单店铺模式)'}（serve 模式，等待调度器指令后才会打开浏览器）")

        set_auto_solve_captcha(not args.manual_captcha)

        # 注意：这里故意不在进入 run_serve_mode 之前就开浏览器/查登录态——
        # 那样会导致子进程一启动就自己连拼多多，不受调度器的串行节奏控制。
        # 具体原因和处理方式见 run_serve_mode() 的文档字符串。
        with sync_playwright() as playwright:
            run_serve_mode(
                playwright, real_stdout,
                confirm=args.confirm, reanalyze=args.reanalyze,
                max_orders_override=args.max_orders,
            )
        return

    # 启动横幅：多店并行时终端里能一眼看清哪个窗口是哪个店。
    print(f"[店铺] {SHOP_ID or '(单店铺模式)'}")

    # 没指定任何阶段标志 → 默认全运行（逐单模式）。这一步必须在下面
    # "--confirm 隐含 --submit" 之前算，否则单独 --confirm 会被误判成
    # "显式选了 submit"，导致 run_all 变 False，只跑 Phase 3（此前的 bug：
    # 单独 python main.py --confirm 不会先采集/分析，直接报错找不到订单）。
    run_all = not (args.collect or args.analyze or args.submit)

    # --confirm 隐含 --submit（无需重复指定）
    if args.confirm:
        args.submit = True
        print()
        print("=" * 60)
        print("⚠⚠⚠  请注意：--confirm 模式会真实提交申诉！ ⚠⚠⚠")
        print("=" * 60)
        print()

    do_collect = args.collect or run_all
    do_analyze = args.analyze or run_all
    do_submit = args.submit or run_all
    # run_all 时（真正裸跑，或只加了 --confirm）走逐单模式：分析完一单立刻
    # 处理提交，不再是"分析全部→提交全部"两轮扫描。显式传了
    # --collect/--analyze/--submit 中任意一个，走原来的分阶段批量路径。
    do_per_order = run_all

    phases = [p for p, flag in [("1", do_collect), ("2", do_analyze), ("3", do_submit)] if flag]
    mode_note = "（逐单模式：分析完一单立刻提交）" if do_per_order else ""
    print(f"将执行 Phase: {', '.join(phases)}{mode_note}")

    # 是否开启自动破解验证码（--manual-captcha 则关闭）
    set_auto_solve_captcha(not args.manual_captcha)

    # ---- 解析订单号（跳过 Phase 1 时从数据库/指定文件加载） ----
    order_numbers: list[str] = []
    order_issues: dict[str, str] = {}

    if not do_collect and (do_analyze or do_submit):
        order_numbers, order_issues = _resolve_order_numbers(args)
        print(f"加载到 {len(order_numbers)} 个订单号")
    elif not do_collect:
        print("没有选择任何阶段，退出。")
        return

    # ---- 单次浏览器会话执行所有启用的阶段 ----
    do_phases = do_collect or do_analyze or do_submit
    if not do_phases:
        return

    with sync_playwright() as playwright:
        context = create_stealth_context(
            playwright, user_data_dir=str(PROFILE_DIR),
            close_warn_center=True,
        )
        page = context.pages[0] if context.pages else context.new_page()
        page = wait_for_login(context=context, page=page)

        # 默认的逐单批量模式为当前店铺固定保留两张页面：聊天记录页只负责
        # 采集/分析，申诉页负责 Phase 1 和逐单提交，避免同一标签来回跳转。
        chat_page = page
        appeal_page = None
        if do_per_order:
            appeal_page = _get_or_create_role_page(
                context,
                appeal_page,
                _APPEAL_PAGE_FRAGMENT,
                excluded_pages=(chat_page,),
            )

        # 登录成功后才打开"验证码自动破解 3 次都失败就直接放弃、跳过当前
        # 任务"——这条代码路径不只是交互式单跑，run_shops.py 也是拿它当
        # 无人值守子进程串行跑各家店铺，同样不该为了等一个不存在的人工
        # 干等 5 分钟。登录本身仍然保留"等人工"，见
        # captcha.set_give_up_after_auto_solve_fails 文档。
        set_give_up_after_auto_solve_fails(True)

        # ---- Phase 1: 采集 ----
        if do_collect:
            order_numbers, order_issues = run_phase1(
                appeal_page if appeal_page is not None else page,
                max_orders_override=args.max_orders,
            )
            if not order_numbers:
                return

        if do_per_order:
            # ---- 逐单模式：分析+提交交织，粒度精确到"一个订单" ----
            results = run_per_order_pipeline(
                context,
                chat_page,
                order_numbers,
                order_issues,
                confirm=args.confirm, reanalyze=args.reanalyze,
                appeal_page=appeal_page,
            )
            
            # 汇总报告
            print()
            print("=" * 60)
            print("汇总报告")
            print("=" * 60)
            print(f"可申诉订单总数:  {len(order_numbers)}")
            success_count = sum(
                1 for _, s in results if s in ("成功", "已提交")
            )
            skip_count = sum(
                # 所有终态跳过的结果字符串都以 "-跳过" 结尾（已申诉/已超时/
                # 已驳回……见 _TERMINAL_STATUSES），以后新增终态不用改这里。
                1 for _, s in results if "-跳过" in s
            )
            fail_count = sum(
                1 for _, s in results if s.startswith("分析失败") or s.startswith("失败")
            )
            print(f"模拟申诉成功:    {success_count}")
            if skip_count:
                print(f"跳过（已申诉/已超时/已驳回）: {skip_count}")
            if fail_count:
                print(f"处理失败:        {fail_count}")
            print()
            for order_no, result in results:
                print(f"  {order_no}: {result}")

        else:
            # ---- Phase 2: 分析（批量，全部订单跑完） ----
            analyzed_orders: list[str] = []

            if do_analyze:
                analyzed_orders = run_phase2(page, order_numbers, order_issues)
                if not do_submit:
                    print("\nPhase 2 结束。")
                    return
            elif do_submit:
                # 跳过 Phase 2，从数据库里发现已分析订单
                analyzed_orders = _discover_analyzed_orders(order_numbers)
                print(f"数据库里检测到 {len(analyzed_orders)} 个已分析订单")
                if not analyzed_orders:
                    print("没有已分析的订单可模拟申诉，退出。")
                    return

            # ---- Phase 3: 模拟申诉（批量，全部订单跑完） ----
            if do_submit:
                results = run_phase3(context, page, analyzed_orders, order_issues, confirm=args.confirm)

                # 汇总报告
                print()
                print("=" * 60)
                print("汇总报告")
                print("=" * 60)
                print(f"可申诉订单总数:  {len(order_numbers)}")
                if do_analyze:
                    print(f"AI 分析成功:     {len(analyzed_orders)}")
                success_count = sum(
                    1 for _, s in results if s in ("成功", "已提交")
                )
                skip_count = sum(
                    # 所有终态跳过的结果字符串都以 "-跳过" 结尾（已申诉/已超时/
                # 已驳回……见 _TERMINAL_STATUSES），以后新增终态不用改这里。
                1 for _, s in results if "-跳过" in s
                )
                print(f"模拟申诉成功:    {success_count}")
                if skip_count:
                    print(f"跳过（已申诉/已超时/已驳回）: {skip_count}")
                print()
                for order_no, result in results:
                    print(f"  {order_no}: {result}")

    print()
    print("全流程结束。")


if __name__ == "__main__":
    main()
