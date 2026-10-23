"""C 页"运行较长时间后自动休息（拟人）"设置的持久化。

只负责"设置存哪"，不碰调度逻辑——具体休息时长怎么随机、什么时候触发，
是 scheduler_core.Scheduler 自己的事，这里只是把配置好的区间传参数进去。
跟"每日自动运行"的时间窗是两个独立概念，分开一个文件：那个决定"今天几点
到几点允许跑"，这个决定"跑的过程中，累计运行多久要主动歇一会儿"。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent.parent.parent / "state" / "rest_settings.json"

_DEFAULT_MIN_HOURS = 3
_DEFAULT_MAX_HOURS = 4
_DEFAULT_MIN_MINUTES = 20
_DEFAULT_MAX_MINUTES = 30


@dataclass
class RestSettings:
    enabled: bool = True
    min_hours: int = _DEFAULT_MIN_HOURS
    max_hours: int = _DEFAULT_MAX_HOURS
    min_minutes: int = _DEFAULT_MIN_MINUTES
    max_minutes: int = _DEFAULT_MAX_MINUTES


def load_settings() -> RestSettings:
    if not SETTINGS_PATH.is_file():
        return RestSettings()
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return RestSettings(
            enabled=bool(data.get("enabled", True)),
            min_hours=int(data.get("min_hours", _DEFAULT_MIN_HOURS)),
            max_hours=int(data.get("max_hours", _DEFAULT_MAX_HOURS)),
            min_minutes=int(data.get("min_minutes", _DEFAULT_MIN_MINUTES)),
            max_minutes=int(data.get("max_minutes", _DEFAULT_MAX_MINUTES)),
        )
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return RestSettings()


def save_settings(settings: RestSettings) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(
        json.dumps(asdict(settings), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
