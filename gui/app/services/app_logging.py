"""GUI 运行日志落盘：C 页运行日志、各页后台刷新失败、未捕获异常统一
写进 gui/state/logs/gui.log，按天滚动、保留最近 14 天。

背景：C 页的 QPlainTextEdit 只是内存态展示（还限了最多 4000 行），关掉
软件或者滚动超限就彻底丢失；A/B/C 三页的后台刷新失败之前只是 print()，
GUI 打包/双击运行大概率没有可见控制台接着，等于也是丢的。运营出问题
时需要能把日志文件发给开发排查，不能靠复现现场。

用法：main.py 的 main() 一进来就调一次 setup_logging()；其它模块
`from .app_logging import logger` 直接用，不需要各自再配置 handler。
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOG_DIR = Path(__file__).resolve().parent.parent.parent / "state" / "logs"
LOG_FILE = LOG_DIR / "gui.log"

logger = logging.getLogger("pddchat_gui")

_configured = False


def setup_logging() -> None:
    """配置好落盘 handler + 全局异常钩子。重复调用是安全的空操作
    （比如测试脚本里多次 import 触发），不会重复挂 handler。"""
    global _configured
    if _configured:
        return
    _configured = True

    LOG_DIR.mkdir(parents=True, exist_ok=True)

    handler = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        backupCount=14,  # 保留最近 14 天，磁盘占用有上限，不用运营手动清理
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )

    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    # 不向根 logger 传播——避免万一以后哪个第三方库配置了根 logger 的
    # handler，同一条消息被重复写两遍。
    logger.propagate = False

    _install_excepthook()

    logger.info("===== GUI 启动，日志开始记录 =====")


def _install_excepthook() -> None:
    """GUI 意外崩溃时把堆栈写进日志文件——运营看到的只是窗口消失，
    这样至少事后能在日志里找到现场，不需要能复现才能排查。"""
    default_hook = sys.excepthook

    def _hook(exc_type, exc_value, exc_tb):
        logger.critical("未捕获异常，GUI 即将退出", exc_info=(exc_type, exc_value, exc_tb))
        default_hook(exc_type, exc_value, exc_tb)

    sys.excepthook = _hook
