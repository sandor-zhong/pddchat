from __future__ import annotations

import unittest

import scheduler_core


class SchedulerProgressTest(unittest.TestCase):
    def test_format_duration_uses_compact_chinese_units(self) -> None:
        self.assertEqual(scheduler_core.format_duration(0), "0秒")
        self.assertEqual(scheduler_core.format_duration(65.4), "1分5秒")
        self.assertEqual(scheduler_core.format_duration(3661), "1小时1分1秒")

    def test_progress_accounts_for_unknown_active_and_terminal_shops(self) -> None:
        shops = ["店铺A", "店铺B", "店铺C"]
        progress = scheduler_core.overall_shop_progress(
            shops,
            terminal_shops={"店铺A"},
            initial_totals={"店铺B": 10},
            remaining_by_shop={"店铺B": 5},
        )

        # A 已完成占 1/3；B 完成一半但预留 2% 收尾；C 还未开始。
        self.assertAlmostEqual(progress, (1.0 + 0.5 * 0.98) / 3, places=6)

    def test_progress_reaches_one_only_after_all_shops_are_terminal(self) -> None:
        shops = ["店铺A", "店铺B"]
        before_close = scheduler_core.overall_shop_progress(
            shops,
            terminal_shops={"店铺A"},
            initial_totals={"店铺B": 1},
            remaining_by_shop={"店铺B": 0},
        )
        finished = scheduler_core.overall_shop_progress(
            shops,
            terminal_shops=set(shops),
            initial_totals={"店铺B": 1},
            remaining_by_shop={"店铺B": 0},
        )

        self.assertLess(before_close, 1.0)
        self.assertEqual(finished, 1.0)


if __name__ == "__main__":
    unittest.main()
