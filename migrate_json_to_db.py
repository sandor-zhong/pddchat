"""一次性迁移脚本：把 output/**/appealable_orders_*.json 和
output/**/chat_*.json 里已经积累的采集/分析历史灌进 state/pddchat.db。

只读旧文件、只写数据库，不删除、不修改任何现有 JSON 文件——迁移完人工核对
数据库里的数据对得上之后，旧文件要不要清掉自己决定，脚本不越俎代庖。

shop 从文件路径推断：output/<shop>/xxx.json -> "<shop>"；
output/xxx.json（没有分片子目录，老的单店铺布局）-> "default"。

用法：
  python migrate_json_to_db.py            实际执行导入
  python migrate_json_to_db.py --dry-run  只打印将要导入什么，不写数据库
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import db

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_ROOT = BASE_DIR / "output"


def _infer_shop(json_path: Path) -> str:
    """output/<shop>/xxx.json -> "<shop>"；output/xxx.json -> "default"。"""
    rel = json_path.relative_to(OUTPUT_ROOT)
    return rel.parts[0] if len(rel.parts) > 1 else "default"


def migrate_appealable_orders(dry_run: bool) -> int:
    """导入所有 appealable_orders_*.json。返回处理过的订单条目总数
    （同一订单出现在多份文件里会被计入多次，但落库是幂等的，不会重复建行）。
    """
    total = 0
    for json_path in sorted(OUTPUT_ROOT.glob("**/appealable_orders_*.json")):
        shop = _infer_shop(json_path)
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[跳过] {json_path}: 读取/解析失败 {exc}")
            continue

        order_numbers = data.get("order_numbers", [])
        order_issues = data.get("order_issues", {})
        print(f"[采集记录] {json_path}  shop={shop}  {len(order_numbers)} 单")

        if not dry_run:
            for order_no in order_numbers:
                db.record_collected(
                    shop, order_no, issue_type=order_issues.get(order_no, ""),
                )
        total += len(order_numbers)
    return total


def migrate_chat_records(dry_run: bool) -> tuple[int, int]:
    """导入所有 chat_*.json。返回 (已分析条目数, 仅有聊天记录未分析条目数)。"""
    analyzed_count = 0
    chat_only_count = 0

    for json_path in sorted(OUTPUT_ROOT.glob("**/chat_*.json")):
        shop = _infer_shop(json_path)
        try:
            data = json.loads(json_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            print(f"[跳过] {json_path}: 读取/解析失败 {exc}")
            continue

        order_no = data.get("order_no")
        if not order_no:
            print(f"[跳过] {json_path}: 缺少 order_no 字段")
            continue

        issue_type = data.get("issue_type", "")
        messages = data.get("messages", [])
        message_count = data.get("message_count", len(messages))
        evidence_paths = data.get("evidence_screenshots", [])
        classification = data.get("appeal_evidence_classification", "")
        appeal_text = data.get("appeal_text", "")

        if classification and appeal_text:
            print(f"[分析记录] {json_path}  shop={shop}  order_no={order_no}")
            if not dry_run:
                db.record_analyzed(
                    shop, order_no,
                    classification=classification,
                    appeal_text=appeal_text,
                    messages=messages,
                    message_count=message_count,
                    evidence_paths=evidence_paths,
                    issue_type=issue_type,
                )
            analyzed_count += 1
        else:
            # 只有聊天记录、还没跑到 AI 分析那一步——至少把订单登记上、
            # 保留 issue_type，不假装它"已分析"（不调 record_analyzed）。
            print(f"[仅聊天记录，未分析] {json_path}  shop={shop}  order_no={order_no}")
            if not dry_run:
                db.record_collected(shop, order_no, issue_type=issue_type)
            chat_only_count += 1

    return analyzed_count, chat_only_count


def main() -> None:
    parser = argparse.ArgumentParser(
        description="一次性把 output/ 下的 JSON 输出文件迁移进 state/pddchat.db",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将要导入什么，不实际写数据库",
    )
    args = parser.parse_args()

    if not OUTPUT_ROOT.is_dir():
        print(f"{OUTPUT_ROOT} 不存在，没有可迁移的数据。")
        return

    print("=" * 60)
    print("迁移可申诉订单采集记录")
    print("=" * 60)
    collected_count = migrate_appealable_orders(args.dry_run)

    print()
    print("=" * 60)
    print("迁移聊天记录 + AI 分析结果")
    print("=" * 60)
    analyzed_count, chat_only_count = migrate_chat_records(args.dry_run)

    print()
    print("=" * 60)
    print("迁移汇总" + ("（--dry-run，未实际写入）" if args.dry_run else ""))
    print("=" * 60)
    print(f"采集记录（订单号+问题类型）处理条目：{collected_count}")
    print(f"已分析订单（分类+申诉文案）：{analyzed_count}")
    print(f"仅有聊天记录、尚未分析：{chat_only_count}")

    if not args.dry_run:
        print(f"\n已写入 {db.DB_PATH}")
        print(
            "确认无误后（用 python main.py --status 核对），"
            "旧的 output/**/appealable_orders_*.json 和 "
            "output/**/chat_*.json 可以自行删除——本脚本不会替你删。"
        )


if __name__ == "__main__":
    main()
