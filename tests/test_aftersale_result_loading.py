from __future__ import annotations

import html
import unittest

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import check_aftersale_appeal_results as checker

HEADERS = ["订单编号", "申诉类型", "申诉时间", "申诉项", "申诉金额(元)", "审核状态", "操作"]
ROW = ["260810-490670733630708", "维权申诉", "2026-08-22 13:02", "货款申诉", "13.89", "部分通过", "详情"]


def table_html(*, loading: bool = False, missing_status: bool = False,
               empty: bool = False, order: str | None = ROW[0]) -> str:
    headers = [h for h in HEADERS if not (missing_status and h == "审核状态")]
    row = [order, *ROW[1:]]
    if missing_status:
        row.pop(5)
    header_html = "".join(f'<th data-testid="beast-core-table-th">{h}</th>' for h in headers)
    body = ""
    if order:
        body = '<tr data-testid="beast-core-table-body-tr">' + "".join(
            f'<td data-testid="beast-core-table-td">{html.escape(value)}</td>' for value in row
        ) + '</tr>'
    if empty:
        body += '<tr><td colspan="7"><div>暂无数据</div></td></tr>'
    return (
        '<div data-testid="beast-core-spin"><div data-testid="beast-core-table">'
        f'<table><thead><tr>{header_html}</tr></thead><tbody>{body}</tbody></table>'
        '<div>共有 0 条</div></div>'
        f'<div data-testid="beast-core-spin-mask" style="display:{"block" if loading else "none"}">'
        '加载中</div></div>'
    )


class AftersaleResultLoadingBrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.playwright = sync_playwright().start()
        cls.addClassCleanup(cls.playwright.stop)
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except PlaywrightError as exc:
            raise unittest.SkipTest(f"本地 Chromium 不可用：{exc}") from exc
        cls.addClassCleanup(cls.browser.close)

    def setUp(self) -> None:
        self.page = self.browser.new_page()
        self.addCleanup(self.page.close)

    def replace_later(self, content: str, delay_ms: int = 150) -> None:
        self.page.evaluate("""({content, delay}) => {
            setTimeout(() => { document.body.innerHTML = content; }, delay);
        }""", {"content": content, "delay": delay_ms})

    def test_screenshot_loading_placeholder_waits_for_status_column_and_rows(self) -> None:
        self.page.set_content(table_html(loading=True, missing_status=True, order=None))
        self.replace_later(table_html())
        rows = checker.extract_aftersale_result_rows(self.page, timeout_ms=2000)
        self.assertEqual(rows[0]["platform_status"], "部分通过")
        self.assertEqual(rows[0]["appeal_amount"], "13.89")

    def test_old_complete_rows_under_loading_mask_are_not_returned(self) -> None:
        self.page.set_content(table_html(loading=True))
        self.replace_later(table_html(order="260901-222222"))
        rows = checker.extract_aftersale_result_rows(self.page, timeout_ms=2000)
        self.assertEqual(rows[0]["order_no"], "260901-222222")

    def test_empty_hint_during_loading_is_not_treated_as_empty_result(self) -> None:
        self.page.set_content(table_html(loading=True, empty=True, order=None))
        self.replace_later(table_html())
        self.assertEqual(len(checker.extract_aftersale_result_rows(self.page, timeout_ms=2000)), 1)

    def test_confirmed_empty_table_can_omit_status_column(self) -> None:
        self.page.set_content(table_html(missing_status=True, empty=True, order=None))
        self.assertEqual(checker.extract_aftersale_result_rows(self.page, timeout_ms=500), [])

    def test_persistent_loading_reports_loading_timeout_not_missing_field(self) -> None:
        self.page.set_content(table_html(loading=True, missing_status=True, order=None))
        with self.assertRaisesRegex(RuntimeError, "页面仍显示加载中") as caught:
            checker.extract_aftersale_result_rows(self.page, timeout_ms=300)
        self.assertNotIn("缺少字段", str(caught.exception))

    def test_real_missing_column_is_reported_after_waiting(self) -> None:
        self.page.set_content(table_html(missing_status=True))
        with self.assertRaisesRegex(RuntimeError, "缺少字段：审核状态"):
            checker.extract_aftersale_result_rows(self.page, timeout_ms=300)

    def test_zero_counter_without_empty_hint_does_not_silently_return_empty(self) -> None:
        self.page.set_content(table_html(order=None))
        with self.assertRaisesRegex(RuntimeError, "未出现完整订单数据或明确的无记录提示"):
            checker.extract_aftersale_result_rows(self.page, timeout_ms=300)

    def test_pagination_waits_for_new_ready_rows(self) -> None:
        self.page.set_content(table_html())
        previous = checker.extract_aftersale_result_rows(self.page, timeout_ms=500)
        self.replace_later(table_html(loading=True, order="260901-333333"), 80)
        self.replace_later(table_html(order="260901-333333"), 220)
        rows = checker.extract_aftersale_result_rows(self.page, previous_rows=previous, timeout_ms=2000)
        self.assertEqual(rows[0]["order_no"], "260901-333333")

    def test_pagination_that_does_not_change_is_reported(self) -> None:
        self.page.set_content(table_html())
        previous = checker.extract_aftersale_result_rows(self.page, timeout_ms=500)
        with self.assertRaisesRegex(RuntimeError, "翻页后仍显示上一页数据"):
            checker.extract_aftersale_result_rows(self.page, previous_rows=previous, timeout_ms=300)

    def test_login_redirect_while_waiting_is_reported_as_login_failure(self) -> None:
        self.page.set_content(table_html(loading=True, missing_status=True, order=None))
        self.replace_later(
            '<div class="login-info-section"><div class="login-tab">'
            '<div data-tracking-click-viewid="scan_code">扫码登录</div>'
            '<div data-tracking-click-viewid="password_log">账号登录</div></div></div>'
        )
        with self.assertRaises(checker.LoginRequiredError):
            checker.extract_aftersale_result_rows(self.page, timeout_ms=2000)


if __name__ == "__main__":
    unittest.main()
