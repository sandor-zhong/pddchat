"""
全流程批量申诉模拟流水线。

三阶段：
  Phase 1 — 从申诉列表采集所有可申诉订单号
  Phase 2 — 逐单查聊天记录并用 AI 分析生成申诉文案
  Phase 3 — 逐单打开申诉详情页，根据 A/B/C 状态自动填表上传凭证，
             最后点"取消"关闭表单（不真实提交）

用法：
  python main.py                    全部三阶段（填完点取消）
  python main.py --confirm          全部三阶段 + 真实提交
  python main.py --confirm --manual-captcha  真实提交 + 人工滑验证码
  python main.py --collect          仅 Phase 1（采集订单号）
  python main.py --analyze          仅 Phase 2（查聊天+AI分析，从最新采集文件获取订单号）
  python main.py --submit           仅 Phase 3（填表+取消）
  python main.py --submit --confirm 仅 Phase 3 + 真实提交
  python main.py --collect --analyze Phase 1 + Phase 2
  python main.py --analyze --submit  Phase 2 + Phase 3
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
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright

from appeal_submit import (
    click_appeal_form_cancel,
    click_appeal_form_submit,
    detect_detail_page_status,
    fill_appeal_modal,
    load_order_classification,
    open_appeal_search_page,
    search_and_open_appeal_detail,
)
from captcha import set_auto_solve_captcha
from login import wait_for_login
from paths import OUTPUT_DIR, PROFILE_DIR, SHOP_ID
from screenshots import save_error_screenshot
from run_appeal_batch import (
    collect_all_appealable_order_numbers,
    open_appeal_list_page,
    open_appealable_orders_table,
    process_single_order,
    save_appealable_order_numbers,
)
from stealth import create_stealth_context, human_delay


# ============================================================
# 辅助函数
# ============================================================


def _load_latest_order_numbers() -> tuple[list[str], dict[str, str]]:
    """从 output/ 中找最新的 appealable_orders_*.json 并返回订单号列表及问题类型映射。

    返回 (order_numbers, order_issues)。旧格式文件（无 order_issues 字段）返回空字典。
    """
    files = sorted(
        OUTPUT_DIR.glob("appealable_orders_*.json"),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )
    if not files:
        raise FileNotFoundError(
            "output/ 中没有找到 appealable_orders_*.json 文件。\n"
            "请先运行 Phase 1: python main.py --collect"
        )
    latest = files[0]
    print(f"从 {latest.name} 加载订单号")
    data = json.loads(latest.read_text(encoding="utf-8"))
    return data.get("order_numbers", []), data.get("order_issues", {})


def _discover_analyzed_orders(possible: list[str]) -> list[str]:
    """在 possible 中过滤出已存在 chat_*.json 的订单号。"""
    result: list[str] = []
    for order_no in possible:
        json_path = OUTPUT_DIR / f"chat_{order_no}.json"
        if json_path.exists():
            result.append(order_no)
    return result


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
        help='配合 --submit 使用：填完表单后点击"提交"按钮真正提交申诉（默认点"取消"）',
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
    return parser.parse_args()


def _resolve_order_numbers(
    args: argparse.Namespace,
) -> tuple[list[str], dict[str, str]]:
    """按优先级解析订单号：--orders > --order-file > 自动发现最新文件。

    返回 (order_numbers, order_issues)。
    """
    if args.orders:
        return args.orders, {}

    if args.order_file:
        # 绝对路径直接用；否则视为 OUTPUT_DIR 下的相对文件名。
        # 用 Path.is_absolute() 而不是判断 "/" —— 后者在 Windows 上会漏掉反斜杠。
        candidate = Path(args.order_file)
        path = candidate if candidate.is_absolute() else OUTPUT_DIR / args.order_file
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("order_numbers", []), data.get("order_issues", {})

    return _load_latest_order_numbers()


# ============================================================
# 各阶段独立函数
# ============================================================


def run_phase1(page, run_id: str) -> tuple[list[str], dict[str, str]]:
    """Phase 1: 采集可申诉订单号及问题类型。

    返回 (order_numbers, order_issues)。
    """
    print()
    print("=" * 60)
    print("Phase 1: 采集可申诉订单号")
    print("=" * 60)

    open_appeal_list_page(page)
    open_appealable_orders_table(page)
    order_numbers, order_issues = collect_all_appealable_order_numbers(page)
    save_appealable_order_numbers(order_numbers, order_issues, run_id)

    if not order_numbers:
        print("未发现可申诉订单，退出。")
    else:
        print(f"共采集到 {len(order_numbers)} 个可申诉订单号")
        goods_count = sum(1 for t in order_issues.values() if t == "商品问题")
        service_count = sum(1 for t in order_issues.values() if t == "服务问题")
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
            save_error_screenshot(page, order_no)

    print(f"\nPhase 2 完成: {len(analyzed_orders)}/{len(order_numbers)} 个订单分析成功")
    return analyzed_orders


def run_phase3(
    context, page, order_numbers: list[str],
    order_issues: dict[str, str],
    confirm: bool = False,
) -> list[tuple[str, str]]:
    """Phase 3: 逐单模拟申诉填表。返回 (订单号, 结果) 列表。"""
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

        detail_page = None
        form_page = None

        try:
            open_appeal_search_page(page)
            detail_page = search_and_open_appeal_detail(context, page, order_no)

            if detail_page is None:
                results.append((order_no, "未找到申诉记录"))
                print(f"  订单 {order_no} 找不到申诉记录，跳过 ✓")
                continue

            status = detect_detail_page_status(detail_page)
            print(f"  订单 {order_no} 详情页状态: {status}")

            if status == "ALREADY_APPEALED":
                detail_page.close()
                results.append((order_no, "已申诉-跳过"))
                print(f"  订单 {order_no} 已有申诉记录，跳过 ✓")
                continue

            classification, appeal_text, order_issue_type = load_order_classification(order_no)

            # issue_type 优先使用采集阶段从表格读到的值，聊天 JSON 中的为备选
            effective_issue = order_issues.get(order_no) or order_issue_type

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
            results.append((order_no, result_text))
            print(f"  订单 {order_no} 模拟申诉完成 ✓")

        except Exception as exc:    
            results.append((order_no, f"失败: {exc}"))
            print(f"  订单 {order_no} 模拟申诉失败: {exc}")

            for p in context.pages:
                if p is not page and not p.is_closed():
                    try:
                        p.close()
                    except Exception:
                        pass

            try:
                save_error_screenshot(page, order_no)
            except Exception:
                pass

    return results


# ============================================================
# 主流程
# ============================================================


def main() -> None:
    args = parse_args()

    # 启动横幅：多店并行时终端里能一眼看清哪个窗口是哪个店。
    print(f"[店铺] {SHOP_ID or '(单店铺模式)'}")

    # --confirm 隐含 --submit（无需重复指定）
    if args.confirm:
        args.submit = True
        print()
        print("=" * 60)
        print("⚠⚠⚠  请注意：--confirm 模式会真实提交申诉！ ⚠⚠⚠")
        print("=" * 60)
        print()

    # 没指定任何阶段标志 → 默认全运行
    run_all = not (args.collect or args.analyze or args.submit)
    do_collect = args.collect or run_all
    do_analyze = args.analyze or run_all
    do_submit = args.submit or run_all

    phases = [p for p, flag in [("1", do_collect), ("2", do_analyze), ("3", do_submit)] if flag]
    print(f"将执行 Phase: {', '.join(phases)}")

    # 是否开启自动破解验证码（--manual-captcha 则关闭）
    set_auto_solve_captcha(not args.manual_captcha)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # ---- 解析订单号（跳过 Phase 1 时从已有文件加载） ----
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
        )
        page = context.pages[0] if context.pages else context.new_page()
        page = wait_for_login(context=context, page=page)

        # ---- Phase 1: 采集 ----
        if do_collect:
            order_numbers, order_issues = run_phase1(page, run_id)
            if not order_numbers:
                return

        # ---- Phase 2: 分析 ----
        analyzed_orders: list[str] = []

        if do_analyze:
            analyzed_orders = run_phase2(page, order_numbers, order_issues)
            if not do_submit:
                print("\nPhase 2 结束。")
                return
        elif do_submit:
            # 跳过 Phase 2，从已有分类文件中发现可用订单
            analyzed_orders = _discover_analyzed_orders(order_numbers)
            print(f"从 output/ 中检测到 {len(analyzed_orders)} 个已分析订单")
            if not analyzed_orders:
                print("没有已分析的订单可模拟申诉，退出。")
                return

        # ---- Phase 3: 模拟申诉 ----
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
                1 for _, s in results if "已申诉" in s
            )
            print(f"模拟申诉成功:    {success_count}")
            if skip_count:
                print(f"已申诉跳过:      {skip_count}")
            print()
            for order_no, result in results:
                print(f"  {order_no}: {result}")

    print()
    print("全流程结束。")


if __name__ == "__main__":
    main()
