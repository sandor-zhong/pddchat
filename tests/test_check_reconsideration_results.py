from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
from check_reconsideration_results import (
    extract_reconsideration_rows_from_current_page,
    match_local_reconsideration_record,
    resolve_platform_result,
)


class ReconsiderationResultRulesTest(unittest.TestCase):
    def test_exact_platform_statuses_are_preserved(self) -> None:
        for status in ("待处理", "审核中", "申诉成功", "申诉失败"):
            resolved, raw, detail = resolve_platform_result(status)
            self.assertEqual(resolved, status)
            self.assertEqual(raw, status)
            self.assertIsNone(detail)

        resolved, raw, detail = resolve_platform_result(
            "申诉成功\n返还金额：3.00元"
        )
        self.assertEqual(resolved, "申诉成功")
        self.assertEqual(raw, "申诉成功")
        self.assertIsNone(detail)

    def test_unknown_platform_status_stays_pending_confirmation(self) -> None:
        resolved, raw, detail = resolve_platform_result("平台复核中")
        self.assertEqual(resolved, "复议结果待确认")
        self.assertEqual(raw, "平台复核中")
        self.assertIn("未知", detail)

    def test_table_columns_are_resolved_by_header_text(self) -> None:
        page = MagicMock()
        table = MagicMock()
        locator = MagicMock()
        locator.first = table
        page.locator.return_value = locator
        table.evaluate.return_value = {
            "headers": [
                "操作", "复议结果", "订单编号", "复议时间",
                "问题类型", "复议金额(元)", "商品信息",
            ],
            "rows": [[
                "查看详情", "申诉成功\n返还金额：3.00元",
                "260802-628526989683681", "2026-08-26 14:32:52",
                "商品问题", "3.00", "商品名称",
            ]],
        }

        with patch('check_reconsideration_results.raise_if_login_page'):
            rows = extract_reconsideration_rows_from_current_page(page)
        self.assertEqual(rows[0]["order_no"], "260802-628526989683681")
        self.assertEqual(rows[0]["refund_amount"], "3.00")
        self.assertEqual(rows[0]["result_cell_text"], "申诉成功\n返还金额：3.00元")

    @patch('check_reconsideration_results.raise_if_login_page')
    def test_waits_for_data_after_empty_table_shell(self, _login) -> None:
        page = MagicMock()
        payload = {
            'headers': ['订单编号', '问题类型', '复议金额(元)', '复议时间', '复议结果'],
            'rows': [['260802-1', '商品问题', '3', '2026-08-26 14:32:52', '申诉成功']],
        }
        with patch('check_reconsideration_results._read_reconsideration_table',
                   side_effect=[{'headers': [], 'rows': []}, {**payload, 'loading': True}, payload]):
            rows = extract_reconsideration_rows_from_current_page(page)
        self.assertEqual(rows[0]['order_no'], '260802-1')
        self.assertEqual(page.wait_for_timeout.call_count, 2)

    @patch('check_reconsideration_results.raise_if_login_page')
    def test_loading_does_not_count_as_empty_or_complete(self, _login) -> None:
        with patch('check_reconsideration_results._read_reconsideration_table',
                   return_value={'headers': [], 'rows': [], 'loading': True, 'empty': True}):
            with self.assertRaisesRegex(RuntimeError, '仍未就绪'):
                extract_reconsideration_rows_from_current_page(MagicMock(), timeout_ms=0)

    @patch('check_reconsideration_results.raise_if_login_page')
    def test_explicit_empty_state_is_accepted(self, _login) -> None:
        with patch('check_reconsideration_results._read_reconsideration_table',
                   return_value={'headers': [], 'rows': [], 'empty': True}):
            self.assertEqual(extract_reconsideration_rows_from_current_page(MagicMock()), [])

    @patch('check_reconsideration_results.raise_if_login_page')
    def test_previous_page_is_not_processed_again(self, _login) -> None:
        payload = {
            'headers': ['订单编号', '问题类型', '复议金额(元)', '复议时间', '复议结果'],
            'rows': [['260802-1', '商品问题', '3', '2026-08-26 14:32:52', '申诉成功']],
        }
        with patch('check_reconsideration_results._read_reconsideration_table', return_value=payload):
            old = extract_reconsideration_rows_from_current_page(MagicMock())
            with self.assertRaisesRegex(RuntimeError, '仍未就绪'):
                extract_reconsideration_rows_from_current_page(MagicMock(), previous_rows=old, timeout_ms=0)

    def test_duplicate_order_uses_reconsideration_time_to_pick_unique_record(self) -> None:
        row = {
            "order_no": "260802-1",
            "issue_type": "商品问题",
            "reconsider_amount": "3.00",
            "reconsidered_at": "2026-08-26 14:32:52",
        }
        candidates = [
            {
                "order_no": "260802-1", "record_key": "old",
                "issue_type": "商品问题", "penalty_amount": "3.00",
                "submitted_at": "2026-08-25T06:32:52+00:00",
            },
            {
                "order_no": "260802-1", "record_key": "current",
                "issue_type": "商品问题", "penalty_amount": "3.00",
                "submitted_at": "2026-08-26T06:32:53+00:00",
            },
        ]
        record, reason = match_local_reconsideration_record(row, candidates, set())
        self.assertIsNone(reason)
        self.assertEqual(record["record_key"], "current")


class ReconsiderationResultDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
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

    def test_result_updates_only_reconsideration_lifecycle_and_adds_history(self) -> None:
        db.record_collected("店铺A", "260802-1", issue_type="商品问题")
        db.record_order_review_status("店铺A", "260802-1", "申诉未通过")
        db.record_reconsideration_discovered(
            "店铺A",
            "260802-1",
            "record-1",
            issue_type="商品问题",
            penalty_amount="3.00",
            punished_at="2026-08-01 10:00:00",
        )
        db.update_reconsideration_status(
            "店铺A", "260802-1", "record-1", "已提交复议"
        )
        db.record_reconsideration_review_result(
            "店铺A",
            "260802-1",
            "record-1",
            "申诉成功",
            result_text="申诉成功",
            platform_reconsidered_at="2026-08-26 14:32:52",
            refund_amount="3.00",
        )

        order = db.get_order("店铺A", "260802-1")
        record = db.list_reconsideration_records("店铺A")[0]
        history = db.list_reconsideration_review_history(
            "店铺A", "260802-1", "record-1"
        )
        self.assertEqual(order["status"], "申诉未通过")
        self.assertEqual(record["status"], "申诉成功")
        self.assertEqual(record["refund_amount"], "3.00")
        self.assertEqual(record["platform_reconsidered_at"], "2026-08-26 14:32:52")
        self.assertEqual(history[0]["status"], "申诉成功")

    def test_old_reconsideration_table_is_migrated_on_startup(self) -> None:
        legacy = sqlite3.connect(db.DB_PATH)
        legacy.execute(
            """
            CREATE TABLE reconsideration_records (
                shop TEXT NOT NULL,
                order_no TEXT NOT NULL,
                record_key TEXT NOT NULL,
                status TEXT NOT NULL,
                deadline_at TEXT,
                PRIMARY KEY (shop, order_no, record_key)
            )
            """
        )
        legacy.commit()
        legacy.close()

        db.get_connection()
        columns = {
            row["name"]
            for row in db.get_connection().execute(
                "PRAGMA table_info(reconsideration_records)"
            ).fetchall()
        }
        self.assertTrue(
            {
                "result_text",
                "result_checked_at",
                "platform_reconsidered_at",
                "refund_amount",
            }.issubset(columns)
        )


if __name__ == "__main__":
    unittest.main()
