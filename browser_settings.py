"""GUI 和浏览器进程共享的持久化运行模式。"""

import json
from pathlib import Path

SETTINGS_PATH = Path(__file__).resolve().parent / "state" / "browser_settings.json"


def load_settings() -> dict:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, AttributeError):
        return {}


def browser_engine() -> str:
    value = load_settings().get("browser_engine", "chromium")
    return value if value in ("chromium", "firefox") else "chromium"


def save_browser_engine(engine: str) -> None:
    if engine not in ("chromium", "firefox"):
        raise ValueError("请选择 Chrome 或 Firefox。")
    _save(browser_engine=engine)


def profile_directory(directory: str | Path, engine: str | None = None) -> Path:
    """Firefox 使用独立子目录；Chrome 保留原来的登录目录。"""
    selected = browser_engine() if engine is None else engine
    if selected not in ("chromium", "firefox"):
        raise ValueError("请选择 Chrome 或 Firefox。")
    base = Path(directory)
    return base / ".firefox" if selected == "firefox" else base


def native_properties_enabled() -> bool:
    return load_settings().get("native_properties", False) is True


def chrome_executable() -> str:
    value = load_settings().get("chrome_executable", "")
    return value if isinstance(value, str) else ""


def save_native_properties(enabled: bool) -> None:
    _save(native_properties=bool(enabled))


def save_chrome_executable(path: str) -> None:
    if path and (not Path(path).is_file() or Path(path).name.lower() != "chrome.exe"):
        raise ValueError("请选择实际存在的 Google Chrome chrome.exe 文件。")
    _save(chrome_executable=path)


def _save(**fields) -> None:
    settings = load_settings()
    settings.update(fields)
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    temporary.replace(SETTINGS_PATH)
