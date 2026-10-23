from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import main
import scheduler_core
from run_appeal_batch import (
    open_appealable_orders_table,
    page_reports_zero_appealable_orders,
)


class EmptyAppealQueueRulesTest(unittest.TestCase):
    def test_zero_count_is_detected_from_platform_summary(self) -> None:
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = (
            "可申诉次数\n不限\n可申诉订单数\n0 单\n近30天申诉成功数\n3 次"
        )

        self.assertTrue(page_reports_zero_appealable_orders(page))

    def test_zero_count_skips_missing_entry_as_normal_empty_queue(self) -> None:
        page = MagicMock()
        page.locator.return_value.inner_text.return_value = "可申诉订单数\n0 单"

        self.assertFalse(open_appealable_orders_table(page))

    def test_missing_entry_without_zero_count_still_raises(self) -> None:
        page = MagicMock()
        body = MagicMock()
        body.inner_text.return_value = "可申诉订单数\n3 单"
        link = MagicMock()
        link.filter.return_value = link
        link.wait_for.side_effect = PlaywrightTimeoutError("入口等待超时")
        page.locator.side_effect = lambda selector: body if selector == "body" else link

        with self.assertRaises(PlaywrightTimeoutError):
            open_appealable_orders_table(page)

    def test_phase1_returns_empty_queue_without_scraping_old_table(self) -> None:
        page = MagicMock()
        with (
            patch.object(main, "open_appeal_list_page"),
            patch.object(main, "open_appealable_orders_table", return_value=False),
            patch.object(main, "collect_all_appealable_order_numbers") as collect,
        ):
            result = main.run_phase1(page)

        self.assertEqual(result, ([], {}))
        collect.assert_not_called()

    def test_scheduler_displays_zero_queue_as_normal_completion(self) -> None:
        worker = MagicMock()
        worker.send.return_value = True
        worker.wait_response.return_value = {"type": "ready", "queue_size": 0}
        events: list[scheduler_core.SchedulerEvent] = []
        scheduler = scheduler_core.Scheduler(["店铺A"], on_event=events.append)

        with (
            patch.object(scheduler_core, "ShopWorker", return_value=worker),
            patch.object(scheduler_core.db, "mark_shop_daily_done") as mark_done,
            patch.object(scheduler_core.db, "mark_shop_daily_failed") as mark_failed,
        ):
            started = scheduler._start_worker("店铺A")

        self.assertFalse(started)
        self.assertNotIn("店铺A", scheduler.active)
        self.assertEqual(events[-1].type, "worker_empty_queue")
        worker.stop.assert_called_once_with()
        mark_done.assert_called_once_with("店铺A")
        mark_failed.assert_not_called()


if __name__ == "__main__":
    unittest.main()
