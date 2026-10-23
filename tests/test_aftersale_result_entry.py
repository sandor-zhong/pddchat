from __future__ import annotations

import io
import json
import unittest
from unittest.mock import MagicMock, patch

import main


class AftersaleResultEntryTest(unittest.TestCase):
    def test_result_query_does_not_open_chat_for_login(self) -> None:
        page = MagicMock()
        context = MagicMock()
        context.pages = [page]
        output = io.StringIO()
        result = {"updates": [], "checked_count": 0, "message": "平台售后申诉列表暂无记录。"}
        with (
            patch.object(main.check_aftersale_appeal_results, "prepare_result_query", return_value=([{"order_no": "target"}], "")),
            patch.object(main, "create_stealth_context", return_value=context),
            patch.object(main, "wait_for_login") as login,
            patch.object(main, "set_give_up_after_auto_solve_fails"),
            patch.object(main.check_aftersale_appeal_results, "check_aftersale_appeal_results", return_value=result) as check,
        ):
            main.run_check_aftersale_results_mode(MagicMock(), output, query_interval_seconds=9)
        login.assert_not_called()
        self.assertIs(check.call_args.args[0], page)
        self.assertEqual(check.call_args.kwargs["query_interval_seconds"], 9)
        context.close.assert_called_once()
        response = json.loads(output.getvalue())
        self.assertTrue(response["success"])
        self.assertEqual(response["message"], result["message"])


if __name__ == "__main__":
    unittest.main()
