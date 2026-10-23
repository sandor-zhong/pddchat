from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call, patch

import check_aftersale_appeal_results as checker
import db
from check_aftersale_appeal_results import (
    PLATFORM_STATUSES,
    extract_aftersale_result_rows,
    match_aftersale_result_record,
    parse_aftersale_return_amount,
    read_partial_aftersale_return_amount,
)


def _partial_row(**overrides: str) -> dict[str, str]:
    return {
        "order_no": "260810-490670733630708",
        "appeal_type": "维权申诉",
        "platform_appealed_at": "2026-08-22 13:02",
        "appeal_item": "货款申诉",
        "appeal_amount": "13.89",
        "platform_status": "部分通过",
        **overrides,
    }


class AftersaleResultParsingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("login.is_login_page", return_value=False))

    def test_reads_return_amount_and_ignores_other_amounts(self) -> None:
        text = (
            "部分通过\n申诉金额13.89元\n返还金额\n11.80\n元已打款成功，"
            "可至「账户资金」查看到账情况。\n打款成功\n"
            "经核实，仅支持部分申诉金额返还，返还金额为：11.80元。"
        )
        self.assertEqual(parse_aftersale_return_amount(text), "11.80")

    def test_return_amount_formats_and_zero(self) -> None:
        for text, expected in (
            ("返还金额为：11.8元", "11.80"),
            ("返还金额 ￥1,234.50 元", "1234.50"),
            ("返还金额0.00元", "0.00"),
        ):
            with self.subTest(text=text):
                self.assertEqual(parse_aftersale_return_amount(text), expected)

    def test_missing_invalid_or_conflicting_return_amount_is_rejected(self) -> None:
        for text in (
            "部分通过 申诉金额13.89元",
            "返还金额待确认",
            "返还金额为：11.800元",
            "返还金额-11.80元",
            "返还金额11.80元 返还金额为：10.00元",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_aftersale_return_amount(text)

    def test_extracts_columns_by_header_name(self) -> None:
        page = MagicMock()
        table = MagicMock()
        page.locator.return_value.first = table
        table.evaluate.return_value = {
            "headers": [
                "订单编号", "申诉类型", "申诉时间", "申诉项",
                "申诉金额(元)", "审核状态", "操作",
            ],
            "rows": [[
                "订单 260728-128104537632552", "维权申诉",
                "2026-08-29 11:05", "运费申诉", "5.27", "部分通过", "详情",
            ]],
        }
        rows = extract_aftersale_result_rows(page)
        self.assertEqual(rows[0]["order_no"], "260728-128104537632552")
        self.assertEqual(rows[0]["appeal_amount"], "5.27")
        self.assertEqual(rows[0]["platform_status"], "部分通过")

    def test_extracts_only_standard_result_from_status_cell_details(self) -> None:
        page = MagicMock()
        table = MagicMock()
        page.locator.return_value.first = table
        table.evaluate.return_value = {
            "headers": [
                "订单编号", "申诉类型", "申诉时间", "申诉项",
                "申诉金额(元)", "审核状态",
            ],
            "rows": [
                ["260001-1", "维权申诉", "2026-08-29 11:05", "货款申诉", "5.00",
                 "申诉成功\n申诉优赔特权"],
                ["260001-2", "维权申诉", "2026-08-29 11:06", "货款申诉", "6.00",
                 "申诉失败\n商家商品或服务存在部分问题"],
                ["260001-3", "维权申诉", "2026-08-29 11:07", "货款申诉", "7.00",
                 "部分通过\n订单存在与商品描述不符等问题"],
            ],
        }

        rows = extract_aftersale_result_rows(page)

        self.assertEqual(
            [row["platform_status"] for row in rows],
            ["申诉成功", "申诉失败", "部分通过"],
        )

    def test_all_six_platform_statuses_are_registered(self) -> None:
        self.assertEqual(
            PLATFORM_STATUSES,
            {"待处理", "审核中", "申诉成功", "申诉失败", "已撤回", "部分通过"},
        )

    def test_duplicate_order_matches_by_item_then_amount(self) -> None:
        records = [
            {
                "order_no": "260001-1", "record_key": "freight",
                "appeal_item": "运费申诉", "appeal_amount": "5.27",
            },
            {
                "order_no": "260001-1", "record_key": "goods",
                "appeal_item": "货款申诉", "appeal_amount": "12.00",
            },
        ]
        matched, reason = match_aftersale_result_record(
            {
                "order_no": "260001-1", "appeal_item": "货款申诉",
                "appeal_amount": "12.00", "platform_appealed_at": "2026-08-29 11:05",
            },
            records,
            set(),
        )
        self.assertIsNone(reason)
        self.assertEqual(matched["record_key"], "goods")

    def test_rechecked_partial_record_matches_time_before_replaced_amount(self) -> None:
        records = [
            {
                "order_no": "260810-490670733630708", "record_key": "current",
                "appeal_item": "货款申诉", "appeal_amount": "11.80",
                "platform_appealed_at": "2026-08-22 13:02",
            },
            {
                "order_no": "260810-490670733630708", "record_key": "old",
                "appeal_item": "货款申诉", "appeal_amount": "13.89",
                "platform_appealed_at": "2026-08-20 10:00",
            },
        ]
        record, reason = match_aftersale_result_record(_partial_row(), records, set())
        self.assertIsNone(reason)
        self.assertEqual(record["record_key"], "current")


class AftersaleResultPopupTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("login.is_login_page", return_value=False))
        self.page = MagicMock()
        self.row = MagicMock()
        self.row.filter.return_value = self.row
        self.row.count.return_value = 1
        self.row.get_by_text.return_value.count.return_value = 1
        self.page.locator.return_value.first.locator.return_value = self.row
        self.detail = self.page.expect_popup.return_value.__enter__.return_value.value
        self.card = MagicMock()
        self.card.filter.return_value = self.card
        self.detail.locator.return_value = self.card
        self.card.inner_text.return_value = "部分通过 返还金额11.80元已打款成功"

    def test_closes_only_opened_popup_after_reading(self) -> None:
        with patch.object(checker, "safe_click"), patch.object(checker, "clear_blockers"):
            amount = read_partial_aftersale_return_amount(self.page, _partial_row())
        self.assertEqual(amount, "11.80")
        self.detail.close.assert_called_once()
        self.page.close.assert_not_called()
        self.page.goto.assert_not_called()

    def test_popup_is_closed_if_load_or_parse_fails(self) -> None:
        for failure in ("load", "parse"):
            with self.subTest(failure=failure):
                self.detail.reset_mock()
                self.detail.wait_for_load_state.side_effect = (
                    RuntimeError("加载失败") if failure == "load" else None
                )
                self.card.inner_text.return_value = "部分通过 申诉金额13.89元"
                with (
                    patch.object(checker, "safe_click"),
                    patch.object(checker, "clear_blockers"),
                    self.assertRaises((RuntimeError, ValueError)),
                ):
                    read_partial_aftersale_return_amount(self.page, _partial_row())
                self.detail.close.assert_called_once()
                self.page.close.assert_not_called()

    def test_ambiguous_row_is_not_clicked(self) -> None:
        self.row.count.return_value = 2
        with patch.object(checker, "safe_click") as click, self.assertRaises(RuntimeError):
            read_partial_aftersale_return_amount(self.page, _partial_row())
        click.assert_not_called()
        self.page.expect_popup.assert_not_called()

    def test_popup_login_is_reported_and_popup_is_closed(self) -> None:
        with (
            patch.object(checker, "safe_click"),
            patch("login.is_login_page", return_value=True),
            self.assertRaises(checker.LoginRequiredError),
        ):
            read_partial_aftersale_return_amount(self.page, _partial_row())
        self.detail.close.assert_called_once()
        self.card.inner_text.assert_not_called()


class AftersaleResultDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.enterContext(patch("login.is_login_page", return_value=False))
        self._temp_dir = tempfile.TemporaryDirectory()
        self._old_path = db.DB_PATH
        self._old_connection = db._connection
        db.DB_PATH = Path(self._temp_dir.name) / "test.db"
        db._connection = None

    def tearDown(self) -> None:
        if db._connection is not None:
            db._connection.close()
        db._connection = self._old_connection
        db.DB_PATH = self._old_path
        self._temp_dir.cleanup()

    def test_result_page_amount_overwrites_submission_amount_and_writes_history(self) -> None:
        db.record_aftersale_appeal_discovered(
            "店铺A", "260001-1", "record-1", appeal_item="运费申诉"
        )
        db.update_aftersale_appeal_status(
            "店铺A", "260001-1", "record-1", "已提交", appeal_amount="5.00"
        )
        db.record_aftersale_appeal_result(
            "店铺A",
            "260001-1",
            "record-1",
            "部分通过",
            result_text="部分通过",
            platform_appealed_at="2026-08-29 11:05",
            appeal_item="运费申诉",
            appeal_amount="5.27",
        )
        record = db.list_aftersale_appeal_records("店铺A")[0]
        self.assertEqual(record["status"], "部分通过")
        self.assertEqual(record["appeal_amount"], "5.27")
        self.assertTrue(record["result_checked_at"])
        history = db.list_aftersale_appeal_review_history(
            "店铺A", "260001-1", "record-1"
        )
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0]["appeal_amount"], "5.27")
        self.assertEqual(history[0]["status"], "部分通过")

    def _seed(self, row: dict[str, str], status: str = "已提交") -> None:
        db.record_aftersale_appeal_discovered(
            "店铺A", row["order_no"], "record-1", appeal_item=row["appeal_item"]
        )
        db.update_aftersale_appeal_status(
            "店铺A", row["order_no"], "record-1", status,
            appeal_amount=row["appeal_amount"],
        )

    def _check(self, rows: list[dict[str, str]], amount: object = "11.80", interval: int = 3) -> dict:
        self.query_page = MagicMock()
        self.query_page.locator.return_value.count.return_value = 0
        with (
            patch.object(checker, "clear_blockers"),
            patch.object(checker, "extract_aftersale_result_rows", return_value=rows),
            patch.object(checker, "read_partial_aftersale_return_amount") as read,
        ):
            if isinstance(amount, Exception):
                read.side_effect = amount
            else:
                read.return_value = amount
            result = checker.check_aftersale_appeal_results(
                self.query_page, "店铺A", query_interval_seconds=interval
            )
            return result

    def test_configured_interval_applies_to_details_but_not_plain_rows(self) -> None:
        rows = [
            _partial_row(platform_status="申诉成功"),
            _partial_row(order_no="260810-2"),
            _partial_row(order_no="260810-3"),
        ]
        for row in rows:
            self._seed(row)
        self._check(rows, interval=7)
        self.assertEqual(self.query_page.wait_for_timeout.call_args_list, [call(7000), call(7000)])

    def test_empty_local_database_stops_without_querying_platform(self) -> None:
        result = self._check([_partial_row()])
        self.query_page.goto.assert_not_called()
        self.assertEqual(db.list_aftersale_appeal_records("店铺A"), [])
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["checked_count"], 0)
        self.assertIn("本地没有", result["message"])
        self.assertTrue(result["skipped"])

    def test_terminal_local_record_is_rechecked_without_creating_duplicate(self) -> None:
        row = _partial_row()
        self._seed(row, status="申诉成功")
        result = self._check([row])
        records = db.list_aftersale_appeal_records("店铺A")
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["record_key"], "record-1")
        self.assertEqual(records[0]["status"], "部分通过")
        self.assertEqual(result["imported_count"], 0)

    def test_multiple_appeals_for_same_order_stay_distinct_on_repeat_query(self) -> None:
        rows = [
            _partial_row(),
            _partial_row(
                platform_appealed_at="2026-08-20 10:00",
                appeal_amount="12.00",
                platform_status="申诉失败",
            ),
            _partial_row(appeal_item="运费申诉", appeal_amount="5.00", platform_status="申诉成功"),
        ]
        for index, row in enumerate(rows, start=1):
            db.record_aftersale_appeal_discovered(
                "店铺A", row["order_no"], f"record-{index}",
                appeal_item=row["appeal_item"],
            )
            db.update_aftersale_appeal_status(
                "店铺A", row["order_no"], f"record-{index}", "已提交",
                appeal_amount=row["appeal_amount"],
            )
        result = self._check(rows)
        self.assertEqual(result["matched_count"], 3)
        result = self._check(list(reversed(rows)))
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["matched_count"], 3)
        self.assertEqual(len(db.list_aftersale_appeal_records("店铺A")), 3)

    def test_platform_empty_message_is_based_on_actual_query(self) -> None:
        self._seed(_partial_row())
        result = self._check([])
        self.query_page.goto.assert_called_once()
        self.assertEqual(result["checked_count"], 0)
        self.assertIn("平台售后申诉列表暂无记录", result["message"])

    def test_platform_only_record_is_ignored_and_local_record_stays_unmatched(self) -> None:
        self._seed(_partial_row(order_no="260810-123"))
        result = self._check([_partial_row()])
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["ignored_platform_count"], 1)
        self.assertEqual(result["updates"], [])
        self.assertEqual(len(db.list_aftersale_appeal_records("店铺A")), 1)
        self.assertEqual(result["unmatched_local_count"], 1)

    def test_continues_pagination_after_all_local_records_are_matched(self) -> None:
        first = _partial_row(platform_status="申诉成功")
        second = _partial_row(order_no="260810-456", platform_status="申诉失败")
        self._seed(first)
        page = MagicMock()
        page.locator.return_value.count.side_effect = [1, 0]
        page.locator.return_value.first.get_attribute.return_value = ""
        progress = MagicMock()
        with (
            patch.object(checker, "clear_blockers"),
            patch.object(checker, "safe_click") as click,
            patch.object(checker, "jitter_wait"),
            patch.object(checker, "extract_aftersale_result_rows", side_effect=[[first], [second]]),
        ):
            result = checker.check_aftersale_appeal_results(page, "店铺A", on_progress=progress)
        self.assertEqual(result["checked_count"], 2)
        self.assertEqual(result["imported_count"], 0)
        self.assertEqual(result["ignored_platform_count"], 1)
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(len(result["updates"]), 1)
        self.assertEqual(progress.call_count, 2)
        click.assert_called_once()
        page.wait_for_timeout.assert_called_once_with(3000)

    def test_unloaded_result_page_is_an_error_with_local_record(self) -> None:
        self._seed(_partial_row())
        page = MagicMock()
        page.url = "https://mms.pinduoduo.com/login"
        with (
            patch.object(checker, "clear_blockers"),
            patch.object(checker, "wait_for_content_or_login", side_effect=checker.PlaywrightTimeoutError("timeout")),
            self.assertRaisesRegex(RuntimeError, "登录状态"),
        ):
            checker.check_aftersale_appeal_results(page, "店铺A")

    def test_visible_login_panel_fails_before_querying_or_writing_results(self) -> None:
        row = _partial_row()
        self._seed(row)
        page = MagicMock()
        with (
            patch("login.is_login_page", return_value=True),
            patch.object(checker, "extract_aftersale_result_rows") as extract,
            self.assertRaisesRegex(checker.LoginRequiredError, "重新登录"),
        ):
            checker.check_aftersale_appeal_results(page, "店铺A")
        extract.assert_not_called()
        record = db.list_aftersale_appeal_records("店铺A")[0]
        self.assertEqual(record["status"], "已提交")
        self.assertEqual(db.list_aftersale_appeal_review_history("店铺A", row["order_no"]), [])

    def test_detail_login_does_not_clear_existing_amount(self) -> None:
        row = _partial_row()
        self._seed(row, status="部分通过")
        db.update_aftersale_appeal_status("店铺A", row["order_no"], "record-1", "部分通过", appeal_amount="11.80")
        with self.assertRaises(checker.LoginRequiredError):
            self._check([row], checker.LoginRequiredError("需要重新登录"))
        record = db.list_aftersale_appeal_records("店铺A")[0]
        self.assertEqual(record["appeal_amount"], "11.80")
        self.assertEqual(db.list_aftersale_appeal_review_history("店铺A", row["order_no"]), [])

    def test_partial_query_writes_actual_return_to_current_record_and_history(self) -> None:
        row = _partial_row()
        self._seed(row)
        result = self._check([row])
        record = db.list_aftersale_appeal_records("店铺A")[0]
        history = db.list_aftersale_appeal_review_history("店铺A", row["order_no"])
        self.assertEqual(record["status"], "部分通过")
        self.assertEqual(record["appeal_amount"], "11.80")
        self.assertEqual(history[0]["appeal_amount"], "11.80")
        self.assertEqual(result["updates"][0]["appeal_amount"], "11.80")

    def test_existing_partial_records_are_corrected_on_next_query(self) -> None:
        row = _partial_row()
        self._seed(row, status="部分通过")
        self._check([row])
        self.assertEqual(db.list_aftersale_appeal_records("店铺A")[0]["appeal_amount"], "11.80")

    def test_detail_failure_keeps_status_clears_wrong_amount_continues_and_retries(self) -> None:
        row = _partial_row()
        success_row = _partial_row(order_no="260814-385687232003053", platform_status="申诉成功")
        self._seed(row)
        self._seed(success_row)
        result = self._check([row, success_row], RuntimeError("详情加载超时"))
        records = {r["order_no"]: r for r in db.list_aftersale_appeal_records("店铺A")}
        record = records[row["order_no"]]
        self.assertEqual(record["status"], "部分通过")
        self.assertIsNone(record["appeal_amount"])
        self.assertIn("详情加载超时", record["error_message"])
        self.assertEqual(result["matched_count"], 2)
        self.assertEqual(records[success_row["order_no"]]["appeal_amount"], "13.89")
        history = db.list_aftersale_appeal_review_history("店铺A", row["order_no"])
        self.assertIsNone(history[0]["appeal_amount"])

        self._check([row])
        records = {r["order_no"]: r for r in db.list_aftersale_appeal_records("店铺A")}
        self.assertEqual(records[row["order_no"]]["appeal_amount"], "11.80")
        self.assertIsNone(records[row["order_no"]]["error_message"])

    def test_other_statuses_keep_list_amount_without_opening_details(self) -> None:
        rows = [
            _partial_row(order_no=f"260810-{index}", platform_status=status)
            for index, status in enumerate(sorted(PLATFORM_STATUSES - {"部分通过"}))
        ]
        for row in rows:
            self._seed(row)
        self._check(rows)
        self.assertTrue(all(
            record["appeal_amount"] == "13.89"
            for record in db.list_aftersale_appeal_records("店铺A")
        ))


if __name__ == "__main__":
    unittest.main()
