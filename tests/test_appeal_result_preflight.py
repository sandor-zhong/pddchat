import io
import json
import time
import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import sync_playwright

import check_appeal_results as checker
import main
from login import LoginRequiredError
import login


class AppealResultPreflightTests(unittest.TestCase):
    def run_mode(self, records, since=None, error=None):
        stream = io.StringIO()
        context = MagicMock()
        page = MagicMock()
        page.is_closed.return_value = True
        context.pages = [page]
        with patch.object(checker.db, "list_orders", return_value=records), \
             patch.object(main, "create_stealth_context", return_value=context) as create, \
             patch.object(main, "wait_for_login") as chat_login, \
             patch.object(main, "set_give_up_after_auto_solve_fails"), \
             patch.object(checker, "check_appeal_results", return_value=[], side_effect=error) as query:
            main.run_check_appeal_results_mode(MagicMock(), stream, 3, submitted_since=since)
        return json.loads(stream.getvalue()), context, create, chat_login, query

    def test_no_local_pending_orders_does_not_launch_browser(self):
        result, _, create, login, query = self.run_mode([])
        self.assertTrue(result["success"])
        self.assertTrue(result["skipped"])
        self.assertIn("本地没有", result["message"])
        self.assertIn("不会自动导入平台历史订单", result["message"])
        create.assert_not_called()
        login.assert_not_called()
        query.assert_not_called()

    def test_all_targets_before_cutoff_explains_time_range(self):
        result, _, create, _, query = self.run_mode(
            [{"order_no": "old", "submitted_at": "2026-10-17T15:59:59+00:00"}],
            "2026-10-18 00:00:00",
        )
        self.assertTrue(result["skipped"])
        self.assertIn("1 条待回查记录", result["message"])
        self.assertIn("2026-10-18 00:00:00", result["message"])
        create.assert_not_called()
        query.assert_not_called()

    def test_eligible_targets_use_business_page_without_chat_login(self):
        records = [{"order_no": "boundary", "submitted_at": "2026-10-17T16:00:00+00:00"}]
        result, context, create, login, query = self.run_mode(records, "2026-10-18 00:00:00")
        self.assertTrue(result["success"])
        self.assertNotIn("skipped", result)
        create.assert_called_once()
        login.assert_not_called()
        self.assertEqual(query.call_args.kwargs["target_orders"], records)
        context.close.assert_called_once()

    def test_navigation_failure_is_reported_as_failure(self):
        result, context, _, login, _ = self.run_mode(
            [{"order_no": "target"}], error=RuntimeError("net::ERR_ABORTED"),
        )
        self.assertFalse(result["success"])
        self.assertIn("net::ERR_ABORTED", result["message"])
        self.assertNotIn("skipped", result)
        login.assert_not_called()
        context.close.assert_called_once()

    def test_expired_login_in_detail_is_not_saved_as_pending_result(self):
        page = MagicMock()
        page.context.new_page.return_value.is_closed.return_value = False
        record = {"order_no": "target", "ticket_sn": "ticket-1"}
        with patch.object(checker, "jitter_wait"), \
             patch.object(checker, "clear_blockers"), \
             patch.object(checker, "extract_appeal_status_from_detail_page", side_effect=LoginRequiredError("登录已失效")), \
             patch.object(checker.db, "record_order_review_status") as write:
            with self.assertRaises(LoginRequiredError):
                checker.query_missing_orders_by_ticket_sn(page, "shop", {"target": record}, ["target"], direct=True)
            write.assert_not_called()
        page.context.new_page.return_value.close.assert_called_once()


class AppealResultLoginBrowserTests(unittest.TestCase):
    LOGIN_HTML = '''<div class="login-info-section"><div class="login-tab">
        <span data-tracking-click-viewid="scan_code">扫码登录</span>
        <span data-tracking-click-viewid="password_log">账号登录</span>
        </div></div>'''

    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        cls.browser = cls.playwright.chromium.launch(headless=True)
        cls.addClassCleanup(cls.browser.close)

    def setUp(self):
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def test_detail_status_is_read_without_chat_navigation(self):
        self.page.set_content('''<section class="detail_status__fixture">
            <h2 class="detail_subTitle__fixture">处理状态</h2>
            <p class="detail_statusName__fixture">已申诉<button>联系客服</button></p>
            </section>''')
        self.assertEqual(checker.extract_appeal_status_from_detail_page(self.page), "已申诉")
        self.assertEqual(self.page.url, "about:blank")

    def test_login_panel_is_reported_as_login_failure(self):
        self.page.set_content(self.LOGIN_HTML)
        with self.assertRaisesRegex(LoginRequiredError, "登录已失效"):
            checker.extract_appeal_status_from_detail_page(self.page)

    def test_delayed_login_panel_is_detected_while_waiting_for_status(self):
        self.page.set_content("<p>正在加载</p>")
        self.page.evaluate("html => setTimeout(() => document.body.innerHTML = html, 150)", self.LOGIN_HTML)
        with self.assertRaisesRegex(LoginRequiredError, "登录已失效"):
            checker.extract_appeal_status_from_detail_page(self.page)

    def test_http_redirect_to_blank_login_page_is_detected_without_login_dom(self):
        def handle(route):
            if "/login/" in route.request.url:
                route.fulfill(status=200, content_type="text/html", body="<html><body></body></html>")
            else:
                route.fulfill(status=302, headers={"location": "https://mms.pinduoduo.com/login/?redirect=/orders"}, body="")

        self.page.route("**/*", handle)
        self.page.goto("https://mms.pinduoduo.com/orders/result-fixture")
        self.assertEqual(self.page.locator(login.LOGIN_PAGE_SELECTOR).count(), 0)
        with self.assertRaises(LoginRequiredError):
            checker.extract_appeal_status_from_detail_page(self.page)

    def test_delayed_redirect_is_detected_before_business_wait_timeout(self):
        def handle(route):
            body = "" if "/login/" in route.request.url else '''<script>
                setTimeout(() => location.href = '/login/?redirect=/orders', 150);
                </script>'''
            route.fulfill(status=200, content_type="text/html", body=body)

        self.page.route("**/*", handle)
        self.page.goto("https://mms.pinduoduo.com/orders/result-fixture")
        started = time.monotonic()
        with self.assertRaises(LoginRequiredError):
            login.wait_for_content_or_login(self.page, self.page.locator("#missing-result"), timeout=5000)
        self.assertLess(time.monotonic() - started, 3)


if __name__ == "__main__":
    unittest.main()
