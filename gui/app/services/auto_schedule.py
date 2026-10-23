"""C 页"每日定时自动运行"设置的持久化 + 时间窗判断。

只负责"设置存哪、怎么判断现在是否在窗口内"这两件轻量的事，不碰任何
Qt/调度逻辑——run_page.py 拿到判断结果之后自己决定要不要触发启动/停止。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import time as dt_time
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "auto_schedule.json"

_DEFAULT_START = "08:00"
_DEFAULT_END = "20:00"


@dataclass
class AutoScheduleSettings:
    enabled: bool = False
    start_time: str = _DEFAULT_START  # "HH:MM"
    end_time: str = _DEFAULT_END


def load_settings() -> AutoScheduleSettings:
    if not SETTINGS_PATH.is_file():
        return AutoScheduleSettings()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return AutoScheduleSettings(
            enabled=bool(data.get("enabled", False)),
            start_time=str(data.get("start_time", _DEFAULT_START)),
            end_time=str(data.get("end_time", _DEFAULT_END)),
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return AutoScheduleSettings()


def save_settings(settings: AutoScheduleSettings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_hhmm(text: str) -> dt_time:
    hour, minute = text.split(":")
    return dt_time(hour=int(hour), minute=int(minute))


def is_within_window(now: dt_time, start: dt_time, end: dt_time) -> bool:
    """判断 now 是否落在 [start, end) 这个每日重复的时间窗内。

    分两种情况：
    - start < end（当天窗口，这次的场景，比如 08:00–20:00）：
      start <= now < end。
    - start >= end（跨天窗口，比如 22:00–06:00，预留给以后可能的夜班
      场景，这次用户没这个需求，但判断逻辑顺手写成通用的，不多一个
      "假设"）：now >= start 或 now < end。
    """
    if start < end:
        return start <= now < end
    if start > end:
        return now >= start or now < end
    return False  # start == end：窗口长度为 0，视为永远不在窗口内
