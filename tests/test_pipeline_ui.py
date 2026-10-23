import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import unittest
from unittest.mock import MagicMock, patch
from PyQt5.QtCore import Qt, QEventLoop, QThread, QTimer
from PyQt5.QtWidgets import QApplication, QWidget, QTabWidget
import scheduler_core
from gui.app.pages.pipeline_ui import PipelineMixin


class Harness(PipelineMixin, QWidget):
    def __init__(self):
        super().__init__()
        self._pipeline_active = False
        self._pipeline_cancelled = False
        self._worker = self._reconsider_worker = self._aftersale_worker = None
        self._task_tabs = QTabWidget()
        for _ in range(4):
            self._task_tabs.addTab(QWidget(), "task")
        self._pipeline_tab_index = self._task_tabs.addTab(self._build_pipeline_tab(), "pipeline")
        self._checked_shops = lambda: ["A", "B"]
        self.is_running = lambda: self._pipeline_active
        self._append_log = MagicMock()
        self.starts = []
        def start(stage, shops, pool, limit):
            self.starts.append((stage, list(shops), pool, limit))
            setattr(self, ["_worker", "_reconsider_worker", "_aftersale_worker"][stage], object())
        self._start_run = lambda *args: start(0, *args)
        self._start_reconsider = lambda *args: start(1, *args)
        self._start_aftersale = lambda *args: start(2, *args)
        self._request_stop = MagicMock()
        self._request_reconsider_stop = MagicMock()
        self._request_aftersale_stop = MagicMock()


class PipelineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        for target, result in [
            ("scheduler_core.active_operation_lock", None),
            ("gui.app.pages.pipeline_ui.PROJECT_PATHS.validate", []),
            ("gui.app.pages.pipeline_ui.msgbox.confirm_yes_no", True),
            ("gui.app.pages.pipeline_ui.msgbox.information", None),
        ]:
            mocked = patch(target, return_value=result)
            mocked.start()
            self.addCleanup(mocked.stop)
        self.ui = Harness()
        self.addCleanup(self.ui.deleteLater)

    def finish_stage(self):
        self.ui._pipeline_phase_complete = True
        self.ui._pipeline_thread_finished()
        self.app.processEvents()

    def test_order_limits_independent_stages_and_thread_boundary(self):
        self.ui._pipeline_options[0][1].setValue(7)
        self.ui._start_pipeline()
        self.assertEqual(self.ui.starts, [(0, ["A", "B"], 2, 7)])
        self.ui._pipeline_shop_result("A", False, "连续3单失败")
        self.ui._pipeline_shop_result("B", True, "无订单")
        self.assertEqual(len(self.ui.starts), 1)
        self.finish_stage()
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))
        self.ui._pipeline_shop_result("A", True, "没有待复议订单")
        self.ui._pipeline_shop_result("B", True, "完成")
        self.finish_stage()
        self.assertEqual(self.ui.starts[-1][0:2], (2, ["A", "B"]))
        self.ui._pipeline_shop_result("A", True, "没有待售后申诉订单")
        self.ui._pipeline_order("B", "已提交")
        self.ui._pipeline_shop_result("B", True, "完成")
        self.finish_stage()
        self.assertFalse(self.ui._pipeline_active)
        self.assertIn("连续3单失败", self.ui._pipeline_table.item(0, 1).text())
        self.assertIn("完成", self.ui._pipeline_table.item(0, 3).text())

    def test_stop_prevents_later_stages(self):
        self.ui._start_pipeline()
        self.ui._stop_pipeline()
        self.finish_stage()
        self.assertEqual(len(self.ui.starts), 1)
        self.assertFalse(self.ui._pipeline_active)
        self.ui._request_stop.assert_called_once()

    def test_selected_subset_and_unexpected_exit(self):
        self.ui._pipeline_options[0][0].setChecked(False)
        self.ui._start_pipeline()
        self.assertEqual(self.ui.starts[0][0], 1)
        self.ui._pipeline_thread_finished()
        self.app.processEvents()
        self.assertTrue(self.ui._pipeline_active)
        self.assertEqual(self.ui.starts[-1][0:2], (2, ["A", "B"]))
        self.assertFalse(self.ui._pipeline_cancelled)
        self.assertIn("工作线程异常结束", self.ui._pipeline_cell("A", 1).text())
        self.ui._pipeline_shop_result("A", True, "完成")
        self.ui._pipeline_shop_result("B", True, "完成")
        self.finish_stage()
        self.assertFalse(self.ui._pipeline_active)

    def test_all_shops_failing_does_not_block_any_later_stage(self):
        self.ui._start_pipeline()
        for stage in range(3):
            self.assertEqual(self.ui.starts[-1][0:2], (stage, ["A", "B"]))
            for shop in ("A", "B"):
                self.ui._pipeline_shop_result(shop, False, f"阶段{stage}：页面加载超时")
            self.finish_stage()
        self.assertEqual(len(self.ui.starts), 3)
        self.assertFalse(self.ui._pipeline_cancelled)
        self.assertEqual(len(self.ui._pipeline_failures), 6)
        for row in range(2):
            for column in range(1, 4):
                text = self.ui._pipeline_table.item(row, column).text()
                self.assertIn("页面加载超时", text)
                self.assertNotIn("前序阶段异常", text)

    def test_failure_detail_contains_order_numbers_and_original_errors(self):
        self.ui._start_pipeline()
        for index in range(3):
            self.ui._pipeline_order("A", f"失败: 等待申诉表单超时 {index}", f"261016-{index}")
        self.ui._pipeline_shop_result("A", False, "连续 3 单失败，结束当前店铺")
        cell = self.ui._pipeline_cell("A", 0)
        for index in range(3):
            self.assertIn(f"261016-{index}", cell.text())
            self.assertIn(f"等待申诉表单超时 {index}", cell.text())
        self.assertIn("处理3，提交0，失败3", cell.text())
        self.assertEqual(cell.text(), cell.toolTip())
        self.assertEqual(self.ui._pipeline_cell("A", 1).text(), "等待")
        self.assertEqual(self.ui._pipeline_cell("A", 2).text(), "等待")
        self.assertEqual(self.ui._pipeline_table.textElideMode(), Qt.ElideNone)
        with patch("gui.app.pages.pipeline_ui.msgbox.information") as info:
            self.ui._show_pipeline_details(0, 1)
        self.assertEqual(info.call_args.args[2], cell.text())

    def test_order_error_event_preserves_message_without_result_text(self):
        self.ui._start_pipeline()
        self.ui._pipeline_event(scheduler_core.SchedulerEvent(
            type="order_error", shop="A", line="订单出错",
            data={"order_no": "261016-100", "message": "登录已过期，未找到登录凭证"}))
        self.ui._pipeline_event(scheduler_core.SchedulerEvent(
            type="worker_too_many_failures", shop="A", line="连续3单失败", data={}))
        text = self.ui._pipeline_cell("A", 0).text()
        self.assertIn("261016-100", text)
        self.assertIn("登录已过期，未找到登录凭证", text)
        self.assertEqual(self.ui._pipeline_counts[("A", 0)], [1, 0, 1])

    def test_recent_errors_stay_with_their_stage(self):
        self.ui._start_pipeline()
        for index in range(4):
            self.ui._pipeline_order("A", f"失败：负向错误{index}", f"261016-{index}")
        self.ui._pipeline_shop_result("A", True, "完成，有失败订单")
        self.ui._pipeline_shop_result("B", True, "完成")
        old = self.ui._pipeline_cell("A", 0).text()
        self.assertNotIn("负向错误0", old)
        self.assertIn("负向错误3", old)
        self.finish_stage()
        self.ui._pipeline_order("A", "已提交复议", "261016-500")
        self.assertNotIn("负向错误", self.ui._pipeline_cell("A", 1).text())
        self.assertEqual(self.ui._pipeline_cell("A", 0).text(), old)
        self.assertEqual(self.ui._pipeline_counts[("A", 1)], [1, 1, 0])

    def test_stage_start_exception_shows_reason_then_continues(self):
        self.ui._start_run = MagicMock(side_effect=RuntimeError("无法创建浏览器进程"))
        self.ui._start_pipeline()
        self.assertIn("无法创建浏览器进程", self.ui._pipeline_cell("A", 0).text())
        self.assertEqual(self.ui.starts, [])
        self.app.processEvents()
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))
        self.assertFalse(self.ui._pipeline_cancelled)

    def test_started_thread_is_stopped_and_waited_for_before_next_stage(self):
        thread = MagicMock()
        thread.isRunning.return_value = True
        def fail_after_start(*args):
            self.ui._pipeline_watch(thread)
            raise RuntimeError("启动后的页面检查失败")
        self.ui._start_run = fail_after_start
        self.ui._start_pipeline()
        self.app.processEvents()
        self.assertEqual(self.ui.starts, [])
        self.ui._request_stop.assert_called_once_with(reason="当前阶段启动异常")
        self.ui._pipeline_thread_finished(0)
        self.app.processEvents()
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))

    def test_repeated_and_late_thread_signals_do_not_skip_a_stage(self):
        self.ui._start_pipeline()
        self.ui._pipeline_thread_finished(0)
        self.ui._pipeline_thread_finished(0)
        self.app.processEvents()
        self.assertEqual([x[0] for x in self.ui.starts], [0, 1])
        self.ui._pipeline_thread_finished(0)
        self.app.processEvents()
        self.assertEqual([x[0] for x in self.ui.starts], [0, 1])

    def test_scheduler_exception_is_shown_and_does_not_cancel_pipeline(self):
        self.ui._start_pipeline()
        self.ui._on_scheduler_error("PermissionError: 无法读取店铺登录目录")
        for shop in ("A", "B"):
            self.assertIn("PermissionError: 无法读取店铺登录目录", self.ui._pipeline_cell(shop, 0).text())
        self.finish_stage()
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))
        self.assertFalse(self.ui._pipeline_cancelled)

    def test_missing_completion_for_one_shop_is_not_silently_reported_as_success(self):
        self.ui._start_pipeline()
        self.ui._pipeline_shop_result("A", True, "完成")
        self.finish_stage()
        self.assertIn("未返回该店铺的完成结果", self.ui._pipeline_cell("B", 0).text())
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))

    def test_late_success_does_not_overwrite_the_recorded_failure(self):
        self.ui._start_pipeline()
        self.ui._pipeline_shop_result("A", False, "子进程退出码1：浏览器启动失败")
        text = self.ui._pipeline_cell("A", 0).text()
        self.ui._pipeline_shop_result("A", True, "正常结束")
        self.ui._pipeline_order("A", "已提交", "261016-200")
        self.assertEqual(self.ui._pipeline_cell("A", 0).text(), text)

    def test_lock_conflict_reports_the_actual_conflicting_operation(self):
        self.ui._start_pipeline()
        self.ui._pipeline_event(scheduler_core.SchedulerEvent(
            type="lock_conflict", shop=None, data={},
            line="运行冲突：客服违规采集正在使用店铺A的浏览器"))
        self.assertIn("客服违规采集", self.ui._pipeline_cell("A", 0).text())
        self.finish_stage()
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))

    def test_real_qt_scheduler_error_waits_for_thread_exit_before_reconsideration(self):
        from gui.app.workers.scheduler_thread import SchedulerWorker
        loop = QEventLoop()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(loop.quit)
        thread = QThread(self.ui)
        worker = SchedulerWorker(
            shops=["A", "B"], extra=[], pool_size=2, max_consecutive_errors=3,
            response_timeout=10, python_executable="python.exe", run_forever=False,
            rest_enabled=False, rest_min_hours=1, rest_max_hours=2,
            rest_min_minutes=1, rest_max_minutes=2)
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.error.connect(self.ui._on_scheduler_error)
        def release_worker():
            self.ui._worker = None
            self.ui._pipeline_phase_complete = True
        worker.stopped.connect(release_worker)
        worker.stopped.connect(thread.quit)
        thread.finished.connect(worker.deleteLater)
        def start_negative(*args):
            self.ui._worker = worker
            self.ui._pipeline_watch(thread)
            thread.start()
        start_reconsider = self.ui._start_reconsider
        running_at_next_start = []
        def start_next(*args):
            running_at_next_start.append(thread.isRunning())
            start_reconsider(*args)
            loop.quit()
        self.ui._start_run = start_negative
        self.ui._start_reconsider = start_next
        scheduler = MagicMock()
        scheduler.run_once.side_effect = RuntimeError("真实线程中的页面加载超时")
        try:
            with patch("gui.app.workers.scheduler_thread.scheduler_core.Scheduler", return_value=scheduler), \
                 patch("gui.app.workers.scheduler_thread.logging.getLogger"):
                self.ui._start_pipeline()
                timer.start(3000)
                loop.exec_()
                timer.stop()
        finally:
            thread.quit()
            self.assertTrue(thread.wait(3000))
        self.assertEqual(running_at_next_start, [False])
        self.assertEqual(self.ui.starts[-1][0:2], (1, ["A", "B"]))
        self.assertIn("RuntimeError: 真实线程中的页面加载超时", self.ui._pipeline_cell("A", 0).text())
        self.assertFalse(self.ui._pipeline_cancelled)

    def test_run_page_constructs_with_pipeline_tab(self):
        from gui.app.pages.run_page import RunPage
        with patch("gui.app.pages.run_page.DbPoller"), patch.object(RunPage, "_check_auto_schedule"), patch.object(RunPage, "_check_result_schedule"):
            page = RunPage()
            self.assertEqual(page._task_tabs.tabText(page._pipeline_tab_index), "流水线运行")
            page._auto_schedule_timer.stop()
            page._run_progress_timer.stop()
            page.deleteLater()
