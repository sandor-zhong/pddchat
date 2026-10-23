import unittest
from unittest.mock import MagicMock, patch

import captcha


def state(kind="字符", source="old", ready=True):
    return {"kind": kind, "sources": [source], "ready": ready}


class RefreshConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.page = MagicMock()
        self.button = self.page.locator.return_value.last.locator.return_value.first
        self.button.count.return_value = 1
        self.icon = self.button.locator.return_value.first
        self.icon.count.return_value = 1
        self.log = patch.object(captcha, "_captcha_diagnostic")
        self.log.start()
        self.addCleanup(self.log.stop)

    def test_waits_for_changed_image_to_finish_loading(self):
        self.page.evaluate.side_effect = [
            state(),
            state(source="new", ready=False),
            state(source="new"),
        ]
        captcha._refresh_and_confirm_captcha(self.page, "字符")
        self.icon.click.assert_called_once_with(timeout=3000)
        self.button.click.assert_not_called()
        self.page.wait_for_timeout.assert_called_once()

    def test_accepts_type_change_without_using_old_container(self):
        self.page.evaluate.side_effect = [state(), state(kind="点选")]
        captcha._refresh_and_confirm_captcha(self.page, "字符")
        self.assertEqual(self.page.evaluate.call_count, 2)

    def test_unchanged_image_retries_then_stops(self):
        self.page.evaluate.return_value = state()
        with patch.object(captcha.time, "monotonic", side_effect=[0, 6, 7, 13]):
            with self.assertRaises(captcha.CaptchaRefreshError):
                captcha._refresh_and_confirm_captcha(self.page, "字符")
        self.assertEqual(self.icon.click.call_count, 2)

    def test_missing_button_is_not_silently_ignored(self):
        self.page.evaluate.return_value = state()
        self.button.count.return_value = 0
        with self.assertRaisesRegex(captcha.CaptchaRefreshError, "未找到"):
            captcha._refresh_and_confirm_captcha(self.page, "点选")
        self.button.click.assert_not_called()

    def test_temporary_disappearance_does_not_count_as_refresh(self):
        self.page.evaluate.side_effect = [
            state(),
            state(kind="none", ready=False),
            state(source="new"),
        ]
        captcha._refresh_and_confirm_captcha(self.page, "字符")
        self.page.wait_for_timeout.assert_called_once()

    def test_verification_refreshes_only_unchanged_challenge(self):
        for current, expected in (
            (state(), True),
            (state(source="new"), False),
            (state(kind="点选"), False),
            (state(kind="none", ready=False), False),
            ({**state(), "question": "new question"}, False),
        ):
            with self.subTest(current=current):
                self.page.evaluate.return_value = current
                self.assertEqual(
                    captcha._same_captcha_after_verification(self.page, state()),
                    expected,
                )
        self.button.click.assert_not_called()


if __name__ == "__main__":
    unittest.main()
