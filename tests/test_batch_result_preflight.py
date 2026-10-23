import io
import json
import unittest
from unittest.mock import MagicMock, patch

import main
import check_reconsideration_results as reconsider
import check_aftersale_appeal_results as aftersale


class BatchResultPreflightTests(unittest.TestCase):
    CASES = (
        (reconsider, main.run_check_reconsideration_results_mode, "list_reconsideration_records", "check_reconsideration_results"),
        (aftersale, main.run_check_aftersale_results_mode, "list_aftersale_appeal_records", "check_aftersale_appeal_results"),
    )

    def run_mode(self, case, records, *, since=None, error=None):
        module, runner, db_function, query_function = case
        stream = io.StringIO()
        context = MagicMock()
        context.pages = [MagicMock()]
        context.pages[0].is_closed.return_value = True
        with patch.object(module.db, db_function, return_value=records), \
             patch.object(main, "create_stealth_context", return_value=context) as create, \
             patch.object(main, "wait_for_login") as login, \
             patch.object(main, "set_give_up_after_auto_solve_fails"), \
             patch.object(module, query_function, return_value={"updates": [], "complete": True}, side_effect=error) as query:
            runner(MagicMock(), stream, submitted_since=since)
        return json.loads(stream.getvalue()), create, login, query, context

    def test_empty_local_records_do_not_launch_browser(self):
        for case in self.CASES:
            with self.subTest(module=case[0].__name__):
                result, create, login, query, _ = self.run_mode(case, [])
                self.assertTrue(result["success"])
                self.assertTrue(result["skipped"])
                self.assertIn("本地没有", result["message"])
                create.assert_not_called()
                login.assert_not_called()
                query.assert_not_called()

    def test_time_filtered_targets_explain_cutoff_without_browser(self):
        for case in self.CASES:
            with self.subTest(module=case[0].__name__):
                records = [{"order_no": "old", "status": "已提交", "submitted_at": "2026-10-17T15:59:59+00:00"}]
                result, create, _, query, _ = self.run_mode(case, records, since="2026-10-18 00:00:00")
                self.assertTrue(result["skipped"])
                self.assertIn("2026-10-18 00:00:00", result["message"])
                self.assertIn("1 条", result["message"])
                create.assert_not_called()
                query.assert_not_called()

    def test_boundary_target_queries_business_page_without_chat(self):
        for case in self.CASES:
            with self.subTest(module=case[0].__name__):
                records = [{"order_no": "boundary", "status": "已提交", "submitted_at": "2026-10-17T16:00:00+00:00"}]
                result, create, login, query, context = self.run_mode(case, records, since="2026-10-18 00:00:00")
                self.assertTrue(result["success"])
                create.assert_called_once()
                login.assert_not_called()
                self.assertEqual(query.call_args.kwargs["target_orders"], records)
                context.close.assert_called_once()

    def test_navigation_errors_remain_failures(self):
        for case in self.CASES:
            with self.subTest(module=case[0].__name__):
                result, _, login, _, context = self.run_mode(
                    case, [{"order_no": "target", "status": "已提交"}], error=RuntimeError("net::ERR_ABORTED"),
                )
                self.assertFalse(result["success"])
                self.assertIn("net::ERR_ABORTED", result["message"])
                login.assert_not_called()
                context.close.assert_called_once()

    def test_aftersale_keeps_terminal_results_but_excludes_unsubmitted_rows(self):
        records = [
            {"order_no": "terminal", "status": "申诉成功"},
            {"order_no": "unsubmitted", "status": "结果待确认"},
        ]
        with patch.object(aftersale.db, "list_aftersale_appeal_records", return_value=records):
            targets, _ = aftersale.prepare_result_query("shop")
        self.assertEqual([row["order_no"] for row in targets], ["terminal"])

    def test_reconsider_login_failure_does_not_write_results(self):
        from login import LoginRequiredError
        page = MagicMock()
        with patch.object(reconsider, "raise_if_login_page", side_effect=LoginRequiredError("登录已失效")), \
             patch.object(reconsider.db, "record_reconsideration_review_result") as write:
            with self.assertRaises(LoginRequiredError):
                reconsider.check_reconsideration_results(page, "shop", target_orders=[{"order_no": "target"}])
        self.assertIn("negativeExperienceReconsiderRecord", page.goto.call_args.args[0])
        write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
