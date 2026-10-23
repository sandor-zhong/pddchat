import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import appeal_submit
import aftersale_appeal
import main
from chat_evidence import select_chat_evidence
from pdd_chat import build_chat_record_dict


def message(index, content, buyer=True, page=1):
    return {"page_number": page, "page_index": index, "sender": "买***" if buyer else "客服",
            "content": content, "is_system_message": False, "image_urls": []}


def order_with_captures(messages, *ranges):
    files = [Path(f"evidence/candidate_{i}.png") for i in range(len(ranges))]
    messages[0]["page_evidence_details"] = [
        {"path": str(path).replace("\\", "/"), "page": 1,
         "full_indices": list(indices), "includes_time": True}
        for path, indices in zip(files, ranges)
    ]
    return files, {"messages": messages}


class ChatEvidenceTests(unittest.TestCase):
    def test_selects_one_complete_exchange_over_partial_favorable_reply(self):
        files, order = order_with_captures([
            message(1, "接口不兼容，请退款"),
            message(2, "请检查型号", False),
            message(3, "我看错型号了，但仍有损坏"),
        ], [2, 3], [1, 2, 3])
        self.assertEqual(select_chat_evidence(files, order), [files[1]])

    def test_incomplete_single_continues_with_explicit_partial_log(self):
        files, order = order_with_captures([
            message(1, "有质量问题"), message(2, "已经解决", False),
            message(3, "没有解决，还是损坏的"),
        ], [1, 2])
        with patch("builtins.print") as log:
            self.assertEqual(select_chat_evidence(files, order), files)
        self.assertIn("上下文未完整覆盖", str(log.call_args))

    def test_keeps_multiple_messages_in_same_buyer_turn(self):
        files, order = order_with_captures([
            message(1, "我看错了"), message(2, "但商品仍有问题"),
            message(3, "请提供图片", False), message(4, "[图片]"),
        ], [1, 3, 4], [1, 2, 3, 4])
        self.assertEqual(select_chat_evidence(files, order), [files[1]])

    def test_neutral_dispute_is_valid_fallback_without_support_words(self):
        files, order = order_with_captures([
            message(1, "有问题"), message(2, "请具体说明", False), message(3, "破损"),
        ], [1, 2, 3])
        self.assertEqual(select_chat_evidence(files, order), files)

    def test_seller_only_original_is_not_labeled_complete_context(self):
        files, order = order_with_captures([message(1, "商品没有问题", False)], [1])
        with patch("builtins.print") as log:
            self.assertEqual(select_chat_evidence(files, order), files)
        self.assertIn("上下文未完整覆盖", str(log.call_args))

    def test_legacy_and_missing_time_metadata_continue_with_warning(self):
        files, order = order_with_captures([message(1, "咨询型号")], [1])
        order["messages"][0]["page_evidence_details"][0]["includes_time"] = False
        with patch("builtins.print") as log:
            self.assertEqual(select_chat_evidence(files, order), files)
        self.assertIn("无法确认完整上下文", str(log.call_args))
        self.assertEqual(select_chat_evidence(files, {"messages": [message(1, "咨询型号")]}), files)

    def test_missing_next_page_is_reported_as_partial_not_skipped(self):
        files, order = order_with_captures([
            message(1, "型号错误"), message(2, "请核对", False),
            message(1, "仍然有问题", page=2),
        ], [1, 2])
        with patch("builtins.print") as log:
            self.assertEqual(select_chat_evidence(files, order), files)
        self.assertIn("上下文未完整覆盖", str(log.call_args))

    def test_no_chat_uses_only_no_chat_original(self):
        files = [Path("other.png"), Path("no_chat_record.png")]
        self.assertEqual(select_chat_evidence(files, {"messages": []}), [files[1]])
        self.assertEqual(select_chat_evidence(files[:1], None), files[:1])

    def test_negative_wrapper_uses_shared_selector(self):
        files, order = order_with_captures([message(1, "咨询型号")], [1])
        self.assertEqual(appeal_submit._select_chat_evidence(files, order), files)

    def test_capture_metadata_survives_record_building(self):
        files, order = order_with_captures([message(1, "咨询型号")], [1])
        result = build_chat_record_dict("test", order["messages"])
        self.assertEqual(select_chat_evidence(files, result), files)

    def test_negative_cached_analysis_requires_usable_evidence(self):
        with patch("main.db.has_analysis", return_value=True), patch("main.collect_evidence_files", return_value=[]):
            self.assertFalse(main._order_has_analysis("test"))

    def test_aftersale_rejects_more_than_two_chat_images_before_opening_form(self):
        with patch("aftersale_appeal.open_aftersale_appeal_modal") as modal:
            with self.assertRaisesRegex(RuntimeError, "最多两张"):
                aftersale_appeal.fill_and_submit_aftersale_form(
                    MagicMock(), {}, Path("refund.png"), [Path("a.png"), Path("b.png"), Path("c.png")], "", None)
        modal.assert_not_called()

    def test_two_adjacent_images_cover_exchange(self):
        files, order = order_with_captures([
            message(1, "型号不对"), message(2, "请检查接口", False), message(3, "我看错了"),
        ], [1, 2], [2, 3])
        with patch("builtins.print") as log:
            self.assertEqual(select_chat_evidence(files, order), files)
        self.assertIn("两张共同覆盖完整上下文", str(log.call_args))

    def test_one_complete_image_preferred_over_pair(self):
        files, order = order_with_captures([
            message(1, "型号不对"), message(2, "请检查接口", False), message(3, "我看错了"),
        ], [1, 2], [2, 3], [1, 2, 3])
        self.assertEqual(select_chat_evidence(files, order), [files[2]])

    def test_more_than_two_needed_uses_best_adjacent_pair_and_continues(self):
        files, order = order_with_captures([
            message(1, "有问题"), message(2, "仍然损坏"),
            message(3, "请提供视频", False), message(4, "我上传了"),
            message(5, "[视频]"),
        ], [1], [2], [3, 4], [4, 5])
        with patch("builtins.print") as log:
            selected = select_chat_evidence(files, order)
        self.assertEqual(selected, files[1:3])
        self.assertIn("上下文未完整覆盖", str(log.call_args))

    def test_legacy_fallback_never_exceeds_two(self):
        files = [Path(f"evidence/{i}.png") for i in range(5)]
        self.assertEqual(select_chat_evidence(files, None), files[:2])

    def test_empty_input_remains_empty(self):
        self.assertEqual(select_chat_evidence([], None), [])


if __name__ == "__main__":
    unittest.main()
