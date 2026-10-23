from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import httpx

import captcha


class PictureCaptchaTest(unittest.TestCase):
    def setUp(self):
        recording = patch("captcha.captcha_diagnostics.save_attempt")
        recording.start()
        self.addCleanup(recording.stop)

    def _page_with_picture_captcha(self):
        page = MagicMock()
        captcha_box = MagicMock()
        question = MagicMock()
        image = MagicMock()

        visible_boxes = MagicMock()
        visible_boxes.count.return_value = 1
        visible_boxes.last = captcha_box
        page.locator.return_value = visible_boxes

        question_boxes = MagicMock()
        question_boxes.count.return_value = 1
        question_boxes.first = question
        image_boxes = MagicMock()
        image_boxes.count.return_value = 1
        image_boxes.first = image

        def locate(selector):
            if selector == captcha.PICTURE_CAPTCHA_QUESTION_SELECTOR:
                return question_boxes
            if selector == captcha.PICTURE_CAPTCHA_IMG_SELECTOR:
                return image_boxes
            raise AssertionError(f"unexpected selector: {selector}")

        captcha_box.locator.side_effect = locate
        question.inner_text.return_value = "请点击字母X正下方的物体"
        image.screenshot.return_value = b"png"
        image.bounding_box.return_value = {
            "x": 100.0,
            "y": 200.0,
            "width": 400.0,
            "height": 300.0,
        }
        return page, captcha_box, question, image

    def test_detection_uses_picture_selector(self) -> None:
        page = MagicMock()
        page.locator.return_value.count.return_value = 1

        self.assertTrue(captcha.is_picture_captcha(page))
        page.locator.assert_called_once_with(captcha.VISIBLE_PICTURE_CAPTCHA_SELECTOR)

    def test_model_point_is_mapped_to_current_image_box_and_clicked(self) -> None:
        page, _captcha_box, _question, image = self._page_with_picture_captcha()
        model_result = {
            "target_name": "圆柱",
            "target_bbox_2d": [200.0, 300.0, 400.0, 600.0],
            "target_point_2d": [300.0, 450.0],
            "confidence": 0.91,
            "reasoning": "在X下方",
        }

        with (
            patch.object(captcha, "_ai_locate_picture_captcha", return_value=model_result) as locate,
            patch.object(captcha, "_dump_picture_captcha_info"),
            patch.object(captcha, "_captcha_diagnostic"),
            patch.object(captcha, "jitter_wait"),
        ):
            result = captcha.try_auto_solve_picture_captcha(page)

        self.assertTrue(result)
        image.scroll_into_view_if_needed.assert_called_once_with()
        locate.assert_called_once_with(
            "data:image/png;base64,cG5n",
            "请点击字母X正下方的物体",
        )
        # x=100+0.3*400=220; y=200+0.45*300=335
        page.mouse.move.assert_called_once_with(220.0, 335.0)
        page.mouse.click.assert_called_once_with(220.0, 335.0)
        page.wait_for_selector.assert_called_once_with(
            captcha.VISIBLE_CAPTCHA_SELECTOR, state="hidden", timeout=5_000
        )

    def test_ai_timeout_refreshes_picture_before_retry(self) -> None:
        page, captcha_box, _question, _image = self._page_with_picture_captcha()

        with (
            patch.object(
                captcha,
                "_ai_locate_picture_captcha",
                side_effect=httpx.ReadTimeout("60 seconds"),
            ),
            patch.object(captcha, "_refresh_picture_captcha") as refresh,
            patch.object(captcha, "_captcha_diagnostic") as diagnostic,
        ):
            result = captcha.try_auto_solve_picture_captcha(page)

        self.assertFalse(result)
        refresh.assert_called_once_with(page, captcha_box)
        page.mouse.click.assert_not_called()
        self.assertIn("超过 60 秒", diagnostic.call_args_list[0].args[0])

    def test_low_confidence_is_not_clicked_and_is_refreshed(self) -> None:
        page, captcha_box, _question, _image = self._page_with_picture_captcha()
        model_result = {
            "target_name": "不确定",
            "target_bbox_2d": [200, 300, 400, 600],
            "target_point_2d": [300, 450],
            "confidence": 0.1,
            "reasoning": "",
        }

        with (
            patch.object(captcha, "_ai_locate_picture_captcha", return_value=model_result),
            patch.object(captcha, "_refresh_picture_captcha") as refresh,
            patch.object(captcha, "_captcha_diagnostic"),
        ):
            result = captcha.try_auto_solve_picture_captcha(page)

        self.assertFalse(result)
        refresh.assert_called_once_with(page, captcha_box)
        page.mouse.click.assert_not_called()

    def test_ai_locator_uses_qwen_client_with_60_second_timeout(self) -> None:
        client = MagicMock()
        expected = {"target_point_2d": [300, 450], "confidence": 0.9}
        with (
            patch.dict(os.environ, {"QWEN_API_KEY": "test-key"}),
            patch.object(captcha, "load_dotenv"),
            patch("qwen_client.create_client") as create_client,
            patch("qwen_client.locate_geometry_captcha_data_uri", return_value=expected) as locate,
        ):
            create_client.return_value.__enter__.return_value = client
            result = captcha._ai_locate_picture_captcha(
                "data:image/png;base64,AA==",
                "点击目标",
            )

        self.assertIs(result, expected)
        create_client.assert_called_once_with(api_key="test-key", timeout=60)
        locate.assert_called_once_with(
            client,
            "data:image/png;base64,AA==",
            "点击目标",
            enable_thinking=False,
        )

    def test_ensure_no_captcha_dispatches_picture_solver(self) -> None:
        page = MagicMock()
        with (
            patch.object(captcha, "is_captcha_visible", return_value=True),
            patch.object(captcha, "is_picture_captcha", return_value=True),
            patch.object(captcha, "try_auto_solve_picture_captcha", return_value=True) as solve,
            patch.object(captcha, "_captcha_diagnostic"),
        ):
            captcha.ensure_no_captcha(page)

        solve.assert_called_once_with(page)

    def test_picture_captcha_stops_after_five_attempts(self) -> None:
        page = MagicMock()
        with (
            patch.object(captcha, "is_captcha_visible", return_value=True),
            patch.object(captcha, "is_picture_captcha", return_value=True),
            patch.object(captcha, "try_auto_solve_picture_captcha", return_value=False) as solve,
            patch.object(captcha, "save_captcha_screenshot", return_value=None),
            patch.object(captcha, "wait_for_human_captcha", return_value=True) as human,
            patch.object(captcha, "_captcha_diagnostic"),
            patch.object(captcha, "jitter_wait"),
        ):
            with self.assertRaisesRegex(RuntimeError, "5 次均失败"):
                captcha.ensure_no_captcha(page)

        self.assertEqual(solve.call_count, captcha.CAPTCHA_AUTO_MAX_ATTEMPTS)
        human.assert_not_called()

    def test_rotate_captcha_refreshes_each_failed_attempt(self) -> None:
        page = MagicMock()
        with (
            patch.object(captcha, "is_captcha_visible", return_value=True),
            patch.object(captcha, "is_picture_captcha", return_value=False),
            patch.object(captcha, "is_text_captcha", return_value=False),
            patch.object(captcha, "is_rotate_captcha", return_value=True),
            patch.object(captcha, "try_auto_solve_rotate_captcha", return_value=False) as solve,
            patch.object(captcha, "_refresh_visible_captcha") as refresh,
            patch.object(captcha, "wait_for_human_captcha", return_value=True) as human,
            patch.object(captcha, "jitter_wait"),
        ):
            with self.assertRaisesRegex(RuntimeError, "5 次均失败"):
                captcha.ensure_no_captcha(page)

        self.assertEqual(solve.call_count, captcha.CAPTCHA_AUTO_MAX_ATTEMPTS)
        self.assertEqual(refresh.call_count, captcha.CAPTCHA_AUTO_MAX_ATTEMPTS)
        human.assert_not_called()


if __name__ == "__main__":
    unittest.main()
