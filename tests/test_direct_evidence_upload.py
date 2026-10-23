import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import appeal_submit
import reconsideration
import aftersale_appeal


class DirectEvidenceUploadTests(unittest.TestCase):
    def test_three_flows_use_file_input_for_single_and_multiple_controls(self):
        files = [Path("first.png"), Path("second.png")]
        resolved = [str(path.resolve()) for path in files]
        for flow in ("negative", "reconsideration", "aftersale"):
            for multiple in (None, ""):
                with self.subTest(flow=flow, multiple=multiple):
                    page, modal, file_input = MagicMock(), MagicMock(), MagicMock()
                    file_input.count.return_value = 1
                    file_input.get_attribute.return_value = multiple
                    modal.locator.return_value = file_input
                    file_input.locator.return_value = file_input
                    with patch.object(Path, "is_file", return_value=True), patch("reconsideration.jitter_wait"):
                        if flow == "negative":
                            appeal_submit.upload_evidence_images(page, modal, files, example_thumbnail_count=None)
                        elif flow == "reconsideration":
                            self.assertEqual(reconsideration._upload_original_evidence(page, modal, files), files)
                        else:
                            self.assertEqual(aftersale_appeal._upload_images(modal, "test", files, required=True), files)
                    expected = [call(resolved)] if multiple is not None else [call(path) for path in resolved]
                    self.assertEqual(file_input.set_input_files.call_args_list, expected)
                    page.expect_file_chooser.assert_not_called()
                    file_input.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
