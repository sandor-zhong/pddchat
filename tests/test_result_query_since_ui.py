import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtCore import QDateTime, QObject, pyqtSignal
from PyQt5.QtWidgets import QApplication

import result_query_settings as settings
from gui.app.pages import run_page
from gui.app.workers import appeal_result_check_worker, reconsideration_result_check_worker, aftersale_result_check_worker


class FakeIO(QObject):
    line_received = pyqtSignal(dict)
    process_exited = pyqtSignal(int)
    latest = None

    def __init__(self, cmd, cwd, parent=None):
        super().__init__(parent)
        self.cmd = cmd
        FakeIO.latest = self

    def start(self):
        pass


class QuerySinceUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(settings, "SETTINGS_PATH", Path(self.folder.name) / "settings.json"))

    def page(self):
        self.enterContext(patch.object(run_page, "DbPoller"))
        page = run_page.RunPage()
        self.addCleanup(page.deleteLater)
        return page

    def test_shared_control_saves_beijing_time_and_restores_on_reopen(self):
        page = self.page()
        self.assertIsNone(page._result_query_since())
        page._result_since_edit.setDateTime(QDateTime.fromString("2026-10-20 12:34:56", "yyyy-MM-dd HH:mm:ss"))
        page._result_since_enabled.setChecked(True)
        self.assertEqual(page._result_query_since(), "2026-10-20T12:34:56+08:00")
        self.assertEqual(settings.active_since(), page._result_query_since())
        reopened = self.page()
        self.assertTrue(reopened._result_since_enabled.isChecked())
        self.assertEqual(reopened._result_query_since(), page._result_query_since())

    def test_negative_query_failure_shows_warning_with_error(self):
        page = self.page()
        page._begin_progress_tracking(["shop"], "等待查询")
        with patch.object(page, "_append_log"), \
             patch.object(run_page.msgbox, "warning") as warning, \
             patch.object(run_page.msgbox, "information") as info:
            page._on_check_results_shop_finished("shop", False, 0, "net::ERR_ABORTED")
            page._on_check_results_all_finished()
        warning.assert_called_once()
        self.assertIn("查询失败", warning.call_args.args[1])
        self.assertIn("net::ERR_ABORTED", warning.call_args.args[2])
        info.assert_not_called()

    def test_negative_query_skip_explains_reason_instead_of_zero_update_popup(self):
        page = self.page()
        page._begin_progress_tracking(["shop"], "等待查询")
        reason = "本地有 2 条待查记录，但提交时间早于起点。"
        worker = appeal_result_check_worker.AppealResultCheckWorker("python", "main.py", ".", [])
        self.addCleanup(worker.deleteLater)
        worker._current_shop = "shop"
        worker.shop_skipped.connect(page._on_check_results_shop_skipped)
        worker.shop_finished.connect(page._on_check_results_shop_finished)
        with patch.object(page, "_append_log"), \
             patch.object(run_page.msgbox, "warning") as warning, \
             patch.object(run_page.msgbox, "information") as info:
            worker._on_line({"type": "check_result", "success": True, "skipped": True, "updated_count": 0, "message": reason})
            page._on_check_results_all_finished()
        warning.assert_not_called()
        self.assertEqual(info.call_args.args[1], "没有符合条件的待查订单")
        self.assertIn(reason, info.call_args.args[2])
        self.assertIn("未打开浏览器", info.call_args.args[2])

    def test_negative_query_partial_failure_preserves_successful_updates(self):
        page = self.page()
        page._begin_progress_tracking(["ok", "failed"], "等待查询")
        with patch.object(page, "_append_log"), patch.object(run_page.msgbox, "warning") as warning:
            page._on_check_results_shop_finished("ok", True, 3, "")
            page._on_check_results_shop_finished("failed", False, 0, "登录已失效")
            page._on_check_results_all_finished()
        self.assertIn("3 条订单", warning.call_args.args[2])
        self.assertIn("登录已失效", warning.call_args.args[2])

    def test_some_skipped_shops_do_not_hide_a_completed_zero_update_query(self):
        page = self.page()
        page._begin_progress_tracking(["empty", "checked"], "等待查询")
        with patch.object(page, "_append_log"), patch.object(run_page.msgbox, "information") as info:
            page._on_check_results_shop_skipped("empty", "没有待查记录")
            page._on_check_results_shop_finished("empty", True, 0, "没有待查记录")
            page._on_check_results_shop_finished("checked", True, 0, "")
            page._on_check_results_all_finished()
        self.assertEqual(info.call_args.args[1], "查询完成")
        self.assertIn("empty", info.call_args.args[2])

    def test_each_worker_passes_fixed_range_to_child(self):
        classes = (
            (appeal_result_check_worker, appeal_result_check_worker.AppealResultCheckWorker),
            (reconsideration_result_check_worker, reconsideration_result_check_worker.ReconsiderationResultCheckWorker),
            (aftersale_result_check_worker, aftersale_result_check_worker.AftersaleResultCheckWorker),
        )
        for module, cls in classes:
            for since, expected in (("2026-10-20 12:34:56", "2026-10-20T12:34:56+08:00"), (None, "all")):
                with self.subTest(worker=cls.__name__, since=since), patch.object(module, "ProcessLineIO", FakeIO):
                    worker = cls("python.exe", "main.py", ".", ["shop"], submitted_since=since)
                    self.addCleanup(worker.deleteLater)
                    settings.save_settings(True, "2020-01-01 00:00:00")
                    worker.start()
                    cmd = FakeIO.latest.cmd
                    self.assertEqual(cmd[cmd.index("--result-submitted-since") + 1], expected)

    def result_cases(self):
        return (
            ("复议", "_on_check_reconsider_shop_finished", "_on_check_reconsider_all_finished"),
            ("售后", "_on_check_aftersale_shop_finished", "_on_check_aftersale_all_finished"),
        )

    def test_both_batch_results_show_skip_reasons_without_zero_update_popup(self):
        for kind, shop_slot, finish_slot in self.result_cases():
            with self.subTest(kind=kind):
                page = self.page()
                page._begin_progress_tracking(["shop"], "等待查询")
                reason = "本地记录均早于 2026-10-18，请调整提交时间起点。"
                with patch.object(page, "_append_log"), patch.object(run_page.msgbox, "information") as info, patch.object(run_page.msgbox, "warning") as warning:
                    getattr(page, shop_slot)("shop", True, {"skipped": True, "complete": True}, reason)
                    getattr(page, finish_slot)()
                warning.assert_not_called()
                self.assertEqual(info.call_args.args[1], "没有符合条件的待查订单")
                self.assertIn(reason, info.call_args.args[2])
                self.assertNotIn("共更新 0", info.call_args.args[2])
                self.assertNotIn("shop", page._run_failed_shops)

    def test_both_batch_failures_show_specific_errors(self):
        for kind, shop_slot, finish_slot in self.result_cases():
            with self.subTest(kind=kind):
                page = self.page()
                page._begin_progress_tracking(["shop"], "等待查询")
                with patch.object(page, "_append_log"), patch.object(run_page.msgbox, "warning") as warning, patch.object(run_page.msgbox, "information") as info:
                    getattr(page, shop_slot)("shop", False, {}, "登录已失效，需要重新登录")
                    getattr(page, finish_slot)()
                info.assert_not_called()
                self.assertIn("查询失败", warning.call_args.args[2])
                self.assertIn("登录已失效", warning.call_args.args[2])

    def test_both_batch_incomplete_results_keep_successful_updates(self):
        for kind, shop_slot, finish_slot in self.result_cases():
            with self.subTest(kind=kind):
                page = self.page()
                page._begin_progress_tracking(["shop"], "等待查询")
                result = {"complete": False, "unmatched_local_count": 2, "updates": [{"status": "申诉成功"}]}
                with patch.object(page, "_append_log"), patch.object(run_page.msgbox, "warning") as warning:
                    getattr(page, shop_slot)("shop", True, result, "还有 2 条未匹配")
                    getattr(page, finish_slot)()
                self.assertIn("共更新 1 条", warning.call_args.args[2])
                self.assertIn("未查齐", warning.call_args.args[2])
                self.assertIn("还有 2 条未匹配", warning.call_args.args[2])

    def test_batch_skip_state_is_reset_between_runs(self):
        page = self.page()
        page._result_check_skipped["old"] = "没有记录"
        page._result_check_issues["old"] = "查询失败"
        page._begin_progress_tracking(["new"], "等待查询")
        self.assertEqual(page._result_check_skipped, {})
        self.assertEqual(page._result_check_issues, {})


if __name__ == "__main__":
    unittest.main()
