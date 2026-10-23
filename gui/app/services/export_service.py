"""F 页报表导出：订单明细全量字段 → Excel（openpyxl）/ CSV（标准库）。

导出范围是当前筛选后的订单明细，字段跟 db.py orders 表的列一一对应
（用户明确要求"订单明细全量字段"，不是精简过的汇总版）。messages/
evidence_paths 这两个字段在库里是 Python list（db._row_to_dict 已经
把 JSON 文本解码过一次），导出时统一转回 JSON 字符串——Excel/CSV 单元格
只能放文本，原样塞一个 Python list 的 repr 不如 JSON 字符串好读、也
方便以后有需要时再解析回去。
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

from openpyxl import Workbook

from . import time_format

# 这些字段导出时只保留本地年月日。后四项在 db.py 里是 offset-aware 的
# UTC ISO 字符串，需要先转成本地日期；order_created_at 来自平台页面，通常
# 已经是本地时间文本，但也走同一套宽容解析以兼容不同页面格式。
_DATE_FIELDS = frozenset(
    {
        "order_created_at",
        "collected_at",
        "analyzed_at",
        "submitted_at",
        "updated_at",
        "punished_at",
        "platform_reconsidered_at",
        "platform_appealed_at",
        "result_checked_at",
        "discovered_at",
        "last_seen_at",
    }
)
_DATE_PREFIX_PATTERN = re.compile(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日)?")

# 跟 db.py 的 orders 表列顺序一致，加一列中文表头方便运营直接看懂。
_COLUMNS: list[tuple[str, str]] = [
    ("shop", "店铺"),
    ("order_no", "订单编号"),
    ("record_key", "申诉记录标识"),
    ("ticket_sn", "ticketSn"),
    ("issue_type", "问题类型"),
    ("goods_title", "商品标题"),
    ("goods_id", "商品ID"),
    ("goods_image_url", "商品图片地址"),
    ("compensation_amount", "补偿金额（元）"),
    ("order_created_at", "创建时间"),
    ("status", "状态"),
    ("classification", "申诉凭证分类"),
    ("appeal_text", "申诉说明"),
    ("message_count", "聊天记录条数"),
    ("evidence_paths", "凭证截图路径"),
    ("error_message", "错误信息"),
    ("error_screenshot_path", "错误截图路径"),
    ("form_screenshot_path", "填表截图路径"),
    ("collected_at", "采集时间"),
    ("analyzed_at", "分析时间"),
    ("submitted_at", "提交时间"),
    ("result_checked_at", "最后查询时间"),
    ("updated_at", "最后更新时间"),
]

_RECONSIDERATION_COLUMNS: list[tuple[str, str]] = [
    ("shop", "店铺"),
    ("order_no", "订单编号"),
    ("ticket_sn", "ticketSn"),
    ("goods_title", "商品标题"),
    ("issue_type", "问题类型"),
    ("original_appeal_status", "原申诉状态"),
    ("penalty_amount", "复议金额（元）"),
    ("status", "复议状态"),
    ("refund_amount", "返还金额（元）"),
    ("reconsider_reason", "复议原因"),
    ("reconsider_text", "复议说明"),
    ("evidence_paths", "复议凭证路径"),
    ("platform_reconsidered_at", "平台复议时间"),
    ("submitted_at", "本地提交时间"),
    ("result_checked_at", "最后查询时间"),
    ("result_text", "平台结果原文"),
    ("error_message", "结果/错误说明"),
    ("record_key", "复议记录标识"),
    ("form_screenshot_path", "填表截图路径"),
    ("error_screenshot_path", "错误截图路径"),
    ("updated_at", "最后更新时间"),
]

_AFTERSALE_COLUMNS: list[tuple[str, str]] = [
    ("shop", "店铺"),
    ("order_no", "订单编号"),
    ("record_key", "售后记录标识"),
    ("aftersale_id", "售后编号"),
    ("goods_title", "商品标题"),
    ("appeal_item", "申诉项"),
    ("refund_amount", "退款金额（元）"),
    ("appeal_amount", "申诉金额（元，以结果页为准）"),
    ("status", "审核状态"),
    ("result_text", "平台状态原文"),
    ("appeal_reason", "申诉原因"),
    ("appeal_text", "申诉说明"),
    ("platform_appealed_at", "平台申诉时间"),
    ("submitted_at", "本地提交时间"),
    ("result_checked_at", "最后回查时间"),
    ("error_message", "结果/错误说明"),
    ("updated_at", "最后更新时间"),
]

def _date_only(value: object) -> str:
    """把常见 ISO/平台时间格式统一导出为 YYYY-MM-DD。"""
    if value is None or value == "":
        return ""
    raw_text = str(value).strip()
    formatted = time_format.format_local(raw_text, fmt="%Y-%m-%d")
    if formatted != raw_text or re.fullmatch(r"\d{4}-\d{2}-\d{2}", formatted):
        return formatted

    # format_local 对非 ISO 文本会原样返回；再兼容 2026/8/24、
    # 2026年8月24日 之类平台展示格式，并统一补零。
    match = _DATE_PREFIX_PATTERN.match(raw_text)
    if match is None:
        return raw_text
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}"


def _cell_value(row: dict, key: str):
    value = row.get(key)
    if key in ("evidence_paths",) and isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False)
    if key in _DATE_FIELDS:
        return _date_only(value)
    return "" if value is None else value


def export_orders_to_excel(rows: list[dict], out_path: str | Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "订单明细"
    ws.append([label for _, label in _COLUMNS])
    for row in rows:
        ws.append([_cell_value(row, key) for key, _ in _COLUMNS])

    # 表头加粗 + 冻结首行，导出的文件打开就能直接看，不用运营自己再调格式。
    from openpyxl.styles import Font

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"

    wb.save(str(out_path))


def export_orders_to_csv(rows: list[dict], out_path: str | Path) -> None:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        # utf-8-sig：带 BOM，Excel 直接双击打开 CSV 时才能正确识别 UTF-8
        # 中文，不然默认按系统 ANSI（cp936）解析会乱码。
        writer = csv.writer(f)
        writer.writerow([label for _, label in _COLUMNS])
        for row in rows:
            writer.writerow([_cell_value(row, key) for key, _ in _COLUMNS])


def export_reconsiderations_to_excel(rows: list[dict], out_path: str | Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "复议明细"
    ws.append([label for _, label in _RECONSIDERATION_COLUMNS])
    for row in rows:
        ws.append([_cell_value(row, key) for key, _ in _RECONSIDERATION_COLUMNS])

    from openpyxl.styles import Font

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    wb.save(str(out_path))


def export_reconsiderations_to_csv(rows: list[dict], out_path: str | Path) -> None:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([label for _, label in _RECONSIDERATION_COLUMNS])
        for row in rows:
            writer.writerow(
                [_cell_value(row, key) for key, _ in _RECONSIDERATION_COLUMNS]
            )


def export_aftersale_appeals_to_excel(rows: list[dict], out_path: str | Path) -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "售后申诉明细"
    ws.append([label for _, label in _AFTERSALE_COLUMNS])
    for row in rows:
        ws.append([_cell_value(row, key) for key, _ in _AFTERSALE_COLUMNS])
    from openpyxl.styles import Font

    for cell in ws[1]:
        cell.font = Font(bold=True)
    ws.freeze_panes = "A2"
    wb.save(str(out_path))


def export_aftersale_appeals_to_csv(rows: list[dict], out_path: str | Path) -> None:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow([label for _, label in _AFTERSALE_COLUMNS])
        for row in rows:
            writer.writerow([_cell_value(row, key) for key, _ in _AFTERSALE_COLUMNS])

