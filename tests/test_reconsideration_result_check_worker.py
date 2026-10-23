from __future__ import annotations

import unittest
from typing import ClassVar
from unittest.mock import patch

try:
    from PyQt5.QtCore import QObject, pyqtSignal
except ImportError:
    raise unittest.SkipTest("需要 GUI venv 中的 PyQt5")
else:
    QT_AVAILABLE = True
    from gui.app.workers import reconsideration_result_check_worker as worker_module


if QT_AVAILABLE:

    class FakeProcessLineIO(QObject):
        line_received = pyqtSignal(dict)
        process_exited = pyqtSignal(int)
        instances: ClassVar[list[FakeProcessLineIO]] = []

        def __init__(self, cmd, cwd, parent=None) -> None:
            super().__init__(parent)
            self.cmd = list(cmd)
            self.cwd = str(cwd)
            self.__class__.instances.append(self)

        @property
        def shop(self) -> str:
            return self.cmd[self.cmd.index("--shop") + 1]

        def start(self) -> None:
            return

        def stderr_text(self) -> str:
            return ""


@unittest.skipUnless(QT_AVAILABLE, "需要 GUI venv 中的 PyQt5")
class ReconsiderationResultCheckWorkerTest(unittest.TestCase):
    def setUp(self) -> None:
        FakeProcessLineIO.instances.clear()

    @patch.object(worker_module, "ProcessLineIO", FakeProcessLineIO)
    def test_shops_are_queried_strictly_sequentially(self) -> None:
        worker = worker_module.ReconsiderationResultCheckWorker(
            pddchat_python="python.exe",
            main_py="main.py",
            pddchat_root=".",
            shops=["店铺A", "店铺B"],
        )
        finished: list[tuple[str, bool, dict]] = []
        all_finished: list[bool] = []
        worker.shop_finished.connect(
            lambda shop, success, result, _message: finished.append(
                (shop, success, result)
            )
        )
        worker.all_finished.connect(lambda: all_finished.append(True))

        worker.start()
        self.assertEqual(len(FakeProcessLineIO.instances), 1)
        first = FakeProcessLineIO.instances[0]
        self.assertEqual(first.shop, "店铺A")
        self.assertIn("--check-reconsideration-results", first.cmd)

        first.line_received.emit({
            "type": "reconsider_check_result",
            "success": True,
            "updates": [{"order_no": "260001-1", "status": "申诉成功"}],
        })
        first.process_exited.emit(0)
        self.assertEqual(len(FakeProcessLineIO.instances), 2)
        self.assertEqual(FakeProcessLineIO.instances[1].shop, "店铺B")
        self.assertEqual(finished[0][0:2], ("店铺A", True))

        second = FakeProcessLineIO.instances[1]
        second.line_received.emit({
            "type": "reconsider_check_result",
            "success": True,
            "updates": [],
        })
        second.process_exited.emit(0)
        self.assertEqual(all_finished, [True])

    @patch.object(worker_module, "ProcessLineIO", FakeProcessLineIO)
    def test_cancel_skips_remaining_shops(self) -> None:
        worker = worker_module.ReconsiderationResultCheckWorker(
            pddchat_python="python.exe",
            main_py="main.py",
            pddchat_root=".",
            shops=["店铺A", "店铺B"],
        )
        all_finished: list[bool] = []
        worker.all_finished.connect(lambda: all_finished.append(True))
        worker.start()
        worker.cancel()
        first = FakeProcessLineIO.instances[0]
        first.line_received.emit({
            "type": "reconsider_check_result",
            "success": True,
            "updates": [],
        })
        first.process_exited.emit(0)

        self.assertEqual(len(FakeProcessLineIO.instances), 1)
        self.assertEqual(all_finished, [True])


if __name__ == "__main__":
    unittest.main()
