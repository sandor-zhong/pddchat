from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

import browser_settings
import memory_monitor
import stealth


class BrowserChoiceTests(unittest.TestCase):
    def test_settings_switch_preserves_chrome_and_other_options(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            patch.object(
                browser_settings, "SETTINGS_PATH", Path(folder) / "settings.json"
            ),
        ):
            browser_settings.save_native_properties(True)
            browser_settings._save(
                chrome_executable="C:/Chrome/chrome.exe", unrelated=17
            )
            browser_settings.save_browser_engine("firefox")
            self.assertEqual(browser_settings.browser_engine(), "firefox")
            self.assertEqual(
                browser_settings.chrome_executable(), "C:/Chrome/chrome.exe"
            )
            self.assertTrue(browser_settings.native_properties_enabled())
            self.assertEqual(browser_settings.load_settings()["unrelated"], 17)
            browser_settings.save_browser_engine("chromium")
            with self.assertRaises(ValueError):
                browser_settings.save_browser_engine("webkit")
            self.assertEqual(browser_settings.browser_engine(), "chromium")

    def test_firefox_profile_does_not_overwrite_existing_chrome_login(self):
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder) / "shop"
            base.mkdir()
            chrome_marker = base / "chrome-login"
            chrome_marker.write_text("preserved", encoding="utf-8")
            firefox = browser_settings.profile_directory(base, "firefox")
            self.assertEqual(firefox.parent, base)
            self.assertNotEqual(firefox, base)
            self.assertFalse(firefox.exists())
            self.assertEqual(browser_settings.profile_directory(base, "chromium"), base)
            self.assertEqual(chrome_marker.read_text(encoding="utf-8"), "preserved")

    def test_firefox_launch_ignores_configured_chrome_and_chrome_flags(self):
        playwright = MagicMock()
        with patch.object(
            browser_settings, "chrome_executable", return_value="missing/chrome.exe"
        ):
            context = stealth.create_stealth_context(
                playwright,
                "shop-profile",
                browser_engine="firefox",
                native_properties=False,
            )
        playwright.chromium.launch_persistent_context.assert_not_called()
        kwargs = playwright.firefox.launch_persistent_context.call_args.kwargs
        self.assertEqual(
            Path(kwargs["user_data_dir"]), Path("shop-profile") / ".firefox"
        )
        for option in (
            "args",
            "channel",
            "ignore_default_args",
            "executable_path",
            "user_agent",
        ):
            self.assertNotIn(option, kwargs)
        context.add_init_script.assert_called_once_with(stealth.STEALTH_INIT_SCRIPT)

    def test_firefox_native_mode_has_no_chrome_options_or_init_script(self):
        playwright = MagicMock()
        context = stealth.create_stealth_context(
            playwright, "shop-profile", browser_engine="firefox", native_properties=True
        )
        context.add_init_script.assert_not_called()
        self.assertNotIn(
            "args", playwright.firefox.launch_persistent_context.call_args.kwargs
        )

    def test_firefox_fill_avoids_unsupported_clipboard_permission(self):
        page, locator = MagicMock(), MagicMock()
        page.context.browser.browser_type.name = "firefox"
        with patch("playwright.sync_api.expect") as expect:
            stealth.paste_text(page, locator, "中文申诉\n完整说明")
        locator.fill.assert_called_once_with("中文申诉\n完整说明")
        page.context.grant_permissions.assert_not_called()
        page.evaluate.assert_not_called()
        expect.return_value.to_have_value.assert_called_once_with(
            "中文申诉\n完整说明", timeout=3000
        )

    def test_chrome_clipboard_behavior_is_preserved(self):
        page, locator = MagicMock(), MagicMock()
        page.url = "https://mms.pinduoduo.com/form"
        page.context.browser.browser_type.name = "chromium"
        with patch("playwright.sync_api.expect"):
            stealth.paste_text(page, locator, "申诉内容")
        locator.fill.assert_not_called()
        page.context.grant_permissions.assert_called_once_with(
            ["clipboard-read", "clipboard-write"], origin="https://mms.pinduoduo.com"
        )
        self.assertEqual(
            [call.args[0] for call in locator.press.call_args_list],
            ["Control+A", "Control+V"],
        )

    def test_browser_process_recognition_includes_firefox_without_python(self):
        for name in ("firefox.exe", "FIREFOX.EXE", "chrome.exe", "msedge.exe"):
            self.assertTrue(memory_monitor.is_browser_process(name))
        self.assertFalse(memory_monitor.is_browser_process("python.exe"))


class FirefoxIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        if not Path(cls.playwright.firefox.executable_path).is_file():
            raise unittest.SkipTest(
                "Run playwright install firefox before browser tests"
            )

    def test_persistent_firefox_forms_popups_screenshots_and_cookie_reuse(self):
        with tempfile.TemporaryDirectory() as folder:
            context = stealth.create_stealth_context(
                self.playwright,
                folder,
                headless=True,
                browser_engine="firefox",
                native_properties=True,
            )
            try:
                page = context.pages[0]
                page.set_content("""<!doctype html><meta charset="utf-8">
                    <button id="open" onclick="window.open('about:blank')">打开详情</button>
                    <dialog id="form"><textarea id="text" oninput="document.querySelector('#preview').textContent=this.value">旧内容</textarea><pre id="preview"></pre></dialog>
                    <script>document.querySelector('#form').showModal()</script>""")
                text = "申诉说明：商品已签收。\n请核对凭证与订单记录。"
                stealth.paste_text(page, page.locator("#text"), text)
                self.assertEqual(page.locator("#text").input_value(), text)
                self.assertEqual(page.locator("#preview").text_content(), text)
                self.assertTrue(page.screenshot().startswith(b"\x89PNG"))
                page.evaluate("document.querySelector('#form').close()")
                with context.expect_page() as popup:
                    page.locator("#open").click()
                popup.value.close()
                self.assertIn("Firefox/", page.evaluate("navigator.userAgent"))
                context.add_cookies(
                    [
                        {
                            "name": "test-login",
                            "value": "persisted",
                            "domain": "example.test",
                            "path": "/",
                            "expires": time.time() + 86400,
                        }
                    ]
                )
            finally:
                context.close()
            self.assertTrue((Path(folder) / ".firefox").is_dir())
            context = stealth.create_stealth_context(
                self.playwright,
                folder,
                headless=True,
                browser_engine="firefox",
                native_properties=False,
            )
            try:
                self.assertEqual(
                    context.cookies("https://example.test")[0]["value"], "persisted"
                )
                page = context.pages[0]
                page.set_content("<input id='order' value='old'>")
                stealth.paste_text(
                    page, page.locator("#order"), "260810-490670733630708"
                )
                self.assertEqual(
                    page.locator("#order").input_value(), "260810-490670733630708"
                )
            finally:
                context.close()


if __name__ == "__main__":
    unittest.main()
