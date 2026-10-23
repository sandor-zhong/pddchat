from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import httpx
import captcha


class TextCaptchaTest(unittest.TestCase):
    def _page_with_text_captcha(self):
        page = MagicMock()
        captcha_box = MagicMock()
        image = MagicMock()
        target_input = MagicMock()

        visible_boxes = MagicMock()
        visible_boxes.count.return_value = 1
        visible_boxes.last = captcha_box
        page.locator.return_value = visible_boxes

        image_boxes = MagicMock()
        image_boxes.count.return_value = 1
        image_boxes.first = image
        input_boxes = MagicMock()
        input_boxes.count.return_value = 1
        input_boxes.first = target_input

        def locate(selector):
            if selector == captcha.TEXT_CAPTCHA_IMG_SELECTOR:
                return image_boxes
            if selector == captcha.TEXT_CAPTCHA_INPUT_SELECTOR:
                return input_boxes
            raise AssertionError(f"unexpected selector: {selector}")

        captcha_box.locator.side_effect = locate
        return page, captcha_box, image, target_input

    def test_detection_uses_visible_captcha_selectors(self) -> None:
        page = MagicMock()
        page.locator.return_value.count.return_value = 1

        self.assertTrue(captcha.is_captcha_visible(page))
        page.locator.assert_called_once_with(captcha.VISIBLE_CAPTCHA_SELECTOR)

        page.reset_mock()
        self.assertTrue(captcha.is_text_captcha(page))
        page.locator.assert_called_once_with(captcha.VISIBLE_TEXT_CAPTCHA_SELECTOR)

    def test_normalize_accepts_only_directly_fillable_result(self) -> None:
        self.assertEqual(captcha._normalize_text_captcha_result(" C5 Qae\n"), "C5Qae")
        self.assertEqual(captcha._normalize_text_captcha_result("`ZCHG68`"), "ZCHG68")
        self.assertEqual(captcha._normalize_text_captcha_result("答案是 C5Qae"), "")
        self.assertEqual(captcha._normalize_text_captcha_result("C5-Qae"), "")
        self.assertEqual(captcha._normalize_text_captcha_result("UNKNOWN"), "")

    def test_ai_result_is_typed_and_verified_in_input(self) -> None:
        page, _captcha_box, image, target_input = self._page_with_text_captcha()
        with (
            patch.object(captcha, "_extract_captcha_image_bytes", return_value=b"png"),
            patch.object(captcha, "_ai_recognize_text_captcha", return_value="C5Qae"),
            patch.object(captcha, "human_click") as human_click,
            patch.object(captcha, "human_type") as human_type,
            patch.object(captcha, "jitter_wait"),
            patch.object(captcha, "_captcha_diagnostic"),
        ):
            result = captcha.try_auto_solve_text_captcha(page)

        self.assertTrue(result)
        human_click.assert_called_once_with(page, target_input)
        target_input.press.assert_any_call("Control+A")
        target_input.press.assert_any_call("Backspace")
        target_input.click.assert_not_called()
        target_input.input_value.assert_not_called()
        target_input.fill.assert_not_called()
        human_type.assert_called_once_with(
            page, target_input, "C5Qae", click_first=False
        )
        page.wait_for_selector.assert_called_once_with(
            captcha.VISIBLE_CAPTCHA_SELECTOR, state="hidden", timeout=5_000
        )
        image.screenshot.assert_not_called()

    def test_invalid_ai_response_is_not_entered(self) -> None:
        page, _captcha_box, _image, target_input = self._page_with_text_captcha()

        with (
            patch.object(captcha, "_extract_captcha_image_bytes", return_value=b"png"),
            patch.object(captcha, "_ai_recognize_text_captcha", return_value="答案是 C5Qae"),
            patch.object(captcha, "_refresh_text_captcha") as refresh,
            patch.object(captcha, "human_type") as human_type,
            patch.object(captcha, "_captcha_diagnostic"),
        ):
            result = captcha.try_auto_solve_text_captcha(page)

        self.assertFalse(result)
        human_type.assert_not_called()
        target_input.fill.assert_not_called()


    def test_ai_client_loads_project_dotenv_before_reading_key(self) -> None:
        expected_env = captcha.Path(captcha.__file__).resolve().parent / ".env"
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": ""}),
            patch.object(captcha, "load_dotenv") as load_dotenv,
        ):
            with self.assertRaisesRegex(RuntimeError, "QWEN_API_KEY"):
                captcha._ai_recognize_text_captcha("data:image/png;base64,AA==")

        load_dotenv.assert_called_once_with(
            dotenv_path=expected_env, override=False
        )

    def test_ai_recognition_uses_qwen_client_with_60_second_timeout(self) -> None:
        client = MagicMock()
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch.object(captcha, "load_dotenv"),
            patch("qwen_client.create_client") as create_client,
            patch(
                "qwen_client.recognize_captcha_data_uri", return_value="aYpM7"
            ) as recognize,
        ):
            create_client.return_value.__enter__.return_value = client
            result = captcha._ai_recognize_text_captcha(
                "data:image/png;base64,AA=="
            )

        self.assertEqual(result, "aYpM7")
        create_client.assert_called_once_with(api_key="test-key", timeout=60)
        recognize.assert_called_once_with(
            client,
            "data:image/png;base64,AA==",
            enable_thinking=False,
        )

    def test_ai_timeout_refreshes_challenge_before_retry(self) -> None:
        page, captcha_box, _image, target_input = self._page_with_text_captcha()

        with (
            patch.object(captcha, "_extract_captcha_image_bytes", return_value=b"png"),
            patch.object(
                captcha,
                "_ai_recognize_text_captcha",
                side_effect=httpx.ReadTimeout("60 seconds"),
            ),
            patch.object(captcha, "_refresh_text_captcha") as refresh,
            patch.object(captcha, "_captcha_diagnostic") as diagnostic,
        ):
            result = captcha.try_auto_solve_text_captcha(page)

        self.assertFalse(result)
        refresh.assert_called_once_with(page, captcha_box)
        target_input.fill.assert_not_called()
        self.assertIn("超过 60 秒", diagnostic.call_args.args[0])

    def test_failed_platform_verification_refreshes_challenge(self) -> None:
        page, captcha_box, _image, target_input = self._page_with_text_captcha()
        target_input.input_value.return_value = "EY3xz"
        page.wait_for_selector.side_effect = captcha.PlaywrightTimeoutError("visible")

        with (
            patch.object(captcha, "_extract_captcha_image_bytes", return_value=b"png"),
            patch.object(captcha, "_ai_recognize_text_captcha", return_value="EY3xz"),
            patch.object(captcha, "human_type"),
            patch.object(captcha, "jitter_wait"),
            patch.object(captcha, "is_captcha_visible", return_value=True),
            patch.object(captcha, "_refresh_text_captcha") as refresh,
            patch.object(captcha, "_captcha_diagnostic"),
        ):
            result = captcha.try_auto_solve_text_captcha(page)

        self.assertFalse(result)
        refresh.assert_called_once_with(page, captcha_box)


if __name__ == "__main__":
    unittest.main()
