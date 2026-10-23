import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import blockers


class ClickDiagnosticsTests(unittest.TestCase):
    def snapshot_mocks(self):
        page, locator = MagicMock(), MagicMock()
        page.url = "https://example.test/appeal"
        locator.evaluate_all.return_value = {
            "count": 1,
            "samples": [{"target": {"html": "<li>申诉原因</li>"},
                         "center_hits_target": False,
                         "center_hit": {"html": '<div class="overlay"></div>'}}],
        }
        page.locator.return_value.evaluate_all.return_value = [
            {"visible": True, "html": '<div data-testid="beast-core-portal">菜单</div>'}
        ]
        page.screenshot.side_effect = lambda **kw: Path(kw["path"]).write_bytes(b"image")
        return page, locator

    def test_snapshot_saves_original_error_target_dropdown_and_screenshot(self):
        page, locator = self.snapshot_mocks()
        error = blockers.PlaywrightTimeoutError("Call log: overlay intercepts pointer events")
        with tempfile.TemporaryDirectory() as folder, patch.object(blockers, "DEBUG_DIR", Path(folder)):
            report = Path(blockers.save_click_diagnostics(page, locator, "申诉原因", 2, error))
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(data["original_error"], str(error))
            self.assertEqual(data["attempt"], 2)
            self.assertEqual(data["url"], page.url)
            self.assertFalse(data["targets"]["samples"][0]["center_hits_target"])
            self.assertEqual(Path(data["screenshot"]).read_bytes(), b"image")
            html = Path(data["html_path"]).read_text(encoding="utf-8")
            self.assertIn("菜单", html)
            self.assertIn("申诉原因", html)
            self.assertEqual(data["capture_errors"], {})
        page.mouse.click.assert_not_called()
        locator.click.assert_not_called()

    def test_screenshot_failure_still_saves_report_and_html(self):
        page, locator = self.snapshot_mocks()
        page.screenshot.side_effect = blockers.PlaywrightError("page closed")
        with tempfile.TemporaryDirectory() as folder, patch.object(blockers, "DEBUG_DIR", Path(folder)):
            report = blockers.save_click_diagnostics(page, locator, "原因", 1, ValueError("original"))
            data = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(data["original_error"], "original")
            self.assertIn("page closed", data["capture_errors"]["screenshot"])
            self.assertTrue(Path(data["html_path"]).exists())

    def test_missing_target_and_hidden_dropdown_are_retained(self):
        page, locator = self.snapshot_mocks()
        locator.evaluate_all.return_value = {"count": 0, "samples": []}
        page.locator.return_value.evaluate_all.return_value[0]["visible"] = False
        with tempfile.TemporaryDirectory() as folder, patch.object(blockers, "DEBUG_DIR", Path(folder)):
            report = blockers.save_click_diagnostics(page, locator, "原因", 1, ValueError("not visible"))
            data = json.loads(Path(report).read_text(encoding="utf-8"))
            self.assertEqual(data["targets"]["count"], 0)
            self.assertFalse(data["dropdowns"][0]["visible"])

    def test_failed_directory_creation_does_not_raise(self):
        page, locator = self.snapshot_mocks()
        with patch.object(Path, "mkdir", side_effect=PermissionError("read only")):
            self.assertIsNone(blockers.save_click_diagnostics(page, locator, "原因", 1, ValueError("original")))
        locator.evaluate_all.assert_not_called()


class SafeClickErrorTests(unittest.TestCase):
    def mocks(self, stack):
        stack.enter_context(patch.object(blockers, "human_delay"))
        clear = stack.enter_context(patch.object(blockers, "clear_blockers"))
        click = stack.enter_context(patch.object(blockers, "human_click"))
        save = stack.enter_context(patch.object(blockers, "save_click_diagnostics", return_value="click.json"))
        return clear, click, save

    def test_success_creates_no_diagnostics(self):
        for helper in (blockers.safe_click, blockers.stealth_safe_click):
            with self.subTest(helper=helper.__name__), ExitStack() as stack:
                clear, click, save = self.mocks(stack)
                helper(MagicMock(), MagicMock(), "原因")
                click.assert_called_once()
                save.assert_not_called()
                self.assertEqual(clear.call_count, int(helper is blockers.safe_click))

    def test_each_timeout_captures_immediately_before_retry_and_keeps_cause(self):
        for helper in (blockers.safe_click, blockers.stealth_safe_click):
            with self.subTest(helper=helper.__name__), ExitStack() as stack:
                clear, click, save = self.mocks(stack)
                events = []
                error = blockers.PlaywrightTimeoutError("element is not stable")

                def fail(*args, **kw):
                    events.append("click")
                    raise error

                def capture(*args):
                    events.append("capture")
                    return "click.json"

                click.side_effect, save.side_effect = fail, capture
                with self.assertRaises(RuntimeError) as caught:
                    helper(MagicMock(), MagicMock(), "原因")
                self.assertEqual(events, ["click", "capture"] * 3)
                self.assertEqual([call.args[3] for call in save.call_args_list], [1, 2, 3])
                self.assertIs(caught.exception.__cause__, error)
                self.assertIn("element is not stable", str(caught.exception))
                self.assertIn("click.json", str(caught.exception))
                self.assertNotIn("仍被遮挡", str(caught.exception))
                if helper is blockers.stealth_safe_click:
                    clear.assert_not_called()

    def test_non_timeout_error_is_captured_without_repeated_clicks(self):
        for helper in (blockers.safe_click, blockers.stealth_safe_click):
            with self.subTest(helper=helper.__name__), ExitStack() as stack:
                _, click, save = self.mocks(stack)
                error = blockers.PlaywrightError("strict mode violation")
                click.side_effect = error
                with self.assertRaises(RuntimeError) as caught:
                    helper(MagicMock(), MagicMock(), "原因")
                click.assert_called_once()
                save.assert_called_once()
                self.assertIs(caught.exception.__cause__, error)

    def test_visibility_timeout_and_unavailable_report_preserve_original_error(self):
        with ExitStack() as stack:
            _, click, save = self.mocks(stack)
            save.return_value = None
            locator = MagicMock()
            error = blockers.PlaywrightTimeoutError("waiting for visible")
            locator.wait_for.side_effect = error
            with self.assertRaises(RuntimeError) as caught:
                blockers.stealth_safe_click(MagicMock(), locator, "原因", retries=1)
            self.assertIs(caught.exception.__cause__, error)
            self.assertIn("waiting for visible", str(caught.exception))
            click.assert_not_called()
            save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
