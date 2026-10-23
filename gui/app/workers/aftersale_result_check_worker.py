"""运行控制页“批量查询售后申诉结果”的逐店铺子进程适配器。"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal

from aftersale_result_settings import (
    DEFAULT_QUERY_INTERVAL_SECONDS,
    validate_query_interval,
)

from ..services.app_logging import logger
from .base_process_worker import ProcessLineIO
from result_query_settings import normalize_since


class AftersaleResultCheckWorker(QObject):
    shop_started = pyqtSignal(str)
    shop_progress = pyqtSignal(str, int, int, int, int)
    shop_finished = pyqtSignal(str, bool, object, str)
    shop_waiting = pyqtSignal(str, int)
    all_finished = pyqtSignal()
    _cancel_requested = pyqtSignal()

    def __init__(
        self,
        pddchat_python: str | Path,
        main_py: str | Path,
        pddchat_root: str | Path,
        shops: list[str],
        parent: QObject | None = None,
        *,
        query_interval_seconds: int = DEFAULT_QUERY_INTERVAL_SECONDS,
        submitted_since: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = str(pddchat_python)
        self._main_py = str(main_py)
        self._root = str(pddchat_root)
        self._queue = list(shops)
        self._current_shop: str | None = None
        self._io: ProcessLineIO | None = None
        self._got_result = False
        self._cancelled = False
        self._finished = False
        self._query_interval_seconds = validate_query_interval(query_interval_seconds)
        self._submitted_since = normalize_since(submitted_since)
        self._next_shop_timer = QTimer(self)
        self._next_shop_timer.setSingleShot(True)
        self._next_shop_timer.timeout.connect(self._run_next)
        self._cancel_requested.connect(self._finish_cancel_if_waiting, Qt.QueuedConnection)

    def start(self) -> None:
        self._run_next()

    def _run_next(self) -> None:
        if self._finished:
            return
        if self._cancelled or not self._queue:
            self._finished = True
            self.all_finished.emit()
            return
        shop = self._queue.pop(0)
        self._current_shop = shop
        self._got_result = False
        self._io = ProcessLineIO(
            [
                self._python, self._main_py, "--shop", shop, "--check-aftersale-results",
                "--aftersale-query-interval", str(self._query_interval_seconds),
                "--result-submitted-since", self._submitted_since or "all",
            ],
            cwd=self._root,
            parent=self,
        )
        self._io.line_received.connect(self._on_line)
        self._io.process_exited.connect(self._on_exit)
        self._io.start()
        self.shop_started.emit(shop)

    def _on_line(self, message: dict) -> None:
        shop = self._current_shop
        if shop is None:
            return
        message_type = message.get("type")
        if message_type == "aftersale_check_progress":
            self.shop_progress.emit(
                shop,
                int(message.get("page", 0)),
                int(message.get("checked", 0)),
                int(message.get("matched", 0)),
                int(message.get("updated", 0)),
            )
        elif message_type == "aftersale_check_result":
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
            message = f"售后结果查询子进程异常退出（退出码 {exit_code}）。"
            if stderr:
                logger.error("售后结果查询子进程异常退出：\n%s", stderr)
                message += "\n" + stderr[-500:]
            self.shop_finished.emit(shop, False, {}, message)
        self._current_shop = None
        if self._queue and not self._cancelled:
            self.shop_waiting.emit(self._queue[0], self._query_interval_seconds)
            self._next_shop_timer.start(self._query_interval_seconds * 1000)
        else:
            self._run_next()

    def cancel(self) -> None:
        self._cancelled = True
        self._queue.clear()
        self._cancel_requested.emit()

    def _finish_cancel_if_waiting(self) -> None:
        self._next_shop_timer.stop()
        if self._current_shop is None:
            self._run_next()
