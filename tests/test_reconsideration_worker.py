from __future__ import annotations

import unittest
from unittest.mock import patch

try:
    from PyQt5.QtCore import QObject, pyqtSignal
except ImportError:  # 主流水线 venv 不含 GUI 依赖；GUI venv 会实际执行这些测试。
    raise unittest.SkipTest("需要 GUI venv 中的 PyQt5")
else:
    QT_AVAILABLE = True
    from gui.app.workers import reconsideration_worker as worker_module


if QT_AVAILABLE:

    class FakeProcessLineIO(QObject):
        line_received = pyqtSignal(dict)
        process_exited = pyqtSignal(int)
        instances: list["FakeProcessLineIO"] = []

        def __init__(self, cmd, cwd, parent=None) -> None:
            super().__init__(parent)
            self.cmd = list(cmd)
            self.cwd = str(cwd)
            self.sent: list[dict] = []
            self.__class__.instances.append(self)

        @property
        def shop(self) -> str:
            return self.cmd[self.cmd.index("--shop") + 1]

        def start(self) -> None:
            return

        def send(self, message: dict) -> bool:
            self.sent.append(dict(message))
            return True

        def stderr_text(self) -> str:
            return ""


@unittest.skipUnless(QT_AVAILABLE, "需要 GUI venv 中的 PyQt5")
class ReconsiderationWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeProcessLineIO.instances.clear()

    def _make_worker(self, shops, pool_size=2):
        return worker_module.ReconsiderationWorker(
            pddchat_python="python.exe",
            main_py="main.py",
            pddchat_root=".",
            shops=list(shops),
            max_orders=20,
            pool_size=pool_size,
        )

    @patch.object(worker_module.random, "choice", side_effect=lambda items: items[0])
    @patch.object(worker_module.QTimer, "singleShot", side_effect=lambda _ms, fn: fn())
    @patch.object(worker_module, "ProcessLineIO", FakeProcessLineIO)
    def test_multiple_browsers_stay_open_but_only_one_shop_gets_next(
        self, _timer, _choice
    ) -> None:
        worker = self._make_worker(["店铺A", "店铺B"], pool_size=2)
        worker.start()

        first = FakeProcessLineIO.instances[0]
        activity = []
        worker.shop_activity.connect(lambda *args: activity.append(args))
        self.assertEqual(first.shop, "店铺A")
        self.assertEqual(first.sent, [{"cmd": "init"}])
        first.line_received.emit({"type": "reconsider_ready", "queue_size": 2})

        second = FakeProcessLineIO.instances[1]
        self.assertEqual(second.shop, "店铺B")
        second.line_received.emit({"type": "reconsider_ready", "queue_size": 2})

        next_counts = [
            sum(message.get("cmd") == "next" for message in io.sent)
            for io in (first, second)
        ]
        self.assertEqual(next_counts, [1, 0])
        first.line_received.emit({
            "type": "activity", "stage": "正在发起复议", "order_no": "A-1"
        })
        self.assertEqual(activity, [("店铺A", "正在发起复议", "A-1")])

        first.line_received.emit({
            "type": "reconsider_order_result",
            "order_no": "A-1",
            "result": "已提交复议",
            "remaining": 1,
        })
        next_counts = [
            sum(message.get("cmd") == "next" for message in io.sent)
            for io in (first, second)
        ]
        self.assertEqual(next_counts, [1, 1])

    @patch.object(worker_module.random, "choice", side_effect=lambda items: items[0])
    @patch.object(worker_module.QTimer, "singleShot", side_effect=lambda _ms, fn: fn())
    @patch.object(worker_module, "ProcessLineIO", FakeProcessLineIO)
    def test_finished_shop_releases_slot_for_waiting_shop(
        self, _timer, _choice
    ) -> None:
        worker = self._make_worker(["店铺A", "店铺B", "店铺C"], pool_size=2)
        worker.start()
        first = FakeProcessLineIO.instances[0]
        first.line_received.emit({"type": "reconsider_ready", "queue_size": 1})
        second = FakeProcessLineIO.instances[1]
        second.line_received.emit({"type": "reconsider_ready", "queue_size": 1})

        first.line_received.emit({
            "type": "reconsider_order_result",
            "order_no": "A-1",
            "result": "已提交复议",
            "remaining": 0,
        })
        self.assertIn({"cmd": "stop"}, first.sent)
        first.process_exited.emit(0)

        self.assertEqual(len(FakeProcessLineIO.instances), 3)
        self.assertEqual(FakeProcessLineIO.instances[2].shop, "店铺C")
        self.assertEqual(FakeProcessLineIO.instances[2].sent, [{"cmd": "init"}])

    @patch.object(worker_module.random, "choice", side_effect=lambda items: items[0])
    @patch.object(worker_module.QTimer, "singleShot", side_effect=lambda _ms, fn: fn())
    @patch.object(worker_module, "ProcessLineIO", FakeProcessLineIO)
    def test_cancel_notifies_all_open_shops_and_waits_for_exit(
        self, _timer, _choice
    ) -> None:
        worker = self._make_worker(["店铺A", "店铺B"], pool_size=2)
        completions: list[bool] = []
        worker.all_finished.connect(lambda: completions.append(True))
        worker.start()
        first = FakeProcessLineIO.instances[0]
        first.line_received.emit({"type": "reconsider_ready", "queue_size": 2})
        second = FakeProcessLineIO.instances[1]
        second.line_received.emit({"type": "reconsider_ready", "queue_size": 2})

        worker.cancel()
        self.assertIn({"cmd": "stop"}, first.sent)
        self.assertIn({"cmd": "stop"}, second.sent)
        self.assertEqual(completions, [])

        first.process_exited.emit(0)
        self.assertEqual(completions, [])
        second.process_exited.emit(0)
        self.assertEqual(completions, [True])


if __name__ == "__main__":
    unittest.main()
