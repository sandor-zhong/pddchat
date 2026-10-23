"""“查询申诉结果”每日定时执行设置的持久化。

这里只保存开关和每天执行时间；是否到点、任务冲突时如何等待，由
``pages/run_page.py`` 统一协调，避免服务层依赖 Qt 或浏览器进程。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

SETTINGS_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "state"
    / "appeal_result_schedule.json"
)

_DEFAULT_RUN_TIME = "21:00"


@dataclass
class AppealResultScheduleSettings:
    enabled: bool = False
    run_time: str = _DEFAULT_RUN_TIME
    last_run_date: str = ""  # "YYYY-MM-DD"；防止 GUI 当天重启后重复执行


def load_settings() -> AppealResultScheduleSettings:
    if not SETTINGS_PATH.is_file():
        return AppealResultScheduleSettings()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return AppealResultScheduleSettings(
            enabled=bool(data.get("enabled", False)),
            run_time=str(data.get("run_time", _DEFAULT_RUN_TIME)),
            last_run_date=str(data.get("last_run_date", "")),
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return AppealResultScheduleSettings()


def save_settings(settings: AppealResultScheduleSettings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
