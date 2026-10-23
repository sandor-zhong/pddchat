from __future__ import annotations

import io
import json
import queue
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import db
from appeal_submit import wait_for_submit_captcha
from main import run_reconsideration_serve_mode
from reconsideration import (
    ALLOWED_REASONS,
    ReconsiderationStopped,
    _close_reconsideration_success_modal,
    choose_reconsider_reason,
    collect_pending_reconsideration_tasks,
    countdown_to_deadline,
    make_record_key,
    open_reconsideration_page,
    run_reconsiderations,
    scan_all_reconsiderable_orders,
)


class ReconsiderationRulesTest(unittest.TestCase):
    def test_continue_reconsideration_uses_modal_safe_click_without_long_retries(
        self,
    ) -> None:
        page = MagicMock()
        success_modal = MagicMock()
        continue_button = MagicMock()
        continue_button.count.return_value = 1
        success_modal.get_by_role.return_value = continue_button

        with (
            patch("reconsideration.stealth_safe_click") as modal_click,
            patch("reconsideration.safe_click") as blocker_click,
        ):
            _close_reconsideration_success_modal(
                page,
                success_modal,
                "260001-1",
            )

        modal_click.assert_called_once_with(
            page,
            continue_button,
            "提交成功后继续复议",
            timeout=3_000,
            retries=1,
        )
        blocker_click.assert_not_called()
        success_modal.wait_for.assert_called_once_with(
            state="hidden",
            timeout=3_000,
        )

    def test_reconsideration_serve_protocol_processes_exactly_one_order_per_next(
        self,
    ) -> None:
        class CommandInput:
            def __init__(self) -> None:
                self.commands: "queue.Queue[str]" = queue.Queue()

            def __iter__(self):
                return self

            def __next__(self) -> str:
                return self.commands.get()

            def put(self, command: dict) -> None:
                self.commands.put(json.dumps(command, ensure_ascii=False) + "\n")

        command_input = CommandInput()
        command_input.put({"cmd": "init"})
        command_input.put({"cmd": "next"})
        output = io.StringIO()
        chat_page = MagicMock()
        reconsideration_page = MagicMock()
        context = MagicMock()
        context.pages = [chat_page]
        context.new_page.return_value = reconsideration_page

        def process_one(*_args, **_kwargs):
            command_input.put({"cmd": "stop"})
            return {"order_no": "260001-1", "result": "已提交复议"}

        with (
            patch("main.sys.stdin", command_input),
            patch("main.create_stealth_context", return_value=context),
            patch("main.wait_for_login", return_value=chat_page),
            patch("main.set_give_up_after_auto_solve_fails"),
            patch(
                "main.reconsideration.collect_pending_reconsideration_tasks",
                return_value=[{"order_no": "260001-1", "record_key": "record-1"}],
            ),
            patch(
                "main.reconsideration.process_reconsideration_task",
                side_effect=process_one,
            ) as process,
        ):
            run_reconsideration_serve_mode(
                MagicMock(),
                output,
                confirm=True,
                max_orders=20,
            )

        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [message["type"] for message in messages],
            ["reconsider_ready", "activity", "reconsider_order_result"],
        )
        self.assertEqual(process.call_count, 1)
        self.assertEqual(messages[1]["order_no"], "260001-1")
        self.assertEqual(messages[2]["remaining"], 0)

    def test_run_limits_each_shop_to_configured_reconsideration_count(self) -> None:
        reconsideration_page = MagicMock()
        chat_page = MagicMock()
        scanned = [
            {
                "order_no": f"260001-{index}",
                "record_key": f"record-{index}",
                "deadline_at": f"2026-08-{index:02d}T10:00:00+08:00",
            }
            for index in (3, 1, 2)
        ]
        with (
            patch(
                "reconsideration.scan_all_reconsiderable_orders",
                return_value=scanned,
            ),
            patch("reconsideration.db.list_reconsideration_records", return_value=[]),
            patch(
                "reconsideration.prepare_reconsideration_material",
                return_value=("其他", "复议文案", [Path("evidence.png")]),
            ),
            patch(
                "reconsideration.submit_reconsideration_task",
                return_value="已提交复议",
            ) as submit,
            patch("reconsideration.human_delay"),
        ):
            outcomes, stopped = run_reconsiderations(
                reconsideration_page,
                chat_page,
                "店铺A",
                confirm=True,
                max_orders=2,
            )

        self.assertFalse(stopped)
        self.assertEqual(
            [item["order_no"] for item in outcomes],
            ["260001-1", "260001-2"],
        )
        self.assertEqual(submit.call_count, 2)

    def test_scan_honors_stop_before_opening_page(self) -> None:
        with self.assertRaises(ReconsiderationStopped):
            scan_all_reconsiderable_orders(
                object(),
                "店铺A",
                should_stop=lambda: True,
            )

    def test_scan_reads_automatically_loaded_rows_without_clicking(self) -> None:
        page = MagicMock()
        page.get_by_text.return_value.count.return_value = 0
        item = {"order_no": "260001-1", "record_key": "处罚|问题"}
        with (
            patch("reconsideration.open_reconsideration_page"),
            patch("reconsideration.extract_reconsideration_rows",
                  side_effect=[[], [item]]),
            patch("reconsideration.db.record_reconsideration_discovered"),
            patch("reconsideration.clear_blockers"),
            patch("reconsideration._scroll_reconsideration_container") as scroll,
            patch("reconsideration.jitter_wait"),
            patch("reconsideration.safe_click") as click,
        ):
            result = scan_all_reconsiderable_orders(
                page, "店铺A", max_rounds=2, early_stop_count=1,
            )
        self.assertEqual(result, [item])
        scroll.assert_called_once_with(page)
        click.assert_not_called()
        page.get_by_text.assert_called_once_with("没有更多了", exact=True)

    def test_open_reconsideration_page_reuses_matching_tab(self) -> None:
        page = MagicMock()
        page.url = (
            "https://mms.pinduoduo.com/orders/appeals/"
            "negativeExperienceReconsider?reconsiderSecondaryTabKey=negativeExperience"
        )
        with patch("reconsideration.clear_blockers"):
            open_reconsideration_page(page)
        page.goto.assert_not_called()
        page.locator.assert_called_once_with('[data-testid="beast-core-table"]')

    def test_submit_captcha_reuses_existing_solver_entrypoint(self) -> None:
        page = object()
        with (
            patch("appeal_submit.is_captcha_visible", return_value=True),
            patch("appeal_submit.ensure_no_captcha") as ensure_no_captcha,
        ):
            wait_for_submit_captcha(page, appear_wait_sec=1)
        ensure_no_captcha.assert_called_once_with(page)

    def test_submit_success_short_circuits_captcha_wait(self) -> None:
        page = object()
        success = MagicMock()
        success.count.return_value = 1
        success.is_visible.return_value = True
        with (
            patch("appeal_submit.is_captcha_visible") as is_captcha_visible,
            patch("appeal_submit.ensure_no_captcha") as ensure_no_captcha,
        ):
            result = wait_for_submit_captcha(
                page,
                appear_wait_sec=10,
                success_locator=success,
            )
        self.assertTrue(result)
        is_captcha_visible.assert_not_called()
        ensure_no_captcha.assert_not_called()

    def test_reason_mapping_never_uses_excluded_goods_problem_options(self) -> None:
        self.assertEqual(
            choose_reconsider_reason("消费者误解商品信息"),
            "商品无问题，消费者误解",
        )
        self.assertEqual(
            choose_reconsider_reason("消费者未表达商品问题"),
            "消费者未表达商品问题（含售后详情内未描述问题等）",
        )
        self.assertEqual(
            choose_reconsider_reason("商品有问题，但消费者主动表示无需处理"),
            "其他",
        )
        self.assertIn(choose_reconsider_reason("未知分类"), ALLOWED_REASONS)

    def test_countdown_is_converted_to_deadline(self) -> None:
        observed = datetime(2026, 8, 26, 10, 0, 0, tzinfo=UTC)
        self.assertEqual(
            countdown_to_deadline("01天07时30分05秒后不可复议", observed),
            "2026-08-27T17:30:05+00:00",
        )
        self.assertEqual(countdown_to_deadline("无法识别", observed), "")

    def test_record_key_separates_multiple_penalties_of_one_order(self) -> None:
        first = make_record_key("260001-1", "2026-08-01 10:00:00", "商品问题")
        second = make_record_key("260001-1", "2026-08-02 10:00:00", "商品问题")
        self.assertNotEqual(first, second)

    def test_result_statuses_never_reenter_reconsideration_queue(self) -> None:
        scanned = [
            {
                "order_no": f"260001-{index}",
                "record_key": f"record-{index}",
                "deadline_at": f"2026-08-{index:02d}T10:00:00+08:00",
            }
            for index in range(1, 6)
        ]
        records = [
            {
                "order_no": f"260001-{index}",
                "record_key": f"record-{index}",
                "status": status,
            }
            for index, status in enumerate(
                ["待处理", "审核中", "申诉成功", "申诉失败"], start=1
            )
        ]
        with (
            patch(
                "reconsideration.scan_all_reconsiderable_orders",
                return_value=scanned,
            ),
            patch(
                "reconsideration.db.list_reconsideration_records",
                return_value=records,
            ),
        ):
            pending = collect_pending_reconsideration_tasks(
                MagicMock(), "店铺A", max_orders=20
            )

        self.assertEqual(
            [(item["order_no"], item["record_key"]) for item in pending],
            [("260001-5", "record-5")],
        )


class ReconsiderationDatabaseTest(unittest.TestCase):
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

    def test_reconsideration_status_does_not_overwrite_order_status(self) -> None:
        db.record_collected("店铺A", "260001-1", issue_type="商品问题")
        db.record_order_review_status("店铺A", "260001-1", "申诉未通过")
        db.record_reconsideration_discovered(
            "店铺A",
            "260001-1",
            "2026-08-01 10:00:00|商品问题",
            issue_type="商品问题",
            penalty_amount="3.00",
            punished_at="2026-08-01 10:00:00",
            countdown_text="1天后不可复议",
            deadline_at="2026-08-27T10:00:00+08:00",
        )
        db.update_reconsideration_status(
            "店铺A",
            "260001-1",
            "2026-08-01 10:00:00|商品问题",
            "已提交复议",
            reconsider_reason="其他",
            reconsider_text="复用原文案",
            evidence_paths=["evidence/a.png"],
        )

        order = db.get_order("店铺A", "260001-1")
        records = db.list_reconsideration_records("店铺A")
        self.assertEqual(order["status"], "申诉未通过")
        self.assertEqual(records[0]["status"], "已提交复议")
        self.assertEqual(records[0]["evidence_paths"], ["evidence/a.png"])


if __name__ == "__main__":
    unittest.main()
