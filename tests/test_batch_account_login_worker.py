import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt5.QtWidgets import QApplication, QPushButton

from gui.app.dialogs.account_management_dialog import AccountManagementDialog
from gui.app.pages.shops_page import ShopsPage
from gui.app.services.account_import import AccountRecord
from gui.app.services.shop_repo import ShopSummary
from gui.app.workers import batch_account_login_worker as worker_module


class _FakeProcessLineIO(QObject):
    line_received = pyqtSignal(dict)
    process_exited = pyqtSignal(int)

    def __init__(self, _cmd, cwd, parent=None):
        super().__init__(parent)

    def start(self):
        pass

    def send(self, payload):
        if payload.get("cmd") == "login":
            def complete():
                self.line_received.emit({"type": "login_result", "success": True, "message": "成功"})
                self.process_exited.emit(0)
            QTimer.singleShot(0, complete)
        return True

    def set_browser_topmost(self, _enabled):
        return True

    def focus_browser(self):
        return True


class BatchAccountLoginWorkerTest(unittest.TestCase):
    def test_queue_advances_and_clears_passwords(self):
        app = QApplication.instance() or QApplication([])
        records = [AccountRecord(f"account-{index}", f"password-{index}", f"shop-{index}") for index in range(3)]
        worker = worker_module.BatchAccountLoginWorker("python", "main.py", ".", records)
        result = []
        worker.finished.connect(lambda *counts: (result.append(counts), app.quit()))
        with (
            patch.object(worker_module, "ProcessLineIO", _FakeProcessLineIO),
            patch.object(worker_module.shop_repo, "register_imported_shops"),
            patch.object(worker_module.shop_repo, "mark_shop_login_succeeded"),
        ):
            QTimer.singleShot(0, worker.start)
            QTimer.singleShot(2000, app.quit)
            app.exec_()

        self.assertEqual(result, [(3, 0, 0)])
        self.assertTrue(all(record.password == "" for record in records))

    def test_shop_page_exposes_excel_batch_button(self):
        _app = QApplication.instance() or QApplication([])
        with patch("gui.app.pages.shops_page.DbPoller.start"):
            page = ShopsPage()
        labels = [button.text() for button in page.findChildren(QPushButton)]
        self.assertIn("账号列表录入 / 批量登录", labels)
        self.assertIn("批量登录选中店铺", labels)
        self.assertIn("停止批量登录", labels)
        self.assertGreaterEqual(page._batch_login_button.minimumHeight(), 32)
        self.assertGreaterEqual(page._stop_batch_login_button.minimumHeight(), 32)
        self.assertGreaterEqual(page._batch_login_button.minimumWidth(), 150)
        page.close()

    def test_shop_checks_survive_filter_and_refresh(self):
        _app = QApplication.instance() or QApplication([])
        with patch("gui.app.pages.shops_page.DbPoller.start"):
            page = ShopsPage()

        def summary(shop):
            return ShopSummary(
                shop=shop,
                has_profile=False,
                disabled=False,
                schedule_status="not_started",
                schedule_detail=None,
                login_expired=False,
                pending_remaining=0,
                submitted_today=0,
                failed_today=0,
                skipped_today=0,
                touched_today=0,
                last_activity=None,
                max_orders=None,
                group_name=None,
            )

        rows = [summary("店铺甲"), summary("店铺乙")]
        page._on_data(rows)
        self.assertTrue(page._model.setData(page._model.index(0, 0), Qt.Checked, Qt.CheckStateRole))
        self.assertEqual(page._selected_shops(), ["店铺甲"])
        page._apply_filter("店铺乙")
        self.assertEqual(page._selected_shops(), ["店铺甲"])
        page._on_data(rows)
        self.assertEqual(page._selected_shops(), ["店铺甲"])
        page._on_data([rows[1]])
        self.assertEqual(page._selected_shops(), [])
        page.close()

    def test_selected_batch_login_uses_only_checked_saved_accounts(self):
        _app = QApplication.instance() or QApplication([])
        with patch("gui.app.pages.shops_page.DbPoller.start"):
            page = ShopsPage()

        def summary(shop):
            return ShopSummary(
                shop=shop,
                has_profile=False,
                disabled=False,
                schedule_status="not_started",
                schedule_detail=None,
                login_expired=False,
                pending_remaining=0,
                submitted_today=0,
                failed_today=0,
                skipped_today=0,
                touched_today=0,
                last_activity=None,
                max_orders=None,
                group_name=None,
            )

        page._on_data([summary("店铺甲"), summary("店铺乙")])
        page._model.setData(page._model.index(1, 0), Qt.Checked, Qt.CheckStateRole)
        records = [
            AccountRecord("账号甲", "密码甲", "店铺甲"),
            AccountRecord("账号乙", "密码乙", "店铺乙"),
        ]
        with (
            patch("gui.app.pages.shops_page.PROJECT_PATHS.validate", return_value=[]),
            patch("gui.app.pages.shops_page.account_store.list_accounts", return_value=records),
            patch.object(page, "_start_batch_login") as start,
        ):
            page._on_batch_login_selected()

        start.assert_called_once()
        selected = start.call_args.args[0]
        self.assertEqual([record.shop for record in selected], ["店铺乙"])
        self.assertEqual(records[0].password, "")
        self.assertEqual(records[1].password, "密码乙")
        page.close()

    def test_account_dialog_matches_edit_and_login_workflow(self):
        _app = QApplication.instance() or QApplication([])
        with patch("gui.app.dialogs.account_management_dialog.account_store.list_accounts", return_value=[]):
            dialog = AccountManagementDialog()
        labels = [button.text() for button in dialog.findChildren(QPushButton)]
        self.assertIn("新增一行", labels)
        self.assertIn("粘贴多行（追加）", labels)
        self.assertIn("读取 Excel…", labels)
        self.assertIn("保存列表", labels)
        self.assertIn("保存并批量登录", labels)
        self.assertEqual(dialog._table.horizontalHeaderItem(0).text(), "分组（可选）")
        self.assertEqual(dialog._table.horizontalHeaderItem(3).text(), "密码")
        dialog.reject()


if __name__ == "__main__":
    unittest.main()
