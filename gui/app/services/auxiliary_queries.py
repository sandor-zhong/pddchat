"""辅助查询的数据快照：北京时间、真实提交、汇总与明细共用同一批记录。"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from contextlib import closing
from datetime import UTC, date, datetime, time, timedelta, timezone
from pathlib import Path

import db

BEIJING = timezone(timedelta(hours=8))
BUSINESS_TYPES = ("负向申诉", "复议", "售后申诉")
NEGATIVE_STATES = {
    "submitted",
    "already_appealed",
    "review_unknown",
    "approved",
    "rejected",
    "已提交",
    "已申诉",
    "结果待确认",
    "申诉通过",
    "申诉未通过已处罚",
}
RECON_STATES = {
    "已提交复议",
    "复议提交确认中",
    "待处理",
    "审核中",
    "复议结果待确认",
    "申诉成功",
    "申诉失败",
}
AFTERSALE_STATES = {
    "已提交",
    "待处理",
    "审核中",
    "结果待确认",
    "申诉成功",
    "部分通过",
    "申诉失败",
    "已撤回",
}
POLICY = (
    "日期按北京时间；提交数量、通过率和明细均按已有订单/申诉记录的提交时间匹配所选日期范围，包含结束当天。优先使用程序提交时间，缺失时用平台提交时间补全，不按采集时间或更新时间统计。只统计真实提交。"
    "负向申诉按订单去重，复议和售后申诉按独立申诉记录统计。审核通过率只计算已出结果记录，"
    "待审核、结果待确认及撤回不计入分母；结果为最近一次回查状态。"
)


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def parse_time(value: object, *, platform: bool = False) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    for old, new in (("年", "-"), ("月", "-"), ("日", " "), ("/", "-"), (".", "-")):
        if old == "." and "T" in text:
            continue
        if old == "." and text.count(".") != 2:
            continue
        text = text.replace(old, new)
    try:
        parsed = datetime.fromisoformat(text.strip())
    except (ValueError, TypeError):
        parsed = None
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text.strip(), pattern).replace(
                    tzinfo=BEIJING if platform else UTC
                )
                break
            except ValueError:
                continue
        if parsed is None:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING if platform else UTC)
    return parsed.astimezone(BEIJING)


def format_time(value: object, *, platform: bool = False) -> str:
    parsed = (
        value if isinstance(value, datetime) else parse_time(value, platform=platform)
    )
    return parsed.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S") if parsed else ""


def _outcome(kind: str, status: str) -> str:
    if status in {"approved", "申诉通过", "申诉成功"}:
        return "通过"
    if status == "部分通过":
        return "部分通过"
    if status in {"rejected", "申诉未通过已处罚", "申诉失败"}:
        return "未通过"
    if status == "已撤回":
        return "已撤回"
    if status in {"review_unknown", "结果待确认", "复议结果待确认", "复议提交确认中"}:
        return "结果待确认"
    return "待审核"


def _load_rows(connection: sqlite3.Connection, shop: str | None) -> list[dict]:
    clause, params = (" WHERE shop=?", (shop,)) if shop is not None else ("", ())
    rows = []
    negative_clause = " WHERE o.shop=?" if shop is not None else ""
    negatives = connection.execute(
        """
        SELECT o.shop, o.order_no, '' AS record_key, o.goods_title, o.status,
               o.issue_type AS appeal_item, o.appeal_text AS description,
               o.compensation_amount AS amount, o.error_message AS result_text,
               COALESCE(NULLIF(o.submitted_at, ''), (
                   SELECT MAX(a.submitted_at) FROM appeal_records a
                   WHERE a.shop=o.shop AND a.order_no=o.order_no
                     AND a.status NOT IN ('simulated', '模拟成功')
               )) AS submitted_at,
               (SELECT MAX(h.checked_at) FROM appeal_review_history h
                WHERE h.shop=o.shop AND h.order_no=o.order_no) AS result_checked_at
        FROM orders o
    """
        + negative_clause,
        params,
    ).fetchall()
    for raw in negatives:
        row = dict(raw)
        if row["status"] in NEGATIVE_STATES:
            row.update(
                business_type="负向申诉", platform_at="", reason="", aftersale_id=""
            )
            rows.append(row)
    for raw in connection.execute(
        """
        SELECT shop, order_no, record_key, goods_title, status, issue_type AS appeal_item,
               reconsider_text AS description, penalty_amount AS amount,
               result_text, submitted_at, platform_reconsidered_at AS platform_at,
               result_checked_at, reconsider_reason AS reason, '' AS aftersale_id
        FROM reconsideration_records
    """
        + clause,
        params,
    ):
        row = dict(raw)
        if row["status"] in RECON_STATES:
            row["business_type"] = "复议"
            rows.append(row)
    for raw in connection.execute(
        """
        SELECT shop, order_no, record_key, goods_title, status, appeal_item,
               appeal_text AS description, appeal_amount AS amount,
               result_text, submitted_at, platform_appealed_at AS platform_at,
               result_checked_at, appeal_reason AS reason, aftersale_id
        FROM aftersale_appeal_records
    """
        + clause,
        params,
    ):
        row = dict(raw)
        if row["status"] in AFTERSALE_STATES:
            row["business_type"] = "售后申诉"
            rows.append(row)
    return rows


def _known_shops(connection: sqlite3.Connection, root: Path) -> list[str]:
    shops = {
        str(row[0])
        for row in connection.execute("""
        SELECT shop FROM orders UNION SELECT shop FROM appeal_records
        UNION SELECT shop FROM reconsideration_records UNION SELECT shop FROM aftersale_appeal_records
        UNION SELECT shop FROM shop_settings
    """)
        if row[0]
    }
    profiles = root / "state" / "profiles"
    if profiles.is_dir():
        shops.update(
            p.name
            for p in profiles.iterdir()
            if p.is_dir() and not p.name.startswith(".")
        )
    try:
        imported = json.loads(
            (root / "gui/state/imported_shops.json").read_text(encoding="utf-8")
        )
        values = imported.get("shops", {})
        if isinstance(values, dict):
            shops.update(str(key) for key in values if key)
    except (OSError, ValueError, AttributeError):
        pass
    return sorted(shops)


def query_report(
    start: date,
    end: date,
    *,
    shop: str | None = None,
    business_type: str | None = None,
    partial_pass: bool = True,
    now: datetime | None = None,
    database: str | Path | None = None,
) -> dict:
    if start > end:
        raise ValueError("开始日期不能晚于结束日期。")
    if business_type is not None and business_type not in BUSINESS_TYPES:
        raise ValueError("请选择有效的申诉类型。")
    current = now or beijing_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=BEIJING)
    current = current.astimezone(BEIJING)
    path = Path(database if database is not None else db.DB_PATH).resolve()
    with closing(
        sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=15)
    ) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        shops = _known_shops(connection, path.parent.parent)
        raw_rows = _load_rows(connection, shop)
    selected_shops = [shop] if shop is not None else shops
    kinds = [business_type] if business_type else list(BUSINESS_TYPES)
    lower = datetime.combine(start, time.min, BEIJING)
    upper = datetime.combine(end + timedelta(days=1), time.min, BEIJING)
    daily_counts = Counter()
    details = []
    missing_time = 0
    for row in raw_rows:
        kind = row["business_type"]
        if kind not in kinds:
            continue
        local_time = parse_time(row.get("submitted_at"))
        platform_time = parse_time(row.get("platform_at"), platform=True)
        effective = local_time or platform_time
        if effective is None:
            missing_time += 1
            continue
        if not (lower <= effective < upper and effective <= current):
            continue
        daily_counts[(row["shop"], kind)] += 1
        outcome = _outcome(kind, row["status"])
        row.update(
            submission_time=format_time(effective),
            local_submission_time=format_time(local_time),
            platform_submission_time=format_time(platform_time),
            time_source="程序提交时间" if local_time else "平台提交时间",
            last_checked=format_time(row.get("result_checked_at")),
            outcome=outcome,
            counts_as_pass=outcome == "通过"
            or (outcome == "部分通过" and partial_pass),
            counts_as_reviewed=outcome in {"通过", "部分通过", "未通过"},
        )
        details.append(row)
    details.sort(
        key=lambda row: (
            row["submission_time"],
            row["shop"],
            row["business_type"],
            row["order_no"],
            row["record_key"] or "",
        ),
        reverse=True,
    )
    daily = []
    for name in selected_shops:
        counts = {kind: daily_counts[(name, kind)] for kind in kinds}
        daily.append(
            {
                "shop": name,
                **{kind: counts.get(kind, 0) for kind in BUSINESS_TYPES},
                "total": sum(counts.values()),
            }
        )
    groups = {(name, kind): [] for name in selected_shops for kind in kinds}
    for row in details:
        groups.setdefault((row["shop"], row["business_type"]), []).append(row)
    summary = []
    for (name, kind), group in sorted(groups.items()):
        counts = Counter(row["outcome"] for row in group)
        reviewed = sum(row["counts_as_reviewed"] for row in group)
        passed = sum(row["counts_as_pass"] for row in group)
        summary.append(
            {
                "shop": name,
                "business_type": kind,
                "submitted": len(group),
                "approved": counts["通过"],
                "partial": counts["部分通过"],
                "rejected": counts["未通过"],
                "waiting": counts["待审核"],
                "unknown": counts["结果待确认"],
                "withdrawn": counts["已撤回"],
                "reviewed": reviewed,
                "passed": passed,
                "pass_rate": passed / reviewed if reviewed else None,
                "submission_pass_rate": passed / len(group) if group else None,
            }
        )
    return {
        "criteria": {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "shop": shop,
            "business_type": business_type,
            "partial_pass": bool(partial_pass),
        },
        "generated_at": format_time(current),
        "today": current.date().isoformat(),
        "shops": shops,
        "daily": daily,
        "summary": summary,
        "details": details,
        "missing_time": missing_time,
        "policy": POLICY,
    }
