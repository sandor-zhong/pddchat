from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

import appeal_submit

ORDER_NO = "260810-490670733630708"
PAGE_HTML = """
<div class="mms-header_search_new mms-header_intelligent_search">
  <input id="global-search" class="mms-header_search_new_box_input"
         placeholder="搜索功能/订单/商品/课程/规则/帮助/服务" value="原搜索关键词">
</div>
<input placeholder="请输入完整订单编号" style="display:none" value="隐藏字段">
<input id="similar" placeholder="请输入完整订单编号或商品编号" value="其他筛选">
<label>订单编号<input id="order" placeholder="请输入完整订单编号" value="260801-111111"></label>
<button>查询</button>
"""


class AppealSearchInputTest(unittest.TestCase):
    def test_replaces_order_number_without_page_keyboard_or_mouse_typing(self) -> None:
        page = MagicMock()
        target = page.get_by_placeholder.return_value.filter.return_value
        target.input_value.return_value = ORDER_NO
        with patch.object(appeal_submit, "human_type") as type_text:
            appeal_submit.fill_appeal_search_order_no(page, f" {ORDER_NO} ")
        page.get_by_placeholder.assert_called_once_with("请输入完整订单编号", exact=True)
        target.fill.assert_called_once_with(ORDER_NO, timeout=20_000)
        type_text.assert_not_called()
        page.keyboard.type.assert_not_called()

    def test_value_mismatch_stops_before_query_button_is_clicked(self) -> None:
        page = MagicMock()
        target = page.get_by_placeholder.return_value.filter.return_value
        target.input_value.return_value = "260810-"
        with patch.object(appeal_submit, "safe_click") as click, self.assertRaisesRegex(
            RuntimeError, "订单编号填写失败"
        ):
            appeal_submit.search_appeal_order(page, ORDER_NO)
        click.assert_not_called()

    def test_empty_order_does_not_touch_any_field(self) -> None:
        page = MagicMock()
        with self.assertRaises(ValueError):
            appeal_submit.fill_appeal_search_order_no(page, "  ")
        page.get_by_placeholder.assert_not_called()


class AppealSearchInputBrowserTest(unittest.TestCase):
    """本地 HTML 回归，不连接真实店铺或提交查询。"""

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
        self.page.set_content(PAGE_HTML)

    def assert_only_order_changed(self, expected: str = ORDER_NO) -> None:
        self.assertEqual(self.page.locator("#order").input_value(), expected)
        self.assertEqual(self.page.locator("#global-search").input_value(), "原搜索关键词")
        self.assertEqual(self.page.locator("#similar").input_value(), "其他筛选")
        self.assertEqual(self.page.locator('input[style="display:none"]').input_value(), "隐藏字段")

    def test_global_search_initially_focused_and_previous_order_is_replaced(self) -> None:
        self.page.locator("#global-search").focus()
        appeal_submit.fill_appeal_search_order_no(self.page, ORDER_NO)
        self.assert_only_order_changed()
        appeal_submit.fill_appeal_search_order_no(self.page, "260902-222222")
        self.assert_only_order_changed("260902-222222")

    def test_input_event_steals_focus_without_redirecting_remaining_characters(self) -> None:
        self.page.evaluate("""() => {
            window.lastOrderInput = null;
            document.querySelector('#order').addEventListener('input', (event) => {
                window.lastOrderInput = event.target.value;
                document.querySelector('#global-search').focus();
            });
        }""")
        appeal_submit.fill_appeal_search_order_no(self.page, ORDER_NO)
        self.assert_only_order_changed()
        self.assertEqual(self.page.evaluate("window.lastOrderInput"), ORDER_NO)

    def test_mouse_handler_that_redirects_focus_is_not_triggered(self) -> None:
        self.page.evaluate("""() => {
            document.querySelector('#order').addEventListener('mousedown', (event) => {
                event.preventDefault();
                document.querySelector('#global-search').focus();
            });
        }""")
        appeal_submit.fill_appeal_search_order_no(self.page, ORDER_NO)
        self.assert_only_order_changed()

    def test_duplicate_visible_order_fields_fail_without_touching_global_search(self) -> None:
        self.page.locator("#order").evaluate("el => el.after(el.cloneNode())")
        with self.assertRaises(PlaywrightError):
            appeal_submit.fill_appeal_search_order_no(self.page, ORDER_NO)
        self.assertEqual(self.page.locator("#global-search").input_value(), "原搜索关键词")


if __name__ == "__main__":
    unittest.main()
