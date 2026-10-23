import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import captcha
import captcha_diagnostics as diagnostics


class CaptchaDiagnosticsTest(unittest.TestCase):
    def test_archive_statistics_and_failure_only_artifacts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "records"
            for index, passed in enumerate([True, False]):
                diagnostics.save_attempt(root, {
                    "id": str(index), "shop": "shop", "type": "字符", "passed": passed,
                }, {"image.png": b"original image", "element.html": b"<div>captcha</div>"})
            target = Path(folder) / "bundle.zip"
            summary = diagnostics.export_bundle(root, str(target))
            self.assertEqual(summary["attempts"], 2)
            self.assertEqual(summary["pass_rate"], 0.5)
            with zipfile.ZipFile(target) as bundle:
                images = [name for name in bundle.namelist() if name.endswith("image.png")]
                self.assertEqual(len(images), 1)
                self.assertEqual(bundle.read(images[0]), b"original image")
                self.assertEqual(json.loads(bundle.read("summary.json"))["failed"], 1)

    def test_captures_before_solver_refresh_and_does_not_break_on_disk_error(self):
        page = MagicMock()
        page.locator.return_value.last.evaluate.return_value = "old challenge"
        page.locator.return_value.last.screenshot.return_value = b"old image"
        def solve(page):
            captcha._captcha_diagnostic("input did not pass")
            page.locator.return_value.last.screenshot.return_value = b"new challenge"
            return False
        with patch.object(diagnostics, "save_attempt", side_effect=OSError("disk full")) as save:
            self.assertFalse(captcha._recorded_solve(page, "字符", solve, "challenge", 1))
        self.assertEqual(save.call_args.args[2]["captcha.png"], b"old image")
        self.assertIn("input did not pass", save.call_args.args[1]["messages"])
        self.assertIsNone(captcha._active_attempt)
