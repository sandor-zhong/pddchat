"""Sequential Excel account login worker with a responsive stop command."""

from __future__ import annotations

from pathlib import Path
from threading import Timer

from PyQt5.QtCore import QObject, pyqtSignal

from ..services import shop_repo
from ..services.account_import import AccountRecord
from .base_process_worker import ProcessLineIO


class BatchAccountLoginWorker(QObject):
    progress = pyqtSignal(int, int, str)
    message = pyqtSignal(str)
    shop_result = pyqtSignal(str, bool, str)
    finished = pyqtSignal(int, int, int)

    def __init__(
        self,
        pddchat_python: str | Path,
        main_py: str | Path,
        pddchat_root: str | Path,
        records: list[AccountRecord],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = str(pddchat_python)
        self._main_py = str(main_py)
        self._root = str(pddchat_root)
        self._records = records
        self._total = len(records)
        self._index = 0
        self._succeeded = 0
        self._failed = 0
        self._stopped = False
        self._done = False
        self._io: ProcessLineIO | None = None
        self._current: AccountRecord | None = None
        self._current_has_result = False

    def start(self) -> None:
        shop_repo.register_imported_shops([record.shop for record in self._records])
        self._start_next()

    def _start_next(self) -> None:
        if self._done:
            return
        if self._stopped or self._index >= self._total:
            self._finish()
            return
        record = self._records[self._index]
        self._current = record
        self._current_has_result = False
        self.progress.emit(self._index + 1, self._total, record.shop)
        self.message.emit(f"[{record.shop}] 正在启动独立浏览器")
        cmd = [self._python, self._main_py, "--shop", record.shop, "--account-login"]
        self._io = ProcessLineIO(cmd, cwd=self._root, parent=self)
        self._io.line_received.connect(self._on_line)
        self._io.process_exited.connect(self._on_exit)
        try:
            self._io.start()
            sent = self._io.send(
                {"cmd": "login", "username": record.username, "password": record.password}
            )
            record.clear_password()
            if not sent:
                raise RuntimeError("无法把登录数据交给登录进程。")
        except Exception:  # noqa: BLE001 - child startup errors become a safe UI message
            record.clear_password()
            failed_io = self._io
            if failed_io is not None:
                try:
                    failed_io.line_received.disconnect(self._on_line)
                    failed_io.process_exited.disconnect(self._on_exit)
                except (TypeError, RuntimeError):
                    pass
                failed_io.stop(timeout=1.0)
                failed_io.deleteLater()
            self._record_result(False, "登录进程启动失败，请检查运行环境后重试。")
            self._index += 1
            self._io = None
            self._current = None
            self._start_next()

    def _on_line(self, payload: dict) -> None:
        if self._current is None:
            return
        kind = payload.get("type")
        if kind == "login_progress":
            if self._io is not None:
                self._io.set_browser_topmost(True)
                self._io.focus_browser()
            self.message.emit(f"[{self._current.shop}] {payload.get('message') or '正在登录'}")
        elif kind == "login_result" and not self._current_has_result:
            self._current_has_result = True
            self._record_result(bool(payload.get("success")), str(payload.get("message") or "登录未完成。"))

    def _record_result(self, success: bool, message: str) -> None:
        if self._current is None:
            return
        if success:
            self._succeeded += 1
            shop_repo.mark_shop_login_succeeded(self._current.shop)
        else:
            self._failed += 1
            shop_repo.mark_imported_shop_login(self._current.shop, False, message)
        self.shop_result.emit(self._current.shop, success, message)

    def _on_exit(self, exit_code: int) -> None:
        if self._current is None or self._done:
            return
        if not self._current_has_result:
            message = (
                "批量登录已停止。"
                if self._stopped
                else f"登录进程异常结束（退出码 {exit_code}），未返回登录结果。"
            )
            self._record_result(False, message)
        if self._io is not None:
            self._io.deleteLater()
        self._io = None
        self._current = None
        self._index += 1
        self._start_next()

    def stop(self) -> None:
        if self._done:
            return
        self._stopped = True
        for record in self._records[self._index + (1 if self._current else 0):]:
            record.clear_password()
            shop_repo.mark_imported_shop_login(record.shop, False, "尚未登录：批量队列已停止。")
        if self._io is not None:
            self.message.emit("正在停止当前登录并关闭浏览器……")
            self._io.send({"cmd": "stop"})
            current_io = self._io
            watchdog = Timer(8.0, lambda: current_io.stop(timeout=1.0))
            watchdog.daemon = True
            watchdog.start()
        else:
            self._finish()

    def _finish(self) -> None:
        if self._done:
            return
        self._done = True
        for record in self._records:
            record.clear_password()
        cancelled = self._total - self._succeeded - self._failed
        self.finished.emit(self._succeeded, self._failed, max(0, cancelled))
