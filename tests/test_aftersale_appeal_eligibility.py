from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error, sync_playwright

import aftersale_appeal as appeal


def list_html(buttons):
    """使用脱敏的九列业务表，复现平台的禁用按钮及可点击详情链接。"""
    rows = []
    for number, button in buttons:
        values = [f"261001-{number}", "测试商品", "已发货", "8.80", "5.70",
                  "2026-10-01 10:00:00", "货款申诉", "1天后将不可发起申诉",
                  button + '<a href="#" onclick="window.detailClicks++">售后详情</a>']
        cells = ''.join(f'<td data-testid="beast-core-table-td">{v}</td>' for v in values)
        rows.append(f'<tr data-testid="beast-core-table-body-tr"><td><input type="checkbox"></td>{cells}</tr>')
    return ('<!doctype html><meta charset="utf-8"><div id="mf-mms-orders-container">'
            '<div data-testid="beast-core-table"><table><tbody>' + ''.join(rows)
            + '</tbody></table></div></div><script>window.detailClicks=0;</script>')


ENABLED = '<button data-testid="beast-core-button"><span>发起申诉</span></button>'
DISABLED = ('<button disabled="" data-testid="beast-core-button" '
            'class="BTN_disabled_5-193-0"><span>发起申诉</span></button>')


class AftersaleEligibilityBrowserTests(unittest.TestCase):
    """离线验证列表按钮筛选，不访问商家平台、聊天或模型服务。"""

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        engine = os.environ.get("TEST_BROWSER_ENGINE", "chromium")
        try:
            cls.browser = getattr(cls.playwright, engine).launch(headless=True)
        except Error as exc:
            raise unittest.SkipTest(f"本地 {engine} 浏览器不可用：{exc}") from exc
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.detail = MagicMock()
        self.detail.is_closed.return_value = False
        self.open_detail = self.enterContext(patch.object(appeal, "open_aftersale_detail", return_value=self.detail))
        self.discover = self.enterContext(patch.object(appeal.db, "record_aftersale_appeal_discovered"))
        self.update = self.enterContext(patch.object(appeal.db, "update_aftersale_appeal_status"))
        self.screenshot = self.enterContext(patch.object(appeal, "save_refund_application_screenshot", return_value=Path("refund.png")))
        self.error_screenshot = self.enterContext(patch.object(appeal, "save_error_screenshot", return_value=None))
        self.analyze = self.enterContext(patch.object(appeal, "analyze_aftersale_chat", return_value=("测试分类", [], [], "[]")))
        self.submit = self.enterContext(patch.object(appeal, "fill_and_submit_aftersale_form", return_value=("5.70", Path("form.png"), "测试原因", "测试说明")))
        self.enterContext(patch.object(appeal, "extract_aftersale_id", return_value="test-aftersale"))
        self.enterContext(patch.object(appeal, "open_aftersale_appeal_page"))
        self.enterContext(patch.object(appeal, "clear_blockers"))
        self.enterContext(patch.object(appeal, "jitter_wait"))
        self.enterContext(patch.object(appeal, "human_delay"))

    def render(self, buttons):
        self.page.set_content(list_html(buttons))
        return appeal.extract_aftersale_rows(self.page)

    def process(self, task, **kwargs):
        return appeal.process_aftersale_task(MagicMock(), self.page, MagicMock(), task, **kwargs)

    def assert_no_processing(self):
        for action in (self.open_detail, self.discover, self.update, self.screenshot,
                       self.error_screenshot, self.analyze, self.submit):
            action.assert_not_called()
        self.assertEqual(self.page.evaluate("window.detailClicks"), 0)

    def test_disabled_button_skips_even_with_countdown_and_detail_link(self):
        task = self.render([(1, DISABLED)])[0]
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page), "rows")
        self.assertTrue(task["countdown_text"])
        self.assertEqual(self.process(task)["result"], appeal._UNAVAILABLE_APPEAL_RESULT)
        self.assert_no_processing()

    def test_all_unavailable_button_states_are_skipped(self):
        variants = [
            '<button aria-disabled="true">发起申诉</button>',
            '<fieldset disabled><button>发起申诉</button></fieldset>',
            '<button style="display:none">发起申诉</button>',
            '<button style="visibility:hidden">发起申诉</button>',
            '<button style="pointer-events:none">发起申诉</button>',
            '<div style="opacity:0"><button>发起申诉</button></div>',
            '<div inert><button>发起申诉</button></div>',
            '<button data-disabled="true">发起申诉</button>',
            '<button class="BTN_disabled_5-193-0">发起申诉</button>',
            '<button>查看发起申诉记录</button>',
            '',
            ENABLED + ENABLED,
        ]
        for button in variants:
            with self.subTest(button=button):
                task = self.render([(1, button)])[0]
                self.assertEqual(self.process(task)["result"], appeal._UNAVAILABLE_APPEAL_RESULT)
                self.assert_no_processing()

    def test_only_enabled_order_enters_details(self):
        tasks = self.render([(1, DISABLED), (2, ENABLED)])
        self.process(tasks[0])
        self.assert_no_processing()
        self.assertEqual(self.process(tasks[1])["result"], "已提交")
        self.assertEqual(self.open_detail.call_args.args[3], "261001-2")
        self.open_detail.assert_called_once()
        self.discover.assert_called_once()
        self.submit.assert_called_once()
        self.detail.close.assert_called_once()

    def test_button_is_rechecked_after_task_was_queued(self):
        task = self.render([(1, ENABLED)])[0]
        self.page.get_by_role("button", name="发起申诉", exact=True).evaluate("b => b.disabled=true")
        self.assertEqual(self.process(task)["result"], appeal._UNAVAILABLE_APPEAL_RESULT)
        self.assert_no_processing()

    def test_stop_before_button_check_never_opens_details(self):
        task = self.render([(1, ENABLED)])[0]
        with self.assertRaises(appeal.AftersaleAppealStopped):
            self.process(task, should_stop=lambda: True)
        self.assert_no_processing()

    def test_ambiguous_row_remains_a_failure_without_opening_details(self):
        task = self.render([(1, ENABLED), (1, ENABLED)])[0]
        result = self.process(task)
        self.assertIn("匹配到多行", result["result"])
        self.open_detail.assert_not_called()
        self.discover.assert_called_once()
        self.assertEqual(self.update.call_args.args[3], "失败")

    def session(self, limit):
        return appeal.AftersaleAppealSession(MagicMock(), self.page, MagicMock(), max_orders=limit)

    def test_session_skips_full_disabled_pages_without_consuming_limit(self):
        self.render([(1, DISABLED), (2, DISABLED)])
        pages = iter([[(3, DISABLED)], [(4, ENABLED), (5, ENABLED)]])
        def next_page(*args, **kwargs):
            self.render(next(pages))
            return {"clicked": True}
        with patch.object(appeal, "click_next_page", side_effect=next_page) as turn:
            session = self.session(1)
            self.assertEqual(session.initialize(), 1)
            outcome, remaining = session.process_next()
        self.assertEqual(outcome["order_no"], "261001-4")
        self.assertEqual((session.processed, session.page_number, remaining), (1, 3, 0))
        self.assertTrue(session.done)
        self.assertEqual(turn.call_count, 2)
        self.open_detail.assert_called_once()
        self.discover.assert_called_once()

    def test_all_disabled_orders_finish_without_failure_or_processing(self):
        self.render([(1, DISABLED), (2, DISABLED)])
        with patch.object(appeal, "click_next_page", return_value={"clicked": False}):
            session = self.session(2)
            session.initialize()
            self.assertEqual(session.process_next(), (None, 0))
        self.assertTrue(session.done)
        self.assertEqual(session.processed, 0)
        self.assert_no_processing()

    def test_restored_queue_rechecks_disabled_button_without_resubmitting(self):
        tasks = self.render([(1, ENABLED), (2, ENABLED), (3, ENABLED)])
        state = dict(processed=1, page_number=1, tasks=tasks, task_index=1,
                     done=False, visited_signatures=['|'.join(t['record_key'] for t in tasks)])
        self.render([(1, DISABLED), (2, DISABLED), (3, ENABLED)])
        session = self.session(2)
        self.assertEqual(session.restore(state), 1)
        outcome, remaining = session.process_next()
        self.assertEqual((outcome["order_no"], remaining, session.processed), ("261001-3", 0, 2))
        self.assertEqual(self.open_detail.call_args.args[3], "261001-3")
        self.open_detail.assert_called_once()
        self.discover.assert_called_once()

    def test_cli_skips_disabled_orders_without_consuming_limit_or_reporting_failure(self):
        self.render([(1, DISABLED), (2, DISABLED)])
        pages = iter([[(3, ENABLED), (4, ENABLED)]])
        def next_page(*args, **kwargs):
            self.render(next(pages))
            return {"clicked": True}
        progress = MagicMock()
        with patch.object(appeal, "click_next_page", side_effect=next_page) as turn:
            outcomes, stopped = appeal.run_aftersale_appeals(
                MagicMock(), self.page, MagicMock(), max_orders=1, on_progress=progress)
        self.assertFalse(stopped)
        self.assertEqual([v['order_no'] for v in outcomes], ["261001-3"])
        self.assertEqual(outcomes[0]['result'], '已提交')
        turn.assert_called_once()
        progress.assert_called_once_with(1, 1)
        self.discover.assert_called_once()
        self.open_detail.assert_called_once()


if __name__ == "__main__":
    unittest.main()
