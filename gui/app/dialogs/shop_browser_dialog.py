"""供人工浏览和检查页面元素的店铺窗口入口。"""

from PyQt5.QtCore import Qt, QTimer
from PyQt5.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from ..services.app_logging import logger
from ..services.project_paths import PROJECT_PATHS
from ..workers.base_process_worker import ProcessLineIO


class ShopBrowserDialog(QDialog):
    def __init__(self, shop: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"打开店铺 · {shop}")
        self.setMinimumWidth(500)
        self._shop = shop
        self._io = None
        self._exited = False
        self._closing = False
        self._got_error = False
        self._got_closed = False
        layout = QVBoxLayout(self)
        instructions = QLabel(
            f"店铺：{shop}\n复用当前选择的浏览器及已保存的登录状态。\n"
            "可手动进入订单或其它后台页面，按 F12 / Ctrl+Shift+I，或右键“检查”查看元素。\n"
            "查看完毕后关闭浏览器，或点击下方“关闭浏览器”。"
        )
        instructions.setWordWrap(True)
        instructions.setTextFormat(Qt.PlainText)
        layout.addWidget(instructions)
        self._status = QLabel("正在打开店铺浏览器……")
        self._status.setWordWrap(True)
        self._status.setTextFormat(Qt.PlainText)
        layout.addWidget(self._status)
        row = QHBoxLayout()
        self._focus_button = QPushButton("前置浏览器")
        self._focus_button.setEnabled(False)
        self._focus_button.clicked.connect(self._focus_browser)
        self._close_button = QPushButton("关闭浏览器")
        self._close_button.clicked.connect(self.reject)
        row.addStretch()
        row.addWidget(self._focus_button)
        row.addWidget(self._close_button)
        layout.addLayout(row)
        QTimer.singleShot(0, self._start)

    def _start(self) -> None:
        if self._closing:
            return
        self._io = ProcessLineIO(
            [str(PROJECT_PATHS.python), str(PROJECT_PATHS.root / "shop_browser.py"),
             "--shop", self._shop],
            cwd=PROJECT_PATHS.root, parent=self,
        )
        self._io.line_received.connect(self._on_line)
        self._io.process_exited.connect(self._on_exit)
        try:
            self._io.start()
        except Exception as exc:
            self._exited = True
            self._got_error = True
            self._status.setText(f"启动浏览器失败：{exc}")
            self._close_button.setText("关闭")
            logger.exception("[打开店铺][%s] 子进程启动失败", self._shop)

    def _on_line(self, message: dict) -> None:
        kind = message.get("type")
        if kind == "shop_browser_error":
            self._got_error = True
            self._status.setText(str(message.get("message") or "打开店铺失败"))
            logger.warning("[打开店铺][%s] %s", self._shop, self._status.text())
        elif kind == "shop_browser_ready" and not self._closing:
            self._status.setText(str(message.get("message") or "浏览器已打开"))
            self._focus_button.setEnabled(True)
        elif kind == "shop_browser_closed":
            self._got_closed = True
            self._focus_button.setEnabled(False)
            if not self._got_error and not self._closing:
                self._status.setText(str(message.get("message") or "浏览器已关闭"))

    def _on_exit(self, exit_code: int) -> None:
        self._exited = True
        self._focus_button.setEnabled(False)
        self._close_button.setEnabled(True)
        self._close_button.setText("关闭")
        if not self._got_error and (exit_code != 0 or not self._got_closed):
            detail = self._io.stderr_text().strip()
            logger.error("[打开店铺][%s] 退出码 %s：%s", self._shop, exit_code, detail)
            self._status.setText(f"店铺浏览器未正常退出（退出码 {exit_code}）。\n{detail[-500:]}")
        if self._closing:
            super().reject()

    def _focus_browser(self) -> None:
        if self._io is not None and not self._exited:
            self._io.focus_browser()

    def reject(self) -> None:
        if self._io is None or self._exited:
            self._closing = True
            super().reject()
            return
        if not self._closing:
            self._closing = True
            self._close_button.setEnabled(False)
            self._focus_button.setEnabled(False)
            self._status.setText("正在关闭浏览器并保存登录状态，请稍候……")
            self._io.send({"cmd": "close"})

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._io is not None and not self._exited:
            event.ignore()
            self.reject()
        else:
            self._closing = True
            event.accept()
