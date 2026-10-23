import time
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

import aftersale_appeal as appeal
from tests.test_aftersale_appeal_eligibility import ENABLED, list_html


EMPTY = '''<div data-testid="beast-core-spin" class="spin-wrapper">
    <div id="mf-mms-orders-container"><div data-testid="beast-core-table">
    <table><tbody><tr><td id="empty">暂无相关订单</td></tr></tbody></table>
    </div></div><div id="mask" data-testid="beast-core-spin-mask" style="display:none">加载中</div>
    </div>'''


class AftersaleListLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)
        self.enterContext(patch.object(appeal, "jitter_wait"))
        self.enterContext(patch.object(appeal, "clear_blockers"))
        self.shot = self.enterContext(patch.object(appeal, "save_error_screenshot", return_value=None))

    def show_rows_later(self, delay_ms):
        self.page.evaluate('''({html, delay}) => {
            setTimeout(() => document.body.innerHTML = html, delay);
        }''', {"html": list_html([(1, ENABLED)]), "delay": delay_ms})

    def test_one_second_empty_placeholder_waits_for_real_orders(self):
        self.page.set_content(EMPTY)
        self.show_rows_later(1200)
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page), "rows")
        self.assertEqual(appeal.extract_aftersale_rows(self.page)[0]["order_no"], "261001-1")

    def test_session_does_not_finish_during_initial_empty_placeholder(self):
        self.page.set_content(EMPTY)
        self.show_rows_later(1200)
        with patch.object(appeal, "open_aftersale_appeal_page"):
            session = appeal.AftersaleAppealSession(MagicMock(), self.page, MagicMock(), max_orders=1)
            self.assertEqual(session.initialize(), 1)
        self.assertFalse(session.done)
        self.assertEqual(session.tasks[0]["order_no"], "261001-1")

    def test_genuine_empty_list_requires_three_seconds_of_stability(self):
        self.page.set_content(EMPTY)
        started = time.monotonic()
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page), "empty")
        self.assertGreaterEqual(time.monotonic() - started, 2.9)

    def test_visible_loading_mask_outside_root_prevents_empty_confirmation(self):
        self.page.set_content(EMPTY)
        self.page.evaluate('''() => {
            document.querySelector('#mask').style.display = 'block';
            setTimeout(() => document.querySelector('#mask').style.display = 'none', 900);
        }''')
        self.show_rows_later(1100)
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page, empty_stable_ms=400), "rows")

    def test_rows_under_loading_mask_are_not_processed_early(self):
        self.page.set_content(list_html([(1, ENABLED)]))
        self.page.evaluate('''() => {
            const mask = document.createElement('div');
            mask.dataset.testid = 'beast-core-spin-mask'; mask.textContent = '加载中';
            document.querySelector('#mf-mms-orders-container').append(mask);
            setTimeout(() => mask.remove(), 350);
        }''')
        started = time.monotonic()
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page), "rows")
        self.assertGreaterEqual(time.monotonic() - started, 0.3)

    def test_disappearing_empty_message_resets_confirmation_timer(self):
        self.page.set_content(EMPTY)
        self.page.evaluate('''() => {
            const empty = document.querySelector('#empty');
            setTimeout(() => empty.textContent = '', 250);
            setTimeout(() => empty.textContent = '暂无相关订单', 450);
        }''')
        self.show_rows_later(750)
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page, empty_stable_ms=500), "rows")

    def test_new_wait_does_not_reuse_previous_empty_confirmation(self):
        self.page.set_content(EMPTY)
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page, empty_stable_ms=250), "empty")
        started = time.monotonic()
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page, empty_stable_ms=250), "empty")
        self.assertGreaterEqual(time.monotonic() - started, 0.22)

    def test_unconfirmed_empty_state_times_out_instead_of_skipping_orders(self):
        self.page.set_content(EMPTY)
        with self.assertRaisesRegex(RuntimeError, "为避免漏单已停止"):
            appeal.wait_for_aftersale_list_ready(self.page, attempt_timeout_ms=120, attempts=2)
        self.shot.assert_called_once()

    def test_replaced_table_resets_empty_confirmation(self):
        self.page.set_content(EMPTY)
        self.page.evaluate('''() => {
            setTimeout(() => {
                const table = document.querySelector('[data-testid="beast-core-table"]');
                table.replaceWith(table.cloneNode(true));
            }, 250);
        }''')
        self.show_rows_later(650)
        self.assertEqual(appeal.wait_for_aftersale_list_ready(self.page, empty_stable_ms=500), "rows")


if __name__ == "__main__":
    unittest.main()
