from __future__ import annotations

import io
import itertools
import json
import queue
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import aftersale_appeal
import db
from main import run_aftersale_serve_mode


class AftersaleAppealRulesTest(unittest.TestCase):
    def test_form_completes_each_group_before_moving_down(self):
        events = []
        page, modal = MagicMock(), MagicMock()
        with ExitStack() as stack:
            stack.enter_context(patch('aftersale_appeal.open_aftersale_appeal_modal', return_value=modal))
            stack.enter_context(patch('aftersale_appeal._ordered_appeal_sections', return_value=[(0, '货款申诉'), (1, '运费申诉')]))
            for name in ('verify_modal_context', 'business_pause', 'save_natural_content_screenshot', 'submit_aftersale_form'):
                stack.enter_context(patch('aftersale_appeal.' + name))
            def reason(*args, **kwargs):
                events.append(('reason', kwargs['section_index'], kwargs['section_item']))
                return '非以上申诉原因'
            def amount(*args, **kwargs):
                events.append(('amount', kwargs['index']))
                return '6.89' if kwargs['index'] == 0 else '6.36'
            def upload(modal, field, paths, **kwargs):
                events.append(('upload', field))
                return paths
            stack.enter_context(patch('aftersale_appeal.select_appeal_reason', side_effect=reason))
            stack.enter_context(patch('aftersale_appeal.fill_maximum_amount', side_effect=amount))
            stack.enter_context(patch('aftersale_appeal._upload_images', side_effect=upload))
            stack.enter_context(patch('aftersale_appeal.wait_for_uploads', side_effect=lambda *a: events.append(('wait',))))
            stack.enter_context(patch('aftersale_appeal.generate_aftersale_description', side_effect=lambda *a: events.append(('generate',)) or '正文'))
            stack.enter_context(patch('aftersale_appeal.fill_description', side_effect=lambda *a: events.append(('description',))))
            aftersale_appeal.fill_and_submit_aftersale_form(page, {'order_no':'260001-1', 'record_key':'sequence', 'appeal_item':'运费申诉、货款申诉'}, Path('refund.png'), [Path('chat.png')], '分类', {})
        expected = []
        for i, item in enumerate(('货款申诉', '运费申诉')):
            expected += [('reason',i,item), ('amount',i), ('upload',f'subInfoVOS[{i}].mustImageUrls'), ('upload',f'subInfoVOS[{i}].optionalImageUrls'), ('wait',)]
        self.assertEqual(events, expected + [('generate',), ('description',)])

    def test_goods_and_freight_select_fallback_in_both_independent_fields(self):
        for bad_second in (False, True):
            with self.subTest(bad_second=bad_second):
                page, modal = MagicMock(), MagicMock()
                containers = [MagicMock(), MagicMock()]
                modal.locator.side_effect = lambda selector: containers[1 if '[1]' in selector else 0]
                containers[0].locator.return_value.input_value.side_effect = ['', aftersale_appeal.GOODS_FALLBACK_REASON]
                containers[1].locator.return_value.input_value.side_effect = ['', '其他' if bad_second else '其他 / 非以上申诉原因']
                with patch('aftersale_appeal._ordered_appeal_sections', return_value=[(0, '货款申诉'), (1, '运费申诉')]), patch('aftersale_appeal.stealth_safe_click'), patch('aftersale_appeal._click_cascader_option') as choose:
                    if bad_second:
                        with self.assertRaisesRegex(RuntimeError, '运费申诉原因读回校验失败'):
                            aftersale_appeal._select_appeal_reason(page, modal, '运费申诉、货款申诉')
                    else:
                        self.assertEqual(aftersale_appeal._select_appeal_reason(page, modal, '货款申诉、运费申诉'), aftersale_appeal.GOODS_FALLBACK_REASON)
                    self.assertEqual([c.args[2] for c in choose.call_args_list], ['非以上申诉原因', '其他', '非以上申诉原因'])

    def test_upload_supports_new_voucher_field_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            image = Path(temporary) / 'evidence.png'
            image.write_bytes(b'test')
            modal, absent, present = MagicMock(), MagicMock(), MagicMock()
            absent.count.return_value = 0
            present.locator.return_value.count.return_value = 1
            modal.locator.side_effect = [absent, present]
            with patch('aftersale_appeal.business_pause'):
                self.assertEqual(aftersale_appeal._upload_images(modal, 'subInfoVOS[0].mustImageUrls', [image], required=True), [image])
            self.assertEqual(modal.locator.call_args.args[0], '[id="subInfoVOS[0].mustVoucherUrls"]')
            present.locator.return_value.set_input_files.assert_called_once_with([str(image.resolve())])

    def test_all_known_combinations_preserve_items_and_use_expected_reason(self):
        items = [aftersale_appeal.GOODS_APPEAL_ITEM, aftersale_appeal.FREIGHT_APPEAL_ITEM,
                 aftersale_appeal.DISPUTE_APPEAL_ITEM]
        for count in (2, 3):
            for combination in itertools.permutations(items, count):
                for separator in ("、", "，", ",", "\n"):
                    value = separator.join(combination)
                    with self.subTest(value=value):
                        self.assertTrue(aftersale_appeal._supported_appeal_item(value))
                        modal, page = MagicMock(), MagicMock()
                        checked = modal.locator.return_value
                        checked.count.return_value = count
                        order = "260801-050121982311537"
                        with patch("aftersale_appeal._modal_form_value", side_effect=[order, value]), patch("aftersale_appeal._clean_text", side_effect=combination):
                            aftersale_appeal.verify_modal_context(modal, {"order_no": order, "appeal_item": value})
                        checked.click.assert_not_called()
                        expected = (
                            aftersale_appeal.GOODS_FALLBACK_REASON
                            if aftersale_appeal.GOODS_APPEAL_ITEM in combination
                            and (aftersale_appeal.DISPUTE_APPEAL_ITEM in combination
                                 or aftersale_appeal.FREIGHT_APPEAL_ITEM in combination)
                            else aftersale_appeal.GOODS_PREFERRED_REASON
                        )
                        reason = modal.locator.return_value.locator.return_value
                        sections = list(enumerate(combination))
                        with patch('aftersale_appeal._clean_text', side_effect=combination):
                            self.assertEqual(aftersale_appeal._ordered_appeal_sections(modal, value), sections)

    def test_combination_mismatch_still_blocks_wrong_form(self):
        modal = MagicMock()
        modal.locator.return_value.count.return_value = 2
        order = "260801-050121982311537"
        with patch("aftersale_appeal._modal_form_value", side_effect=[order, "货款申诉、运费申诉"]), patch("aftersale_appeal._clean_text", side_effect=["货款申诉", "纠纷退款率申诉"]):
            with self.assertRaisesRegex(RuntimeError, "申诉项不一致"):
                aftersale_appeal.verify_modal_context(modal, {"order_no": order, "appeal_item": "货款申诉、运费申诉"})

    def test_combined_items_preserve_default_checked_items(self) -> None:
        modal = MagicMock()
        checked = modal.locator.return_value
        checked.count.return_value = 2
        task = {"order_no": "260801-050121982311537", "appeal_item": "纠纷退款率申诉、货款申诉"}
        with (
            patch("aftersale_appeal._modal_form_value", side_effect=[task["order_no"], "货款申诉纠纷退款率申诉"]),
            patch("aftersale_appeal._clean_text", side_effect=["货款申诉", "纠纷退款率申诉"]),
        ):
            aftersale_appeal.verify_modal_context(modal, task)
        checked.click.assert_not_called()
        self.assertTrue(aftersale_appeal._supported_appeal_item(task["appeal_item"]))
        self.assertTrue(aftersale_appeal._supported_appeal_item("货款申诉、纠纷退款率申诉"))
        self.assertFalse(aftersale_appeal._supported_appeal_item("货款申诉、未知申诉"))

    def test_goods_and_dispute_reason_changes_quality_to_fallback(self) -> None:
        page, modal = MagicMock(), MagicMock()
        reason_input = modal.locator.return_value.locator.return_value
        reason_input.input_value.side_effect = [
            aftersale_appeal.GOODS_PREFERRED_REASON, aftersale_appeal.GOODS_FALLBACK_REASON,
        ]
        with (
            patch('aftersale_appeal._ordered_appeal_sections', return_value=[(0, '货款申诉')]),
            patch("aftersale_appeal.stealth_safe_click"),
            patch("aftersale_appeal._click_cascader_option") as choose,
        ):
            result = aftersale_appeal.select_appeal_reason(page, modal, "纠纷退款率申诉、货款申诉")
        self.assertEqual(result, aftersale_appeal.GOODS_FALLBACK_REASON)
        self.assertEqual(choose.call_args.args[2], aftersale_appeal.GOODS_FALLBACK_REASON)

    def test_list_waits_for_valid_rows_instead_of_table_shell(self) -> None:
        page = MagicMock()
        handle = MagicMock()
        handle.json_value.return_value = "rows"
        page.wait_for_function.return_value = handle

        with patch("aftersale_appeal.jitter_wait"):
            state = aftersale_appeal.wait_for_aftersale_list_ready(page)

        self.assertEqual(state, "rows")
        page.wait_for_function.assert_called_once()
        call = page.wait_for_function.call_args
        self.assertEqual(call.kwargs["timeout"], 20_000)
        probe_options = page.evaluate_handle.call_args.args[1]
        self.assertIn("暂无数据", probe_options["emptyTexts"])
        self.assertEqual(probe_options["emptyStableMs"], 3000)
        self.assertIs(call.kwargs["arg"], page.evaluate_handle.return_value)
        page.evaluate_handle.return_value.dispose.assert_called_once()

    def test_list_without_rows_or_explicit_empty_state_fails_closed(self) -> None:
        page = MagicMock()
        page.wait_for_function.side_effect = (
            aftersale_appeal.PlaywrightTimeoutError("still loading")
        )
        shot = Path("debug") / "list_not_ready.png"

        with (
            patch("aftersale_appeal.clear_blockers") as clear,
            patch("aftersale_appeal.jitter_wait"),
            patch(
                "aftersale_appeal.save_error_screenshot",
                return_value=shot,
            ) as screenshot,
        ):
            with self.assertRaisesRegex(RuntimeError, "为避免漏单已停止"):
                aftersale_appeal.wait_for_aftersale_list_ready(
                    page,
                    attempt_timeout_ms=10,
                    attempts=2,
                )

        self.assertEqual(page.wait_for_function.call_count, 2)
        self.assertEqual(clear.call_count, 2)
        screenshot.assert_called_once()

    def test_explicit_empty_list_finishes_without_processing(self) -> None:
        with (
            patch("aftersale_appeal.open_aftersale_appeal_page"),
            patch(
                "aftersale_appeal.wait_for_aftersale_list_ready",
                return_value="empty",
            ),
            patch("aftersale_appeal.process_aftersale_task") as process,
        ):
            outcomes, stopped = aftersale_appeal.run_aftersale_appeals(
                MagicMock(),
                MagicMock(),
                MagicMock(),
                max_orders=10,
            )

        self.assertEqual(outcomes, [])
        self.assertFalse(stopped)
        process.assert_not_called()

    def test_session_processes_exactly_one_order_per_next(self) -> None:
        context = MagicMock()
        list_page = MagicMock()
        chat_page = MagicMock()
        tasks = [
            {
                "order_no": f"260001-{index}",
                "record_key": f"record-{index}",
            }
            for index in (1, 2)
        ]
        outcomes = [
            {
                "order_no": task["order_no"],
                "record_key": task["record_key"],
                "result": "已提交",
            }
            for task in tasks
        ]

        with (
            patch("aftersale_appeal.open_aftersale_appeal_page"),
            patch(
                "aftersale_appeal.wait_for_aftersale_list_ready",
                return_value="rows",
            ),
            patch("aftersale_appeal.extract_aftersale_rows", return_value=tasks),
            patch(
                "aftersale_appeal.process_aftersale_task",
                side_effect=outcomes,
            ) as process,
        ):
            session = aftersale_appeal.AftersaleAppealSession(
                context,
                list_page,
                chat_page,
                max_orders=2,
            )
            self.assertEqual(session.initialize(), 2)
            first, first_remaining = session.process_next()
            self.assertEqual(process.call_count, 1)
            second, second_remaining = session.process_next()

        self.assertEqual(first["order_no"], "260001-1")
        self.assertEqual(first_remaining, 1)
        self.assertEqual(second["order_no"], "260001-2")
        self.assertEqual(second_remaining, 0)
        self.assertEqual(process.call_count, 2)

    def test_aftersale_serve_protocol_runs_one_order_for_one_next(self) -> None:
        class CommandInput:
            def __init__(self) -> None:
                self.commands: queue.Queue[str] = queue.Queue()

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
        chat_page.url = "https://mms.pinduoduo.com/mms-chat/search"
        list_page = MagicMock()
        context = MagicMock()
        context.pages = [chat_page]
        context.new_page.return_value = list_page
        session = MagicMock()
        session.checkpoint.return_value = {'processed': 1}
        session.initialize.return_value = 2

        def process_one():
            session.processed = 1
            command_input.put({"cmd": "stop"})
            return (
                {
                    "order_no": "260001-1",
                    "record_key": "record-1",
                    "result": "已提交",
                },
                1,
            )

        session.process_next.side_effect = process_one
        with (
            patch("main.sys.stdin", command_input),
            patch("main.create_stealth_context", return_value=context),
            patch("main.wait_for_login", return_value=chat_page),
            patch("main.set_give_up_after_auto_solve_fails"),
            patch(
                "main.aftersale_appeal.AftersaleAppealSession",
                return_value=session,
            ),
        ):
            run_aftersale_serve_mode(
                MagicMock(),
                output,
                max_orders=10,
            )

        messages = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(
            [message["type"] for message in messages],
            ["aftersale_ready", "activity", "aftersale_order_result"],
        )
        session.process_next.assert_called_once_with()

    def test_extract_row_collects_all_requested_business_fields(self) -> None:
        page = MagicMock()
        rows = MagicMock()
        row = MagicMock()
        cells = MagicMock()
        cell_items = [MagicMock() for _ in range(9)]
        page.locator.return_value = rows
        rows.count.return_value = 1
        rows.nth.return_value = row
        row.locator.return_value = cells
        row.get_by_text.return_value.count.return_value = 1
        cells.count.return_value = 9
        cells.nth.side_effect = cell_items.__getitem__
        values = [
            "260821-109010041182926",
            "商品标题 商品规格",
            "已发货",
            "8.80",
            "5.70",
            "2026-08-21 10:20:30",
            "运费申诉",
            "1天2小时后将不可发起申诉",
            "售后详情",
        ]
        for cell, value in zip(cell_items, values, strict=True):
            cell.inner_text.return_value = value

        ellipses = MagicMock()
        ellipses.count.return_value = 2
        title = MagicMock()
        title.inner_text.return_value = "商品标题"
        spec = MagicMock()
        spec.inner_text.return_value = "商品规格"
        ellipses.nth.side_effect = [title, spec]
        image = MagicMock()
        image.count.return_value = 1
        image.first.get_attribute.return_value = "https://example.test/goods.png"
        cell_items[1].locator.side_effect = lambda selector: (
            ellipses if "ellipsis" in selector else image
        )

        item = aftersale_appeal.extract_aftersale_rows(page)[0]

        self.assertEqual(item["order_no"], "260821-109010041182926")
        self.assertEqual(item["order_info"], "商品标题 商品规格")
        self.assertEqual(item["goods_title"], "商品标题")
        self.assertEqual(item["goods_spec"], "商品规格")
        self.assertEqual(item["goods_image_url"], "https://example.test/goods.png")
        self.assertEqual(item["shipping_status"], "已发货")
        self.assertEqual(item["paid_amount"], "8.80")
        self.assertEqual(item["refund_amount"], "5.70")
        self.assertEqual(item["refund_time"], "2026-08-21 10:20:30")
        self.assertEqual(item["appeal_item"], "运费申诉")
        self.assertEqual(item["countdown_text"], "1天2小时后将不可发起申诉")

    def test_record_key_separates_multiple_aftersales_for_one_order(self) -> None:
        first = aftersale_appeal.make_record_key(
            "260001-1", "2026-08-01 10:00:00", "货款申诉"
        )
        second = aftersale_appeal.make_record_key(
            "260001-1", "2026-08-02 10:00:00", "货款申诉"
        )
        self.assertNotEqual(first, second)

    def test_amount_always_uses_platform_maximum(self) -> None:
        modal = MagicMock()
        container = MagicMock()
        amount_input = MagicMock()
        modal.locator.return_value = container
        container.inner_text.return_value = "申诉金额 最多可申诉 2.93 元"
        container.locator.return_value = amount_input
        amount_input.input_value.return_value = "2.93"

        maximum = aftersale_appeal.fill_maximum_amount(modal)

        self.assertEqual(maximum, "2.93")
        amount_input.fill.assert_called_once_with("2.93")

    def test_goods_reason_prefers_quality_option_when_available(self) -> None:
        self.assertEqual(
            aftersale_appeal.choose_goods_reason([
                aftersale_appeal.GOODS_FALLBACK_REASON,
                aftersale_appeal.GOODS_PREFERRED_REASON,
            ]),
            aftersale_appeal.GOODS_PREFERRED_REASON,
        )

    def test_goods_reason_falls_back_for_logistics_variant(self) -> None:
        self.assertEqual(
            aftersale_appeal.choose_goods_reason([
                "集运仓/二段物流长时间未退货",
                aftersale_appeal.GOODS_FALLBACK_REASON,
            ]),
            aftersale_appeal.GOODS_FALLBACK_REASON,
        )

    def test_unknown_goods_reason_options_fail_closed(self) -> None:
        with self.assertRaises(RuntimeError):
            aftersale_appeal.choose_goods_reason(["尚未适配的新原因"])

    def test_preselected_freight_reason_is_verified_without_reopening(self) -> None:
        page = MagicMock()
        modal = MagicMock()
        container = MagicMock()
        reason_input = MagicMock()
        modal.locator.return_value = container
        container.locator.return_value = reason_input
        reason_input.input_value.return_value = (
            f"{aftersale_appeal.FREIGHT_REASON_PARENT} / "
            f"{aftersale_appeal.FREIGHT_REASON}"
        )
        with patch("aftersale_appeal.stealth_safe_click") as click:
            reason = aftersale_appeal.select_appeal_reason(
                page,
                modal,
                aftersale_appeal.FREIGHT_APPEAL_ITEM,
            )
        self.assertEqual(reason, aftersale_appeal.FREIGHT_REASON)
        click.assert_not_called()

    def test_unknown_appeal_item_fails_before_touching_form(self) -> None:
        modal = MagicMock()
        with self.assertRaises(RuntimeError):
            aftersale_appeal.select_appeal_reason(
                MagicMock(), modal, "补贴申诉"
            )
        modal.locator.assert_not_called()

    def test_filled_form_always_submits_and_never_clicks_cancel(self) -> None:
        detail_page = MagicMock()
        modal = MagicMock()
        refund_application = Path("refund.png")
        chat_image = Path("chat.png")
        form_path = Path("debug") / "filled.png"

        with (
            patch(
                "aftersale_appeal.open_aftersale_appeal_modal",
                return_value=modal,
            ),
            patch("aftersale_appeal.verify_modal_context"),
            patch(
                "aftersale_appeal.select_appeal_reason",
                return_value=aftersale_appeal.GOODS_PREFERRED_REASON,
            ),
            patch(
                "aftersale_appeal.generate_aftersale_description",
                return_value="申诉描述",
            ),
            patch("aftersale_appeal.fill_maximum_amount", return_value="2.93"),
            patch(
                "aftersale_appeal._upload_images",
                side_effect=[[refund_application], [chat_image]],
            ),
            patch("aftersale_appeal.fill_description"),
            patch("aftersale_appeal.wait_for_uploads"),
            patch.object(aftersale_appeal, "DEBUG_DIR", form_path.parent),
            patch("aftersale_appeal.submit_aftersale_form") as submit,
        ):
            aftersale_appeal.fill_and_submit_aftersale_form(
                detail_page,
                {
                    "order_no": "260001-1",
                    "record_key": "record-1",
                    "appeal_item": aftersale_appeal.GOODS_APPEAL_ITEM,
                },
                refund_application,
                [chat_image],
                "消费者误解商品信息",
                {"o": "260001-1", "m": []},
            )

        submit.assert_called_once_with(detail_page, modal, "260001-1")
        modal.get_by_role.assert_not_called()

    def test_combined_forms_upload_every_section_and_block_incomplete_first_section(self):
        for item, missing in [
            ("货款申诉、纠纷退款率申诉", False),
            ("货款申诉、运费申诉、纠纷退款率申诉", False),
            ("货款申诉、纠纷退款率申诉", True),
        ]:
            with self.subTest(item=item, missing=missing), ExitStack() as stack:
                page, modal = MagicMock(), MagicMock()
                refund, chat = Path('refund.png'), Path('chat.png')
                count = len(aftersale_appeal._appeal_items(item))
                stack.enter_context(patch('aftersale_appeal._ordered_appeal_sections', return_value=list(enumerate(item.split('、')))))
                stack.enter_context(patch('aftersale_appeal.open_aftersale_appeal_modal', return_value=modal))
                for name in ('verify_modal_context', 'wait_for_uploads', 'fill_description', 'save_natural_content_screenshot', 'business_pause'):
                    stack.enter_context(patch('aftersale_appeal.' + name))
                stack.enter_context(patch('aftersale_appeal.select_appeal_reason', return_value='非以上申诉原因'))
                stack.enter_context(patch('aftersale_appeal.generate_aftersale_description', return_value='组合申诉描述'))
                stack.enter_context(patch('aftersale_appeal.fill_maximum_amount', return_value='6.39'))
                files = [[refund], [chat]] * count
                if missing:
                    files[0] = []
                upload = stack.enter_context(patch('aftersale_appeal._upload_images', side_effect=files))
                submit = stack.enter_context(patch('aftersale_appeal.submit_aftersale_form'))
                args = (page, {'order_no':'260001-1', 'record_key':'combo', 'appeal_item':item}, refund, [chat], '分类', {})
                if missing:
                    with self.assertRaisesRegex(RuntimeError, '第1组凭证'):
                        aftersale_appeal.fill_and_submit_aftersale_form(*args)
                    submit.assert_not_called()
                else:
                    aftersale_appeal.fill_and_submit_aftersale_form(*args)
                    submit.assert_called_once_with(page, modal, '260001-1')
                self.assertEqual([call.args[1] for call in upload.call_args_list], [
                    f'subInfoVOS[{i}].{field}' for i in range(1 if missing else count)
                    for field in ('mustImageUrls', 'optionalImageUrls')
                ])

    def test_submit_requires_exact_button_and_hidden_modal(self) -> None:
        detail_page = MagicMock()
        modal = MagicMock()
        submit = MagicMock()
        submit.count.return_value = 1
        modal.get_by_role.return_value = submit

        with (
            patch("aftersale_appeal.stealth_safe_click") as click,
            patch("aftersale_appeal.wait_for_submit_captcha") as captcha,
        ):
            aftersale_appeal.submit_aftersale_form(
                detail_page,
                modal,
                "260001-1",
            )

        modal.get_by_role.assert_called_once_with(
            "button", name="提交申诉", exact=True
        )
        click.assert_called_once_with(
            detail_page, submit, "真实提交售后申诉"
        )
        captcha.assert_called_once_with(detail_page)
        modal.wait_for.assert_called_once_with(state="hidden", timeout=20_000)

    def test_stop_before_submit_cancels_the_filled_form(self) -> None:
        detail_page = MagicMock()
        modal = MagicMock()
        cancel = MagicMock()
        cancel.count.return_value = 1
        modal.get_by_role.return_value = cancel
        refund_application = Path("refund.png")

        with (
            patch(
                "aftersale_appeal.open_aftersale_appeal_modal",
                return_value=modal,
            ),
            patch("aftersale_appeal.verify_modal_context"),
            patch(
                "aftersale_appeal.select_appeal_reason",
                return_value=aftersale_appeal.GOODS_PREFERRED_REASON,
            ),
            patch(
                "aftersale_appeal.generate_aftersale_description",
                return_value="申诉描述",
            ),
            patch("aftersale_appeal.fill_maximum_amount", return_value="2.93"),
            patch(
                "aftersale_appeal._upload_images",
                side_effect=[[refund_application], []],
            ),
            patch("aftersale_appeal.fill_description"),
            patch("aftersale_appeal.wait_for_uploads"),
            patch("aftersale_appeal.submit_aftersale_form") as submit,
            patch("aftersale_appeal.stealth_safe_click") as click,
        ):
            with self.assertRaises(aftersale_appeal.AftersaleAppealStopped):
                aftersale_appeal.fill_and_submit_aftersale_form(
                    detail_page,
                    {
                        "order_no": "260001-1",
                        "record_key": "record-1",
                        "appeal_item": aftersale_appeal.GOODS_APPEAL_ITEM,
                    },
                    refund_application,
                    [],
                    "消费者误解商品信息",
                    {"o": "260001-1", "m": []},
                    should_stop=lambda: True,
                )

        submit.assert_not_called()
        click.assert_called_once_with(
            detail_page, cancel, "停止前取消售后申诉"
        )
        modal.wait_for.assert_called_once_with(state="hidden", timeout=10_000)

    def test_no_chat_does_not_call_model(self) -> None:
        page = MagicMock()
        with (
            patch("aftersale_appeal.open_chat_page"),
            patch(
                "aftersale_appeal.query_chat_records",
                return_value=([], None),
            ),
            patch(
                "aftersale_appeal.build_chat_record_dict",
                return_value={"messages": [], "evidence_screenshots": []},
            ),
            patch("aftersale_appeal.classify_appeal_evidence") as classify,
        ):
            classification, messages, evidence, compact = (
                aftersale_appeal.analyze_aftersale_chat(page, "260001-1")
            )

        self.assertEqual(classification, aftersale_appeal.NO_CHAT_RECORD_LABEL)
        self.assertEqual(messages, [])
        self.assertEqual(evidence, [])
        self.assertIsNone(compact)
        classify.assert_not_called()

    def test_no_chat_freight_description_matches_freight_reason(self) -> None:
        text = aftersale_appeal.generate_aftersale_description(
            None,
            aftersale_appeal.NO_CHAT_RECORD_LABEL,
            aftersale_appeal.FREIGHT_APPEAL_ITEM,
            aftersale_appeal.FREIGHT_REASON,
        )
        self.assertNotIn("属于消费者无理由退货", text)
        self.assertIn("暂无法确认退货原因", text)
        self.assertIn("运费扣款", text)


class AftersaleAppealDatabaseTest(unittest.TestCase):
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

    def test_aftersale_status_does_not_overwrite_normal_appeal(self) -> None:
        db.record_collected("店铺A", "260001-1", issue_type="商品问题")
        db.record_order_review_status("店铺A", "260001-1", "申诉通过")
        db.record_aftersale_appeal_discovered(
            "店铺A",
            "260001-1",
            "record-1",
            appeal_item="货款申诉",
            order_info="商品标题 商品规格",
            goods_title="商品标题",
            goods_spec="商品规格",
            goods_image_url="https://example.test/goods.png",
            shipping_status="已发货",
            paid_amount="2.93",
            refund_amount="2.93",
            refund_time="2026-08-01 10:00:00",
            countdown_text="7小时后将不可发起申诉",
        )
        db.update_aftersale_appeal_status(
            "店铺A",
            "260001-1",
            "record-1",
            "已提交",
            classification="消费者误解商品信息",
            appeal_reason=aftersale_appeal.GOODS_PREFERRED_REASON,
            appeal_text="买家没有提供有效质量问题凭证。",
            messages=[{"content": "不好用"}],
            message_count=1,
            refund_application_path="evidence/refund.png",
            chat_evidence_paths=["evidence/chat.png"],
        )

        order = db.get_order("店铺A", "260001-1")
        aftersale = db.list_aftersale_appeal_records("店铺A")[0]
        self.assertEqual(order["status"], "申诉通过")
        self.assertEqual(aftersale["status"], "已提交")
        self.assertEqual(aftersale["order_info"], "商品标题 商品规格")
        self.assertEqual(aftersale["shipping_status"], "已发货")
        self.assertEqual(aftersale["paid_amount"], "2.93")
        self.assertEqual(
            aftersale["appeal_reason"],
            aftersale_appeal.GOODS_PREFERRED_REASON,
        )
        self.assertEqual(aftersale["chat_evidence_paths"], ["evidence/chat.png"])
        self.assertEqual(aftersale["messages"], [{"content": "不好用"}])

    def test_existing_aftersale_table_gets_new_list_columns(self) -> None:
        connection = sqlite3.connect(db.DB_PATH)
        connection.execute(
            """
            CREATE TABLE aftersale_appeal_records (
                shop TEXT NOT NULL,
                order_no TEXT NOT NULL,
                record_key TEXT NOT NULL,
                status TEXT NOT NULL,
                refund_time TEXT,
                PRIMARY KEY (shop, order_no, record_key)
            )
            """
        )
        connection.commit()
        connection.close()

        migrated = db.get_connection()
        columns = {
            row[1]
            for row in migrated.execute(
                "PRAGMA table_info(aftersale_appeal_records)"
            ).fetchall()
        }
        self.assertTrue(
            {
                "order_info",
                "goods_title",
                "goods_spec",
                "goods_image_url",
                "shipping_status",
                "paid_amount",
                "appeal_reason",
            }.issubset(columns)
        )


if __name__ == "__main__":
    unittest.main()
