"""三类结果回查共用的提交时间范围（北京时间，包含起点）。"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from itertools import count
from pathlib import Path

SHANGHAI_TZ = timezone(timedelta(hours=8))
SETTINGS_PATH = Path(__file__).resolve().parent / "state" / "result_query_settings.json"


def empty_query_reason(pending_count: int, submitted_since: str | None, kind: str) -> str:
    if pending_count:
        since_text = parse_time(submitted_since).strftime("%Y-%m-%d %H:%M:%S")
        return (
            f"本地有 {pending_count} 条可回查的{kind}记录，但提交时间均早于 {since_text}（北京时间）。"
            "请调整结果回查的提交时间起点，或取消时间限制后重试。"
        )
    return (
        f"本地没有符合回查状态的{kind}记录。"
        "此入口仅回查本地已记录的申诉，不会自动导入平台历史订单。"
    )


def parse_time(value: object) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI_TZ)
    return parsed.astimezone(SHANGHAI_TZ)


def normalize_since(value: object) -> str | None:
    if value is None or value == "all":
        return None
    parsed = parse_time(value)
    if parsed is None:
        raise ValueError("提交时间须为 YYYY-MM-DD HH:mm:ss，或 all 表示不限制")
    return parsed.isoformat(timespec="seconds")


def load_settings() -> dict:
    default = (datetime.now(SHANGHAI_TZ) - timedelta(days=7)).replace(
        hour=0, minute=0, second=0, microsecond=0,
    ).isoformat()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        since = normalize_since(data["submitted_since"])
        if since is not None:
            return {"enabled": data.get("enabled") is True, "submitted_since": since}
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return {"enabled": False, "submitted_since": default}


def active_since() -> str | None:
    settings = load_settings()
    return settings["submitted_since"] if settings["enabled"] else None


def save_settings(enabled: bool, submitted_since: str) -> None:
    since = normalize_since(submitted_since)
    if since is None:
        raise ValueError("请选择有效的提交时间")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps({"enabled": bool(enabled), "submitted_since": since}), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)


def filter_records(records: list[dict], since: str | None, *time_fields: str) -> list[dict]:
    normalized = normalize_since(since)
    if normalized is None:
        return records
    cutoff = parse_time(normalized)
    result = []
    unknown = 0
    for record in records:
        submitted = None
        for field in time_fields:
            submitted = parse_time(record.get(field))
            if submitted is not None:
                break
        # 缺少历史提交时间时不能擅自当作旧订单丢弃。
        if submitted is None:
            unknown += 1
        if submitted is None or submitted >= cutoff:
            result.append(record)
    print(f"[结果回查] 提交时间从 {cutoff:%Y-%m-%d %H:%M:%S}（北京时间，含）起，目标 {len(result)} 条，跳过旧记录 {len(records) - len(result)} 条。")
    if unknown:
        print(f"[结果回查] {unknown} 条历史记录缺少有效提交时间，保留回查以避免漏查。")
    return result


def platform_before_since(value: str, since: str | None) -> bool:
    if since is None:
        return False
    parsed = parse_time(value)
    cutoff = parse_time(since)
    if parsed is None:
        return False
    # 平台只显示到分钟时，不能把该分钟内晚于起点的提交错误排除。
    if re.search(r"\d{2}:\d{2}$", value.strip()):
        parsed += timedelta(minutes=1, microseconds=-1)
    return parsed < cutoff


def same_submission(record: dict, platform_value: str, time_field: str, tolerance_seconds: int) -> bool:
    platform_time = parse_time(platform_value)
    local_time = parse_time(record.get(time_field))
    if platform_time is None or local_time is None:
        return True
    return abs((platform_time - local_time).total_seconds()) <= tolerance_seconds


def page_numbers(max_pages: int, since: str | None):
    # 有时间范围时以目标记录是否查齐/平台末页为结束条件，避免旧页数上限截断。
    return count(1) if since is not None else range(1, max_pages + 1)
