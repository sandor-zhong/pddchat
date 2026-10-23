"""Progress dialog for sequential account-password login imported from Excel."""

from __future__ import annotations

from PyQt5.QtCore import QThread, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..services.account_import import AccountRecord
from ..services.project_paths import PROJECT_PATHS
from ..workers.batch_account_login_worker import BatchAccountLoginWorker


class BatchAccountLoginDialog(QDialog):
    batch_finished = pyqtSignal(int, int, int)

    def __init__(self, records: list[AccountRecord], parent=None, embedded=False) -> None:
        super().__init__(parent)
        if embedded:
            self.setWindowFlags(Qt.Widget)
            self.setMaximumHeight(170)
        self.setWindowTitle("批量登录店铺")
        if not embedded:
            self.resize(720, 500)
        self.completed = False
        self._closing_after_stop = False
        self._thread = QThread(self)
        self._worker = BatchAccountLoginWorker(
            PROJECT_PATHS.python,
            PROJECT_PATHS.main_py,
            PROJECT_PATHS.root,
            records,
        )
        self._worker.moveToThread(self._thread)

        layout = QVBoxLayout(self)
        self._status = QLabel(f"准备处理 {len(records)} 家店铺。登录时只显示当前店铺，不显示密码。")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)
        self._progress = QProgressBar()
        self._progress.setRange(0, len(records))
        layout.addWidget(self._progress)
        self._log = QPlainTextEdit()
        self._log.setReadOnly(True)
        self._log.document().setMaximumBlockCount(500)
        layout.addWidget(self._log, 1)
        hint = QLabel(
            "遇到短信或滑块验证时，正在处理的浏览器会切到前面。请在 5 分钟内人工完成；"
            "成功、失败或超时后会自动关闭该浏览器并继续下一家。"
        )
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #666;")
        layout.addWidget(hint)
        hint.setVisible(not embedded)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self._stop_button = QPushButton("停止批量登录")
        self._stop_button.clicked.connect(self._stop)
        self._close_button = QPushButton("关闭")
        self._close_button.setEnabled(False)
        self._close_button.clicked.connect(self.accept)
        buttons.addWidget(self._stop_button)
        buttons.addWidget(self._close_button)
        layout.addLayout(buttons)
        self._stop_button.setVisible(not embedded)
        self._close_button.setVisible(not embedded)

        self._thread.started.connect(self._worker.start)
        self._worker.progress.connect(self._on_progress)
        self._worker.message.connect(self._append)
        self._worker.shop_result.connect(self._on_result)
        self._worker.finished.connect(self._on_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _append(self, text: str) -> None:
        self._log.appendPlainText(text)

    def _on_progress(self, current: int, total: int, shop: str) -> None:
        self._progress.setValue(current - 1)
        self._status.setText(f"正在处理 {current}/{total}：{shop}")

    def _on_result(self, shop: str, success: bool, message: str) -> None:
        self._append(f"[{shop}] {'成功' if success else '未完成'}：{message}")
        self._progress.setValue(min(self._progress.maximum(), self._progress.value() + 1))

    def _stop(self) -> None:
        if self.completed:
            return
        self._stop_button.setEnabled(False)
        self._status.setText("正在安全停止并关闭当前浏览器……")
        self._worker.stop()

    def stop_batch_login(self) -> None:
        """供店铺管理页的“停止批量登录”按钮安全停止当前队列。"""
        self._stop()

    def _on_finished(self, succeeded: int, failed: int, cancelled: int) -> None:
        self._final_counts = (succeeded, failed, cancelled)
        self._progress.setValue(self._progress.maximum())
        self._stop_button.setEnabled(False)
        self._close_button.setEnabled(True)
        self._status.setText(
            f"处理结束：成功 {succeeded} 家，未完成 {failed} 家，未开始 {cancelled} 家。"
        )

    def _on_thread_finished(self) -> None:
        self.completed = True
        self.batch_finished.emit(*getattr(self, "_final_counts", (0, 0, 0)))
        if self._closing_after_stop:
            self.accept()

    def closeEvent(self, event) -> None:
        if not self.completed:
            self._closing_after_stop = True
            self._stop()
            event.ignore()
            return
        super().closeEvent(event)
