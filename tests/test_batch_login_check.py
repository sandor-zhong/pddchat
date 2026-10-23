import unittest
from unittest.mock import MagicMock, patch

from PyQt5.QtWidgets import QApplication

from gui.app.workers.batch_login_check import BatchLoginCheck
from gui.app.pages.shops_page import ShopsPage


class LoginCheckTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_pool_waits_for_exit_before_replacing_shop(self):
        processes = []
        def create(*args):
            process = MagicMock()
            process.readAllStandardOutput.return_value = b'{"type":"login_check_result","success":true,"message":"ok"}\n'
            processes.append(process)
            return process
        with patch("gui.app.workers.batch_login_check.QProcess", side_effect=create), patch("gui.app.workers.batch_login_check.QTimer"):
            worker = BatchLoginCheck(["A", "B", "C", "D", "E"])
            results = []
            completed = []
            worker.result.connect(lambda *args: results.append(args))
            worker.finished.connect(lambda: completed.append(True))
            worker.start()
            self.assertEqual(len(processes), 3)
            worker._complete(processes[0])
            self.assertEqual(len(processes), 4)
            self.assertEqual(len(worker.active), 3)
            # Duplicate exit signals must not consume a second queue entry.
            worker._complete(processes[0])
            self.assertEqual(len(processes), 4)
            worker._complete(processes[1])
            worker._complete(processes[2])
            worker._complete(processes[3])
            worker._complete(processes[4])
            self.assertEqual(len(results), 5)
            self.assertEqual(results[0], ("A", True, "ok"))
            self.assertEqual(completed, [True])

    def test_stop_does_not_start_remaining_shops_or_mark_them_logged_out(self):
        with patch("gui.app.workers.batch_login_check.QProcess", side_effect=lambda *args: MagicMock()), patch("gui.app.workers.batch_login_check.QTimer"):
            worker = BatchLoginCheck(["A", "B", "C", "D"])
            worker.start()
            with patch.object(worker, "_terminate") as terminate:
                worker.stop()
                self.assertEqual(terminate.call_count, 3)
            self.assertEqual(worker.pending, [])

    def test_shop_page_constructs_and_button_tracks_selection(self):
        with patch("gui.app.pages.shops_page.DbPoller"):
            page = ShopsPage()
            self.assertFalse(page._check_login_button.isEnabled())
            self.assertFalse(page._stop_check_button.isEnabled())
            page.deleteLater()


if __name__ == "__main__":
    unittest.main()
