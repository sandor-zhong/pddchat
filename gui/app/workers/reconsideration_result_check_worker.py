"""运行控制页“批量查询复议结果”的逐店铺一次性子进程适配器。"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

from ..services.app_logging import logger
from .base_process_worker import ProcessLineIO
from result_query_settings import normalize_since


class ReconsiderationResultCheckWorker(QObject):
    shop_started = pyqtSignal(str)
    shop_progress = pyqtSignal(str, int, int, int, int)
    shop_finished = pyqtSignal(str, bool, object, str)
    all_finished = pyqtSignal()

    def __init__(
        self,
        pddchat_python: str | Path,
        main_py: str | Path,
        pddchat_root: str | Path,
        shops: list[str],
        parent: QObject | None = None,
        *,
        submitted_since: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = str(pddchat_python)
        self._main_py = str(main_py)
        self._root = str(pddchat_root)
        self._submitted_since = normalize_since(submitted_since)
        self._queue = list(shops)
        self._current_shop: str | None = None
        self._io: ProcessLineIO | None = None
        self._got_result = False
        self._cancelled = False

    def start(self) -> None:
        self._run_next()

    def _run_next(self) -> None:
        if self._cancelled or not self._queue:
            self.all_finished.emit()
            return
        shop = self._queue.pop(0)
        self._current_shop = shop
        self._got_result = False
        command = [
            self._python,
            self._main_py,
            "--shop",
            shop,
            "--check-reconsideration-results",
            "--result-submitted-since", self._submitted_since or "all",
        ]
        self._io = ProcessLineIO(command, cwd=self._root, parent=self)
        self._io.line_received.connect(self._on_line)
        self._io.process_exited.connect(self._on_exit)
        self._io.start()
        self.shop_started.emit(shop)

    def _on_line(self, message: dict) -> None:
        shop = self._current_shop
        if shop is None:
            return
        message_type = message.get("type")
        if message_type == "reconsider_check_progress":
            self.shop_progress.emit(
                shop,
                int(message.get("page", 0)),
                int(message.get("checked", 0)),
                int(message.get("matched", 0)),
                int(message.get("updated", 0)),
            )
        elif message_type == "reconsider_check_result":
            self._got_result = True
            self.shop_finished.emit(
                shop,
                bool(message.get("success")),
                dict(message),
                str(message.get("message") or ""),
            )

    def _on_exit(self, exit_code: int) -> None:
        shop = self._current_shop
        if shop is not None and not self._got_result:
            stderr = self._io.stderr_text().strip() if self._io is not None else ""
            message = f"复议结果查询子进程异常退出（退出码 {exit_code}）。"
            if stderr:
                logger.error("复议结果查询子进程异常退出：\n%s", stderr)
                message += "\n" + stderr[-500:]
            self.shop_finished.emit(shop, False, {}, message)
        self._current_shop = None
        self._run_next()

    def cancel(self) -> None:
        """停止后续店铺；当前店铺完成本页查询和数据库事务后自然退出。"""
        self._cancelled = True
        self._queue.clear()
