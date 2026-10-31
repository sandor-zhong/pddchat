import json
import unittest
from unittest.mock import MagicMock, patch

import aftersale_appeal
import appeal_classifier
import appeal_wording
from appeal_style import evidence_style
import reconsideration


class AppealWordingTest(unittest.TestCase):
    def test_compact_chat_keeps_available_times_without_inventing_missing_times(self):
        data = {
            "order_no": "test",
            "messages": [
                {"sender": "买***", "is_system_message": False,
                 "content": "放假再联系", "image_urls": [], "time": "昨天 13:20"},
                {"sender": "客服", "is_system_message": False,
                 "content": "好的", "image_urls": []},
                {"sender": "系统", "is_system_message": True,
                 "content": "退款成功通知", "image_urls": [], "time": "2026-10-28 13:30"},
                {"sender": "客服", "is_system_message": False,
                 "content": "", "image_urls": [], "time": "2026-10-28 13:31"},
            ],
        }
        self.assertEqual(appeal_classifier.compact_chat_json(data), {
            "o": "test",
            "m": [["B", "放假再联系", "昨天 13:20"], ["S", "好的"],
                  ["SYS", "退款成功通知", "2026-10-28 13:30"]],
        })
        self.assertEqual(data["messages"][0]["time"], "昨天 13:20")

    def test_negative_and_reconsideration_share_style_and_fact_constraints(self):
        negative = appeal_classifier.APPEAL_TEXT_SYSTEM_PROMPT
        self.assertIn(evidence_style(50, 110), negative)
        self.assertIn(evidence_style(), appeal_wording.RECONSIDERATION_DESCRIPTION_PROMPT)
        self.assertNotIn("商品本身绝对没有质量问题", negative)
        self.assertIn("申诉凭证类型仅为分类参考，不是核验结论", negative)
        self.assertIn("不要称为复议", negative)
        self.assertIn("不超过120字", negative)

    def test_negative_uses_raw_chat_and_new_prompt(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = "申请结合记录复核本次负向体验判定。"
        chat = {"o": "test", "m": [["B", "[图片]"], ["S", "请说明接口型号"]]}
        with patch("appeal_classifier.get_client", return_value=client):
            text = appeal_classifier.generate_appeal_text(chat, "消费者误解商品信息")
        self.assertEqual(text, "申请结合记录复核本次负向体验判定。")
        args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(args["messages"][0]["content"], appeal_classifier.APPEAL_TEXT_SYSTEM_PROMPT)
        payload = json.loads(args["messages"][1]["content"])
        self.assertEqual(payload["chat"], chat)
        self.assertEqual(payload["申诉凭证类型"], "消费者误解商品信息")
        self.assertEqual(args["temperature"], 0.65)

    def test_negative_no_chat_templates_stay_evidence_limited(self):
        with patch("appeal_classifier.get_client") as client:
            label, text = appeal_classifier.analyze_chat_text("", "test")
        client.assert_not_called()
        self.assertEqual(label, appeal_classifier.NO_CHAT_RECORD_LABEL)
        self.assertEqual(text, appeal_classifier.get_no_chat_appeal_text("test"))
        for text in appeal_classifier.NO_CHAT_RECORD_APPEAL_TEXTS:
            self.assertNotIn("本次查询", text)
            self.assertIn("商家", text)
            self.assertNotIn("复核", text)
            self.assertNotIn("未收到", text)
            self.assertNotIn("没有相关图片", text)
            self.assertLessEqual(len(text), 120)

    def test_negative_keeps_existing_length_limit(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = "字" * 150
        with patch("appeal_classifier.get_client", return_value=client):
            text = appeal_classifier.generate_appeal_text({"m": [["B", "你好"]]}, "其他")
        self.assertLessEqual(len(text), 120)
        self.assertTrue(text.endswith("。"))

    def test_no_chat_reconsideration_does_not_invent_absent_complaints(self):
        with patch("appeal_wording.get_client") as client:
            text = appeal_wording.generate_reconsideration_description(
                {"o": "test", "m": []}, "其他"
            )
        client.assert_not_called()
        self.assertNotIn("未获取到", text)
        self.assertIn("处罚", text)
        self.assertNotIn("消费者未反馈", text)

    def test_no_chat_aftersale_has_no_model_call_or_unconditional_verdict(self):
        with patch("aftersale_appeal.get_client") as client:
            for chat in (None, {"m": []}):
                for item in ("货款申诉", "运费申诉", "货款申诉、运费申诉"):
                    text = aftersale_appeal.generate_aftersale_description(
                        chat, "消费者未表达商品问题", item, "非以上申诉原因"
                    )
                    self.assertNotIn("未获取到", text)
                    self.assertNotIn("商品无质量问题", text)
                    self.assertNotIn("属于消费者无理由退货", text)
        client.assert_not_called()

    def test_reconsideration_uses_raw_chat_with_its_own_prompt(self):
        client = MagicMock()
        client.chat.completions.create.return_value.choices[0].message.content = "申请复核本次处罚。"
        chat = {"o": "test", "m": [["B", "[图片]"], ["S", "请说明接口型号"]]}
        with patch("appeal_wording.get_client", return_value=client):
            text = appeal_wording.generate_reconsideration_description(chat, "其他")
        self.assertEqual(text, "申请复核本次处罚。")
        args = client.chat.completions.create.call_args.kwargs
        self.assertEqual(args["messages"][0]["content"], appeal_wording.RECONSIDERATION_DESCRIPTION_PROMPT)
        self.assertEqual(json.loads(args["messages"][1]["content"])["chat"], chat)

    def test_bad_outputs_are_rejected_instead_of_silently_truncated(self):
        for module, limit in ((appeal_wording, 200), (aftersale_appeal, 300)):
            for output in (None, " ", "字" * (limit + 1)):
                with self.subTest(module=module.__name__, length=len(output or "")):
                    client = MagicMock()
                    client.chat.completions.create.return_value.choices[0].message.content = output
                    with patch.object(module, "get_client", return_value=client):
                        with self.assertRaises(RuntimeError):
                            if module is appeal_wording:
                                module.generate_reconsideration_description({"m": [["B", "你好"]]}, "其他")
                            else:
                                module.generate_aftersale_description({"m": [["B", "你好"]]}, "未知", "货款申诉", "非以上申诉原因")

    def test_existing_reconsideration_material_gets_new_text_without_overwriting_original(self):
        order = {
            "classification": "消费者误解商品信息",
            "appeal_text": "旧文案不能作为事实输入",
            "messages": [{"sender": "买***", "is_system_message": False, "content": "接口怎么选择", "image_urls": []}],
        }
        task = {"order_no": "test", "record_key": "key"}
        evidence = reconsideration.BASE_DIR / "evidence" / "test.png"
        evidence_second = reconsideration.BASE_DIR / "evidence" / "test_second.png"
        with (
            patch("reconsideration.db.get_order", return_value=order),
            patch("reconsideration.collect_evidence_files", return_value=[evidence, evidence_second]),
            patch("reconsideration.process_single_order") as collect,
            patch("reconsideration.generate_reconsideration_description", return_value="新的复议描述") as generate,
            patch("reconsideration.db.update_reconsideration_status") as save,
        ):
            reason, text, files = reconsideration.prepare_reconsideration_material(MagicMock(), "shop", task)
        collect.assert_not_called()
        self.assertEqual(text, "新的复议描述")
        self.assertEqual(files, [evidence, evidence_second])
        self.assertNotIn(order["appeal_text"], str(generate.call_args))
        self.assertEqual(save.call_args.kwargs["reconsider_text"], text)
        self.assertEqual(order["appeal_text"], "旧文案不能作为事实输入")


if __name__ == "__main__":
    unittest.main()
