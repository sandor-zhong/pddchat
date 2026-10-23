import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from PyQt5.QtCore import QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

from gui.app.dialogs import shop_browser_dialog as dialog_module
from gui.app.pages import shops_page as page_module


class FakeIO(QObject):
    line_received = pyqtSignal(dict)
    process_exited = pyqtSignal(int)

    def __init__(self, command, cwd, parent=None):
        super().__init__(parent)
        self.command = command
        self.sent = []
        self.focus_browser = MagicMock()

    def start(self):
        pass

    def send(self, command):
        self.sent.append(command)
        return True

    def stderr_text(self):
        return "diagnostic detail"


class ShopBrowserUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def make_dialog(self):
        self.enterContext(patch.object(dialog_module.QTimer, "singleShot"))
        self.enterContext(patch.object(dialog_module, "ProcessLineIO", FakeIO))
        dialog = dialog_module.ShopBrowserDialog("店铺A")
        self.addCleanup(dialog.deleteLater)
        dialog._start()
        return dialog

    def test_ready_keeps_browser_open_until_user_closes_dialog(self):
        dialog = self.make_dialog()
        self.assertEqual(dialog._io.command[-2:], ["--shop", "店铺A"])
        self.assertTrue(dialog._io.command[1].endswith("shop_browser.py"))
        dialog._io.line_received.emit({"type": "shop_browser_ready", "message": "已打开"})
        self.assertEqual(dialog._io.sent, [])
        self.assertTrue(dialog._focus_button.isEnabled())
        dialog._focus_button.click()
        dialog._io.focus_browser.assert_called_once()
        dialog.reject()  # Escape 和关闭按钮都要等子进程真正退出。
        self.assertEqual(dialog._io.sent, [{"cmd": "close"}])
        self.assertFalse(dialog._exited)
        dialog.reject()
        self.assertEqual(len(dialog._io.sent), 1)
        dialog._io.process_exited.emit(0)
        self.assertTrue(dialog._exited)

    def test_browser_error_is_not_overwritten_by_closed_event(self):
        dialog = self.make_dialog()
        dialog._io.line_received.emit({"type": "shop_browser_error", "message": "浏览器被占用"})
        dialog._io.line_received.emit({"type": "shop_browser_closed", "message": "关闭"})
        dialog._io.process_exited.emit(0)
        self.assertEqual(dialog._status.text(), "浏览器被占用")

    def test_abnormal_exit_reports_diagnostic_details(self):
        dialog = self.make_dialog()
        dialog._io.process_exited.emit(1)
        self.assertIn("diagnostic detail", dialog._status.text())
        self.assertFalse(dialog._focus_button.isEnabled())

    def test_single_checked_shop_takes_precedence_over_previous_highlight(self):
        page = SimpleNamespace(
            _model=SimpleNamespace(row_at=lambda _index: SimpleNamespace(shop="旧店铺")),
            _table=SimpleNamespace(currentIndex=lambda: None),
            _selected_shops=lambda: ["已勾选店铺"], _open_shop_browser=MagicMock(),
        )
        page_module.ShopsPage._on_open_shop_clicked(page)
        page._open_shop_browser.assert_called_once_with("已勾选店铺")

    def test_entry_opens_requested_shop_and_blocks_running_tasks(self):
        page = SimpleNamespace(_login_check=None, _batch_is_running=lambda: False)
        with patch.object(page_module.PROJECT_PATHS, "validate", return_value=[]), \
             patch("scheduler_core.active_operation_lock", return_value=None) as active, \
             patch.object(page_module, "ShopBrowserDialog") as dialog, \
             patch.object(page_module.msgbox, "information") as information:
            page_module.ShopsPage._open_shop_browser(page, "店铺A")
            dialog.assert_called_once_with(shop="店铺A", parent=page)
            dialog.return_value.exec_.assert_called_once()
            active.return_value = {"operation": "batch"}
            page_module.ShopsPage._open_shop_browser(page, "店铺B")
            self.assertEqual(dialog.call_count, 1)
            information.assert_called_once()

    def test_shop_page_contains_visible_entry(self):
        with patch.object(page_module, "DbPoller"):
            page = page_module.ShopsPage()
            self.addCleanup(page.deleteLater)
            self.assertEqual(page._open_shop_button.text(), "打开店铺")


if __name__ == "__main__":
    unittest.main()
