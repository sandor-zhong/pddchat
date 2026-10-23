from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from PyQt5.QtWidgets import QApplication, QComboBox, QLabel, QPushButton

from app.pages.run_page import RunPage
from app.services import shop_repo
import browser_settings


class BrowserSelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.enterContext(patch.object(browser_settings, "SETTINGS_PATH", Path(self.folder.name) / "settings.json"))
        combo = QComboBox()
        combo.addItem("Chrome", "chromium")
        combo.addItem("Firefox", "firefox")
        self.shops = SimpleNamespace(_login_check=None, _batch_is_running=lambda: False,
            _model=SimpleNamespace(login_checks={"old": (True, "chrome login")}), _poller=MagicMock())
        self.page = SimpleNamespace(_browser_engine_combo=combo,
            _chrome_controls=(QPushButton(), QPushButton(), QPushButton()),
            _browser_engine_hint=QLabel(), _shop_poller=MagicMock(),
            is_running=lambda: False, window=lambda: SimpleNamespace(_shops_page=self.shops))
        self.page._update_browser_controls = lambda: RunPage._update_browser_controls(self.page)
        combo.setCurrentIndex(1)

    def test_selection_is_saved_and_old_login_check_display_is_cleared(self):
        RunPage._save_browser_engine(self.page, 1)
        self.assertEqual(browser_settings.browser_engine(), "firefox")
        self.assertTrue(all(not control.isEnabled() for control in self.page._chrome_controls))
        self.assertFalse(self.shops._model.login_checks)
        self.shops._poller.start.assert_called_once()
        self.page._shop_poller.start.assert_called_once()
        self.assertIn("重新登录", self.page._browser_engine_hint.text())
        self.page._browser_engine_combo.setCurrentIndex(0)
        RunPage._save_browser_engine(self.page, 0)
        self.assertEqual(browser_settings.browser_engine(), "chromium")
        self.assertTrue(all(control.isEnabled() for control in self.page._chrome_controls))

    def test_active_task_or_login_prevents_switch(self):
        for mode in ("task", "login-check", "batch-login"):
            with self.subTest(mode=mode), patch("app.pages.run_page.msgbox.warning"):
                self.page.is_running = lambda: mode == "task"
                self.shops._login_check = object() if mode == "login-check" else None
                self.shops._batch_is_running = lambda: mode == "batch-login"
                self.page._browser_engine_combo.setCurrentIndex(1)
                RunPage._save_browser_engine(self.page, 1)
                self.assertEqual(browser_settings.browser_engine(), "chromium")
                self.assertEqual(self.page._browser_engine_combo.currentData(), "chromium")
                self.page._shop_poller.start.assert_not_called()

    def test_failed_settings_write_reverts_selection(self):
        with patch.object(browser_settings, "save_browser_engine", side_effect=OSError("read-only")), patch("app.pages.run_page.msgbox.warning"):
            RunPage._save_browser_engine(self.page, 1)
        self.assertEqual(self.page._browser_engine_combo.currentData(), "chromium")

    def test_login_results_are_saved_separately_for_each_browser(self):
        with patch.object(shop_repo, "IMPORTED_SHOPS_PATH", Path(self.folder.name) / "imported.json"):
            shop_repo.mark_imported_shop_login("shop", True, "Chrome login")
            browser_settings.save_browser_engine("firefox")
            record = shop_repo._load_imported_shops()["shop"]
            self.assertEqual(shop_repo._browser_login_state(record)["status"], "needs_login")
            shop_repo.mark_imported_shop_login("shop", False, "Firefox needs login")
            browser_settings.save_browser_engine("chromium")
            record = shop_repo._load_imported_shops()["shop"]
            self.assertEqual(shop_repo._browser_login_state(record)["status"], "verified")
            browser_settings.save_browser_engine("firefox")
            self.assertEqual(shop_repo._browser_login_state(record)["status"], "needs_login")

    def test_legacy_login_result_is_only_used_for_chrome(self):
        record = {"status": "verified", "message": "old Chrome login"}
        self.assertEqual(shop_repo._browser_login_state(record)["status"], "verified")
        browser_settings.save_browser_engine("firefox")
        self.assertEqual(shop_repo._browser_login_state(record)["status"], "needs_login")


if __name__ == "__main__":
    unittest.main()
