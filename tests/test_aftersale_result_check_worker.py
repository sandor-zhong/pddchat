from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import MagicMock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PyQt5.QtCore import QObject, pyqtSignal
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QApplication
except ImportError:
    raise unittest.SkipTest("需要 GUI venv 中的 PyQt5")

from gui.app.workers import aftersale_result_check_worker as worker_module
from gui.app.pages import run_page as run_page_module

_APP = QApplication.instance() or QApplication([])


class FakeProcessLineIO(QObject):
    line_received = pyqtSignal(dict)
    process_exited = pyqtSignal(int)
    instances: ClassVar[list] = []

    def __init__(self, cmd, cwd, parent=None) -> None:
        super().__init__(parent)
        self.cmd = list(cmd)
        self.__class__.instances.append(self)

    def start(self) -> None:
        pass

    def stderr_text(self) -> str:
        return ""

    def finish(self) -> None:
        self.line_received.emit({"type": "aftersale_check_result", "success": True, "updates": []})
        self.process_exited.emit(0)


class AftersaleResultCheckWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeProcessLineIO.instances.clear()
        self.enterContext(patch.object(worker_module, "ProcessLineIO", FakeProcessLineIO))
        self.worker = worker_module.AftersaleResultCheckWorker(
            "python.exe", "main.py", ".", ["店铺A", "店铺B"], query_interval_seconds=1
        )
        self.finished: list[bool] = []
        self.worker.all_finished.connect(lambda: self.finished.append(True))
        self.addCleanup(self.worker._next_shop_timer.stop)

    def test_interval_is_passed_to_child_and_delays_next_shop(self) -> None:
        self.worker.start()
        first = FakeProcessLineIO.instances[0]
        option = first.cmd.index("--aftersale-query-interval")
        self.assertEqual(first.cmd[option + 1], "1")
        first.finish()
        self.assertTrue(self.worker._next_shop_timer.isActive())
        self.assertEqual(self.worker._next_shop_timer.interval(), 1000)
        self.assertEqual(len(FakeProcessLineIO.instances), 1)
        QTest.qWait(1200)
        self.assertEqual(len(FakeProcessLineIO.instances), 2)
        FakeProcessLineIO.instances[1].finish()
        self.assertEqual(self.finished, [True])
        self.assertFalse(self.worker._next_shop_timer.isActive())

    def test_cancel_during_interval_finishes_without_launching_next_shop(self) -> None:
        self.worker.start()
        FakeProcessLineIO.instances[0].finish()
        self.worker.cancel()
        QTest.qWait(20)
        self.assertEqual(len(FakeProcessLineIO.instances), 1)
        self.assertFalse(self.worker._next_shop_timer.isActive())
        self.assertEqual(self.finished, [True])

    def test_cancel_during_active_shop_waits_for_that_shop_only(self) -> None:
        self.worker.start()
        self.worker.cancel()
        QTest.qWait(20)
        self.assertEqual(self.finished, [])
        FakeProcessLineIO.instances[0].finish()
        self.assertEqual(len(FakeProcessLineIO.instances), 1)
        self.assertEqual(self.finished, [True])


class AftersaleResultSummaryTest(unittest.TestCase):
    def test_failed_shop_shows_failure_warning_instead_of_success_popup(self) -> None:
        page = SimpleNamespace(
            _run_failed_shops={"未登录店铺"},
            _run_shops=["未登录店铺"],
            _run_finished_elapsed=1.0,
            _check_aftersale_results_lock_id="lock-1",
            _check_aftersale_results_worker=object(),
            _check_aftersale_results_thread=object(),
            _check_aftersale_result_counts={},
            _check_aftersale_results_updated=0,
            _complete_progress_tracking=MagicMock(),
            _restore_buttons_after_aftersale_check=MagicMock(),
            _append_log=MagicMock(),
            _result_check_skipped={},
            _result_check_issues={"未登录店铺": "查询失败：店铺未登录"},
        )
        page._show_result_check_summary = lambda *args: run_page_module.RunPage._show_result_check_summary(page, *args)
        with (
            patch.object(run_page_module.scheduler_core, "release_operation_lock"),
            patch.object(run_page_module.msgbox, "warning") as warning,
            patch.object(run_page_module.msgbox, "information") as information,
        ):
            run_page_module.RunPage._on_check_aftersale_all_finished(page)

        information.assert_not_called()
        warning.assert_called_once()
        self.assertEqual(warning.call_args.args[1], "售后结果查询失败或未查齐")
        self.assertIn("未登录店铺", warning.call_args.args[2])


if __name__ == "__main__":
    unittest.main()
