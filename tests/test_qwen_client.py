from __future__ import annotations

import os
import unittest
from unittest.mock import MagicMock, patch

import qwen_client


class QwenClientTest(unittest.TestCase):
    def test_create_client_prefers_qwen_key_over_legacy_ark_key(self) -> None:
        with (
            patch.dict(
                os.environ,
                {"QWEN_API_KEY": "qwen-key", "ARK_API_KEY": "ark-key"},
            ),
            patch.object(qwen_client, "OpenAI") as openai,
        ):
            qwen_client.create_client()

        self.assertEqual(openai.call_args.kwargs["api_key"], "qwen-key")

    def test_recognize_data_uri_uses_same_qwen_request(self) -> None:
        client = MagicMock()
        choice = MagicMock()
        choice.message.content = "aYpM7"
        client.chat.completions.create.return_value.choices = [choice]

        result = qwen_client.recognize_captcha_data_uri(
            client,
            "data:image/png;base64,AA==",
            enable_thinking=False,
        )

        self.assertEqual(result, "aYpM7")
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["model"], qwen_client.MODEL)
        self.assertEqual(request["extra_body"], {"enable_thinking": False})
        image_part = request["messages"][0]["content"][0]
        self.assertEqual(
            image_part["image_url"]["url"],
            "data:image/png;base64,AA==",
        )

    def test_recognize_data_uri_rejects_non_image_value(self) -> None:
        with self.assertRaisesRegex(ValueError, "Data URI"):
            qwen_client.recognize_captcha_data_uri(
                MagicMock(), "https://example.com/captcha.png"
            )

    def test_geometry_locator_parses_and_validates_normalized_point(self) -> None:
        client = MagicMock()
        choice = MagicMock()
        choice.message.content = (
            '```json\n{"target_name":"蓝色圆柱",'
            '"target_bbox_2d":[200,300,400,600],'
            '"target_point_2d":[300,450],"confidence":0.91,'
            '"reasoning":"位于字母X正下方"}\n```'
        )
        client.chat.completions.create.return_value.choices = [choice]

        result = qwen_client.locate_geometry_captcha_data_uri(
            client,
            "data:image/png;base64,AA==",
            "请点击字母X正下方的物体",
        )

        self.assertEqual(result["target_point_2d"], [300.0, 450.0])
        self.assertEqual(result["confidence"], 0.91)
        request = client.chat.completions.create.call_args.kwargs
        self.assertEqual(request["extra_body"], {"enable_thinking": False})
        self.assertEqual(request["max_tokens"], 256)
        self.assertIn("请点击字母X正下方的物体", request["messages"][0]["content"][1]["text"])

    def test_geometry_locator_rejects_point_outside_bbox(self) -> None:
        client = MagicMock()
        choice = MagicMock()
        choice.message.content = (
            '{"target_bbox_2d":[200,300,400,600],'
            '"target_point_2d":[800,450],"confidence":0.9}'
        )
        client.chat.completions.create.return_value.choices = [choice]

        with self.assertRaisesRegex(ValueError, "坐标越界"):
            qwen_client.locate_geometry_captcha_data_uri(
                client,
                "data:image/png;base64,AA==",
                "点击目标",
            )


if __name__ == "__main__":
    unittest.main()
