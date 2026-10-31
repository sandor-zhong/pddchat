import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

import appeal_style


def response(text):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))]
    )


class WordingDiversityTests(unittest.TestCase):
    def setUp(self):
        self.saved = list(appeal_style._recent_wording)
        self.sequence = appeal_style._wording_sequence
        appeal_style._recent_wording.clear()

    def tearDown(self):
        appeal_style._recent_wording.clear()
        appeal_style._recent_wording.extend(self.saved)
        appeal_style._wording_sequence = self.sequence

    def test_repeated_stance_retries_using_original_order_data(self):
        appeal_style._recent_wording.append("接口还没确认，不能直接认定是我们的过错。")
        create = MagicMock(
            side_effect=[
                response("充电原因待查，不能直接认定是我们的过错。"),
                response("退换通道一直开着，说我们拒绝处理与这段沟通不符。"),
            ]
        )
        messages = [
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "current order"},
        ]
        result = appeal_style.diverse_completion(
            create, max_chars=120, messages=messages
        )
        self.assertEqual(create.call_count, 2)
        self.assertIn("退换通道", result.choices[0].message.content)
        self.assertEqual(create.call_args.kwargs["messages"][:2], messages)
        self.assertEqual(len(messages), 2)
        self.assertIn(
            "绝不是本订单事实", create.call_args.kwargs["messages"][2]["content"]
        )

    def test_product_words_alone_do_not_trigger_rewrites(self):
        appeal_style._recent_wording.append(
            "买家反馈数据线充电速度慢，我们给出了寄回换新的办法。"
        )
        create = MagicMock(
            return_value=response("买家反馈数据线充电速度慢，接口适配情况仍需确认。")
        )
        appeal_style.diverse_completion(create, max_chars=120, messages=[])
        create.assert_called_once()

    def test_status_opening_is_allowed_when_relevant(self):
        appeal_style._recent_wording.extend(
            ["这单已提出寄回换新。", "这单已答应退货。", "这单还在等待接口照片。"]
        )
        create = MagicMock(
            return_value=response(
                "这单退款已经完成。之前双方谈好退一半，记录有到账通知，请审核。"
            )
        )
        appeal_style.diverse_completion(create, max_chars=120, messages=[])
        create.assert_called_once()
        self.assertIn(
            "立场落到具体责任或费用上", create.call_args.kwargs["messages"][-1]["content"]
        )

    def test_retry_budget_is_bounded(self):
        text = "不能直接认定是我们的过错。"
        appeal_style._recent_wording.append(text)
        create = MagicMock(return_value=response(text))
        appeal_style.diverse_completion(create, max_chars=120, messages=[])
        self.assertEqual(create.call_count, 3)

    def test_overused_opening_is_rewritten_even_with_a_different_stance(self):
        appeal_style._recent_wording.extend(
            ["买家说接口不符。", "买家要求退货。", "买家问怎么使用。"]
        )
        create = MagicMock(
            side_effect=[
                response("买家提出的邮费争议已经答复，费用不该由我们承担。"),
                response("邮费争议已经答复，这笔费用不该由我们承担。"),
            ]
        )
        result = appeal_style.diverse_completion(create, max_chars=120, messages=[])
        self.assertEqual(create.call_count, 2)
        self.assertTrue(result.choices[0].message.content.startswith("邮费"))

    def test_alternative_generic_stance_is_also_detected(self):
        first = appeal_style.stance_fragments("直接按商品故障归到我们身上不合适。")
        second = appeal_style.stance_fragments(
            "这个结果直接按商品故障归到我们身上不合适。"
        )
        self.assertTrue(first & second)

    def test_short_stance_cannot_evade_long_fragment_detection(self):
        appeal_style._recent_wording.extend(
            ["这里的问题我们不认可。", "这种说法我们不认可。"]
        )
        create = MagicMock(
            side_effect=[
                response("发货责任还没有谈清，我们不认可。"),
                response("发货环节没有证据显示差错，不能把接口选择混为发错货。"),
            ]
        )
        result = appeal_style.diverse_completion(create, max_chars=120, messages=[])
        self.assertEqual(create.call_count, 2)
        self.assertNotIn("我们不认可", result.choices[0].message.content)

    def test_invalid_length_is_left_to_callers_existing_validation(self):
        create = MagicMock(return_value=response("字" * 201))
        appeal_style.diverse_completion(create, max_chars=200, messages=[])
        create.assert_called_once()
        self.assertEqual(len(appeal_style._recent_wording), 0)
