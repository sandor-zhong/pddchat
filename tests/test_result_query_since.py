import io
from itertools import islice
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

import result_query_settings as settings
import check_appeal_results as negative
import check_aftersale_appeal_results as aftersale
import check_reconsideration_results as reconsider
import main

SINCE = "2026-10-20 00:00:00"
SUBMITTED = "2026-10-19T16:00:00+00:00"


class QueryTimeSettingsTests(unittest.TestCase):
    def test_page_numbers_have_no_cap_with_or_without_time_filter(self):
        for since in (None, SINCE):
            self.assertEqual(list(islice(settings.page_numbers(1, since), 150)), list(range(1, 151)))

    def test_beijing_boundary_includes_equivalent_utc_timestamp(self):
        records = [
            {"id": "old", "submitted_at": "2026-10-19T15:59:59+00:00"},
            {"id": "boundary", "submitted_at": SUBMITTED},
            {"id": "new", "submitted_at": "2026-10-20 12:00:00"},
        ]
        self.assertEqual([r["id"] for r in settings.filter_records(records, SINCE, "submitted_at")], ["boundary", "new"])

    def test_unknown_submission_time_is_retained_and_reported(self):
        records = [{"submitted_at": "bad"}, {}]
        with redirect_stdout(io.StringIO()) as output:
            result = settings.filter_records(records, SINCE, "submitted_at")
        self.assertEqual(result, records)
        self.assertIn("2 条历史记录", output.getvalue())

    def test_platform_time_is_used_only_when_local_time_missing(self):
        records = [{"submitted_at": None, "platform_appealed_at": "2026-10-19 23:59"}]
        self.assertEqual(settings.filter_records(records, SINCE, "submitted_at", "platform_appealed_at"), [])

    def test_minute_precision_does_not_exclude_boundary_minute(self):
        self.assertFalse(settings.platform_before_since("2026-10-20 12:30", "2026-10-20 12:30:45"))
        self.assertTrue(settings.platform_before_since("2026-10-20 12:29", "2026-10-20 12:30:45"))

    def test_settings_persist_and_explicit_all_disables_cutoff(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, "SETTINGS_PATH", Path(folder) / "settings.json"):
            self.assertIsNone(settings.active_since())
            settings.save_settings(True, SINCE)
            self.assertEqual(settings.active_since(), "2026-10-20T00:00:00+08:00")
            settings.save_settings(False, SINCE)
            self.assertIsNone(settings.active_since())
        self.assertIsNone(settings.normalize_since("all"))
        with self.assertRaises(ValueError):
            settings.normalize_since("invalid")


def local(order="261020-1", key="new", submitted=SUBMITTED):
    return dict(order_no=order, record_key=key, status="已提交", submitted_at=submitted,
                appeal_item="货款申诉", appeal_amount="5.00", penalty_amount="5.00", issue_type="商品问题")


def platform(module, order="261020-1", time="2026-10-20 00:00:00"):
    if module is aftersale:
        return dict(order_no=order, appeal_type="维权申诉", platform_appealed_at=time[:16],
                    appeal_item="货款申诉", appeal_amount="5.00", platform_status="申诉成功")
    return dict(order_no=order, issue_type="商品问题", reconsider_amount="5.00",
                reconsidered_at=time, result_cell_text="申诉成功", refund_amount="5.00")


class ScopedPaginationTests(unittest.TestCase):
    def query(self, module, records, pages, *, since=SINCE, next_exists=True):
        page = MagicMock()
        button = page.locator.return_value
        button.first = button
        button.count.return_value = int(next_exists)
        button.get_attribute.return_value = ""
        with ExitStack() as stack:
            stack.enter_context(patch.object(module, "clear_blockers"))
            click = stack.enter_context(patch.object(module, "safe_click"))
            stack.enter_context(patch.object(module, "jitter_wait"))
            if module is aftersale:
                stack.enter_context(patch.object(module, "raise_if_login_page"))
                stack.enter_context(patch.object(module, "wait_for_content_or_login"))
                stack.enter_context(patch.object(module.db, "list_aftersale_appeal_records", return_value=records))
                write = stack.enter_context(patch.object(module.db, "record_aftersale_appeal_result"))
                extract = stack.enter_context(patch.object(module, "extract_aftersale_result_rows", side_effect=pages))
                result = module.check_aftersale_appeal_results(page, "shop", max_pages=1, submitted_since=since)
            else:
                stack.enter_context(patch.object(module, "raise_if_login_page"))
                stack.enter_context(patch.object(module, "wait_for_content_or_login"))
                stack.enter_context(patch.object(module, "_wait_for_page_changed"))
                stack.enter_context(patch.object(module.db, "list_reconsideration_records", return_value=records))
                write = stack.enter_context(patch.object(module.db, "record_reconsideration_review_result"))
                extract = stack.enter_context(patch.object(module, "extract_reconsideration_rows_from_current_page", side_effect=pages))
                result = module.check_reconsideration_results(page, "shop", max_pages=1, submitted_since=since)
        return result, page, write, extract, click

    def test_target_on_page_after_old_limit_is_found_without_scanning_further(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                pages = [[platform(module, order="unrelated")], [platform(module)]]
                result, _, write, extract, click = self.query(module, [local()], pages)
                self.assertTrue(result["complete"])
                self.assertEqual(result["unmatched_local_count"], 0)
                self.assertEqual(extract.call_count, 2)
                self.assertEqual(click.call_count, 1)
                write.assert_called_once()

    def test_old_record_for_same_order_cannot_consume_new_target(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                records = [local(key="old", submitted="2026-10-19 00:00:00"), local()]
                pages = [[platform(module, time="2026-10-19 00:00:00")], [platform(module)]]
                result, _, write, extract, _ = self.query(module, records, pages)
                self.assertEqual(result["matched_count"], 1)
                self.assertEqual(write.call_args.args[2], "new")
                self.assertEqual(extract.call_count, 2)

    def test_multiple_local_appeals_for_same_order_are_all_queried(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                records = [local(), local(key="second", submitted="2026-10-20 01:00:00")]
                result, _, write, extract, _ = self.query(module, records, [[platform(module)], [platform(module, time="2026-10-20 01:00:00")]])
                self.assertTrue(result["complete"])
                self.assertEqual(write.call_count, 2)
                self.assertEqual(extract.call_count, 2)

    def test_no_targets_in_range_does_not_navigate(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                result, page, write, _, _ = self.query(module, [local(submitted="2026-10-19 00:00:00")], [])
                page.goto.assert_not_called()
                write.assert_not_called()
                self.assertEqual(result["checked_count"], 0)

    def test_unmatched_targets_are_explicitly_reported(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                result, _, write, _, _ = self.query(module, [local()], [[platform(module, order="another")]], next_exists=False)
                self.assertFalse(result["complete"])
                self.assertEqual(result["unmatched_local_count"], 1)
                self.assertIn("未完全覆盖", result["message"])
                write.assert_not_called()

    def test_unmatched_targets_without_cutoff_are_also_incomplete(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                result, _, write, _, _ = self.query(module, [local()], [[platform(module, order="another")]], since=None, next_exists=False)
                self.assertFalse(result["complete"])
                self.assertIn("未匹配", result["message"])
                self.assertIn("未完全覆盖", result["message"])
                write.assert_not_called()

    def test_different_submission_time_is_not_treated_as_target(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                result, _, write, _, _ = self.query(module, [local()], [[platform(module, time="2026-10-20 06:00:00")]], next_exists=False)
                self.assertFalse(result["complete"])
                write.assert_not_called()

    def test_page_cycle_fails_instead_of_running_forever(self):
        for module in (aftersale, reconsider):
            with self.subTest(module=module.__name__):
                pages = [[platform(module, order="A")], [platform(module, order="B")], [platform(module, order="A")]]
                with self.assertRaisesRegex(RuntimeError, "重复页面"):
                    self.query(module, [local()], pages)


class NegativeScopedQueryTests(unittest.TestCase):
    def test_filtered_targets_use_ticket_details_without_scanning_pages(self):
        old = local(order="old", submitted="2026-10-19 00:00:00")
        new = dict(local(), ticket_sn="ticket-1")
        with patch.object(negative.db, "list_orders", return_value=[old, new]), \
             patch.object(negative, "query_missing_orders_by_ticket_sn", return_value=[{"order_no": new["order_no"], "result_text": "申诉通过"}]) as query, \
             patch.object(negative, "open_appeal_search_page") as scan:
            result = negative.check_appeal_results(MagicMock(), "shop", submitted_since=SINCE)
        self.assertEqual(query.call_args.args[3], [new["order_no"]])
        self.assertTrue(query.call_args.kwargs["direct"])
        self.assertEqual(len(result), 1)
        scan.assert_not_called()

    def test_no_ticket_scanning_is_not_cut_off_at_old_page_limit(self):
        page = MagicMock()
        rows = [dict(order_no="other", row_text="other-row", result_text="已申诉"),
                dict(order_no="261020-1", row_text="target-row", result_text="已申诉")]
        with ExitStack() as stack:
            stack.enter_context(patch.object(negative.db, "list_orders", return_value=[local()]))
            stack.enter_context(patch.object(negative.db, "record_order_review_status"))
            stack.enter_context(patch.object(negative, "query_missing_orders_by_ticket_sn", return_value=[]))
            for name in ("open_appeal_search_page", "raise_if_login_page", "safe_click", "clear_blockers", "jitter_wait", "_backfill_order_details_from_result_row"):
                stack.enter_context(patch.object(negative, name))
            stack.enter_context(patch.object(negative, "get_search_result_signature", return_value=""))
            stack.enter_context(patch.object(negative, "wait_for_search_result_updated", return_value=True))
            extract = stack.enter_context(patch.object(negative, "extract_appeal_result_rows_from_current_page", side_effect=[[rows[0]], [rows[1]]]))
            stack.enter_context(patch.object(negative, "click_next_page", side_effect=[{"clicked": True}, {"clicked": False}]))
            result = negative.check_appeal_results(page, "shop", max_pages=1, submitted_since=SINCE)
        self.assertEqual(extract.call_count, 2)
        self.assertEqual(result[0]["order_no"], "261020-1")


class EntrySinceTests(unittest.TestCase):
    def test_all_entry_modes_forward_the_selected_cutoff(self):
        cases = (
            (main.run_check_appeal_results_mode, negative, "check_appeal_results", [], {"since_days": 3}),
            (main.run_check_reconsideration_results_mode, reconsider, "check_reconsideration_results", {"updates": []}, {}),
            (main.run_check_aftersale_results_mode, aftersale, "check_aftersale_appeal_results", {"updates": []}, {}),
        )
        for runner, module, function_name, result, kwargs in cases:
            with self.subTest(mode=function_name), ExitStack() as stack:
                context = MagicMock()
                page = MagicMock()
                context.pages = [page]
                stack.enter_context(patch.object(main, "create_stealth_context", return_value=context))
                stack.enter_context(patch.object(main, "wait_for_login", return_value=page))
                stack.enter_context(patch.object(main, "set_give_up_after_auto_solve_fails"))
                stack.enter_context(patch.object(negative, "prepare_result_query", return_value=([local()], "")))
                stack.enter_context(patch.object(reconsider, "prepare_result_query", return_value=([local()], "")))
                stack.enter_context(patch.object(aftersale, "prepare_result_query", return_value=([local()], "")))
                check = stack.enter_context(patch.object(module, function_name, return_value=result))
                runner(MagicMock(), io.StringIO(), submitted_since=SINCE, **kwargs)
                self.assertEqual(check.call_args.kwargs["submitted_since"], SINCE)
                context.close.assert_called_once()

    def test_cli_explicit_all_overrides_saved_cutoff(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(settings, "SETTINGS_PATH", Path(folder) / "settings.json"):
            settings.save_settings(True, SINCE)
            with patch.object(main.sys, "argv", ["main.py", "--result-submitted-since", "all"]):
                self.assertIsNone(main.parse_args().result_submitted_since)
            with patch.object(main.sys, "argv", ["main.py"]):
                self.assertEqual(main.parse_args().result_submitted_since, "2026-10-20T00:00:00+08:00")


if __name__ == "__main__":
    unittest.main()
