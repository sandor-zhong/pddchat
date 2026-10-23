from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import check_appeal_results
import db


class MissingAppealTicketFallbackTest(unittest.TestCase):
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

    def test_missing_ticket_stays_pending_without_order_number_search(self) -> None:
        shop = "店铺A"
        order_no = "260816-1"
        db.record_collected(shop, order_no, issue_type="商品问题")
        db.record_submit_result(shop, order_no, "已提交")
        local_order = db.get_order(shop, order_no)
        page = MagicMock()

        updates = check_appeal_results.query_missing_orders_by_ticket_sn(
            page,
            shop,
            {order_no: local_order},
            [order_no],
        )

        order = db.get_order(shop, order_no)
        self.assertEqual(updates[0]["record_key"], "ticketSn:缺失")
        self.assertEqual(order["status"], "结果待确认")
        self.assertIn("缺少 ticketSn", order["error_message"])
        page.context.new_page.assert_not_called()


if __name__ == "__main__":
    unittest.main()
