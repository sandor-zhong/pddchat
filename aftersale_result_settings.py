"""售后结果查询间隔，供命令行和 GUI 共用（不依赖浏览器或 Qt）。"""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_QUERY_INTERVAL_SECONDS = 3
MIN_QUERY_INTERVAL_SECONDS = 1
MAX_QUERY_INTERVAL_SECONDS = 60
SETTINGS_PATH = Path(__file__).resolve().parent / "state" / "aftersale_result_settings.json"


def validate_query_interval(value: object) -> int:
    try:
        seconds = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("售后查询间隔必须为 1～60 秒的整数") from exc
    if (
        isinstance(value, bool)
        or (isinstance(value, float) and value != seconds)
        or not MIN_QUERY_INTERVAL_SECONDS <= seconds <= MAX_QUERY_INTERVAL_SECONDS
    ):
        raise ValueError("售后查询间隔必须为 1～60 秒的整数")
    return seconds


def load_query_interval() -> int:
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return validate_query_interval(data.get("query_interval_seconds", DEFAULT_QUERY_INTERVAL_SECONDS))
    except (OSError, ValueError, TypeError):
        pass
    return DEFAULT_QUERY_INTERVAL_SECONDS


def save_query_interval(value: int) -> None:
    seconds = validate_query_interval(value)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps({"query_interval_seconds": seconds}, indent=2), encoding="utf-8"
    )
