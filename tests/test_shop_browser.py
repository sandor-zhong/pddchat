import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import browser_settings
import scheduler_core
import shop_browser
import stealth


class ShopBrowserTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.profile = Path(self.folder.name) / "shop"
        self.profile.mkdir()
        self.output = io.StringIO()
        self.context = MagicMock()
        self.page = MagicMock()
        self.page.is_closed.return_value = False
        self.context.pages = [self.page]
        self.page.wait_for_timeout.side_effect = lambda _ms: setattr(self.context, "pages", [])
        self.enterContext(patch.object(browser_settings, "browser_engine", return_value="chromium"))
        self.launch = self.enterContext(patch.object(stealth, "create_stealth_context", return_value=self.context))
        self.lock = self.enterContext(patch.object(scheduler_core, "acquire_operation_lock", return_value=("lock-1", None)))
        self.release = self.enterContext(patch.object(scheduler_core, "release_operation_lock"))
        # Most tests mimic the live GUI pipe, which remains open without commands.
        self.reader = self.enterContext(patch.object(shop_browser.threading, "Thread"))

    def messages(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def run_browser(self):
        shop_browser.run_shop_browser(object(), self.output, self.profile, "shop")

    def test_reuses_profile_keeps_window_open_and_releases_lock_on_close(self):
        self.run_browser()
        self.assertEqual(self.launch.call_args.kwargs, {"user_data_dir": str(self.profile), "timeout": 30_000})
        self.lock.assert_called_once_with(["shop"], "manual_browser")
        self.page.goto.assert_called_once()
        self.page.wait_for_timeout.assert_called_once_with(250)
        self.context.close.assert_called_once()
        self.release.assert_called_once_with("lock-1")
        self.assertEqual([item["type"] for item in self.messages()], ["shop_browser_ready", "shop_browser_closed"])

    def test_other_order_tabs_remain_open_after_initial_tab_closes(self):
        order_page = MagicMock()
        order_page.is_closed.return_value = False
        self.page.wait_for_timeout.side_effect = lambda _ms: setattr(self.context, "pages", [order_page])
        order_page.wait_for_timeout.side_effect = lambda _ms: setattr(self.context, "pages", [])
        self.run_browser()
        order_page.wait_for_timeout.assert_called_once_with(250)

    def test_navigation_failure_still_allows_manual_browsing(self):
        self.page.goto.side_effect = RuntimeError("network timeout")
        self.run_browser()
        self.assertIn("可手动刷新", self.messages()[0]["message"])
        self.page.wait_for_timeout.assert_called_once()

    def test_missing_profile_does_not_create_new_login_directory(self):
        self.profile.rmdir()
        self.run_browser()
        self.launch.assert_not_called()
        self.lock.assert_not_called()
        self.assertFalse(self.profile.exists())
        self.assertEqual(self.messages()[0]["type"], "shop_browser_error")

    def test_firefox_requires_its_separate_saved_profile(self):
        with patch.object(browser_settings, "browser_engine", return_value="firefox"):
            self.run_browser()
            self.launch.assert_not_called()
            (self.profile / ".firefox").mkdir()
            self.run_browser()
        self.launch.assert_called_once()

    def test_active_task_never_launches_another_browser(self):
        self.lock.return_value = (None, {"operation": "batch"})
        self.run_browser()
        self.launch.assert_not_called()
        self.release.assert_not_called()
        self.assertEqual(self.messages()[0]["type"], "shop_browser_error")

    def test_launch_failure_releases_operation_lock(self):
        self.launch.side_effect = RuntimeError("browser unavailable")
        self.run_browser()
        self.release.assert_called_once_with("lock-1")
        self.assertIn("browser unavailable", self.messages()[0]["message"])

    def test_close_command_or_parent_eof_cancels_before_launch(self):
        for command in ('{"cmd":"close"}\n', "", 'null\n[]\ninvalid\n{"cmd":"close"}\n'):
            with self.subTest(command=command), patch.object(shop_browser.sys, "stdin", io.StringIO(command)):
                self.reader.side_effect = lambda *, target, daemon: MagicMock(start=target)
                self.run_browser()
        self.launch.assert_not_called()
        self.assertEqual(self.release.call_count, 3)


if __name__ == "__main__":
    unittest.main()
