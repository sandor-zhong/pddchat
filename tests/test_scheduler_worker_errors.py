import unittest
from unittest.mock import MagicMock, patch

from gui.app.workers.scheduler_thread import SchedulerWorker


class SchedulerWorkerErrorTests(unittest.TestCase):
    def worker(self, forever=False):
        return SchedulerWorker(
            shops=["A"], extra=[], pool_size=1, max_consecutive_errors=3,
            response_timeout=10, python_executable="python.exe", run_forever=forever,
            rest_enabled=False, rest_min_hours=1, rest_max_hours=2,
            rest_min_minutes=1, rest_max_minutes=2)

    def test_constructor_error_is_reported_before_stopped_signal(self):
        worker = self.worker()
        signals = []
        worker.error.connect(lambda message: signals.append(("error", message)))
        worker.stopped.connect(lambda: signals.append(("stopped", "")))
        with patch("gui.app.workers.scheduler_thread.scheduler_core.Scheduler", side_effect=PermissionError("无法读取登录目录")), \
             patch("gui.app.workers.scheduler_thread.logging.getLogger") as logger:
            worker.start()
        self.assertEqual(signals, [("error", "PermissionError: 无法读取登录目录"), ("stopped", "")])
        logger.return_value.exception.assert_called_once()

    def test_run_error_is_reported_before_stopped_for_both_modes(self):
        for forever in (False, True):
            with self.subTest(forever=forever):
                worker = self.worker(forever)
                signals = []
                worker.error.connect(lambda message: signals.append(("error", message)))
                worker.stopped.connect(lambda: signals.append(("stopped", "")))
                scheduler = MagicMock()
                runner = scheduler.run_forever if forever else scheduler.run_once
                runner.side_effect = RuntimeError("调度初始化超时")
                with patch("gui.app.workers.scheduler_thread.scheduler_core.Scheduler", return_value=scheduler), \
                     patch("gui.app.workers.scheduler_thread.logging.getLogger"):
                    worker.start()
                self.assertEqual(signals, [("error", "RuntimeError: 调度初始化超时"), ("stopped", "")])

    def test_normal_completion_does_not_report_error(self):
        worker = self.worker()
        error, stopped = MagicMock(), MagicMock()
        worker.error.connect(error)
        worker.stopped.connect(stopped)
        with patch("gui.app.workers.scheduler_thread.scheduler_core.Scheduler") as scheduler:
            worker.start()
        scheduler.return_value.run_once.assert_called_once()
        error.assert_not_called()
        stopped.assert_called_once()

    def test_stop_requested_before_scheduler_creation_is_preserved(self):
        worker = self.worker()
        worker.request_stop()
        with patch("gui.app.workers.scheduler_thread.scheduler_core.Scheduler") as scheduler:
            worker.start()
        scheduler.return_value.request_stop.assert_called_once()
        scheduler.return_value.run_once.assert_called_once()


if __name__ == "__main__":
    unittest.main()
