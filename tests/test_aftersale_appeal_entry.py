from __future__ import annotations

import os
import unittest
from html import escape
from unittest.mock import MagicMock, patch

from playwright.sync_api import Error, sync_playwright

import aftersale_appeal as appeal

ORDER_NO = "261001-123456789"


class AftersaleEntryValidationTests(unittest.TestCase):
    def test_missing_entry_reports_both_supported_names_without_clicking(self):
        page = MagicMock()
        page.get_by_role.return_value.first.wait_for.side_effect = (
            appeal.PlaywrightTimeoutError("missing")
        )
        with patch.object(appeal, "safe_click") as click:
            with self.assertRaisesRegex(RuntimeError, "维权申诉 / 极速退款申诉"):
                appeal.open_aftersale_appeal_modal(page)
        click.assert_not_called()

    def test_multiple_matching_forms_are_not_returned(self):
        page = MagicMock()
        page.get_by_role.return_value.count.return_value = 1
        modal = page.locator.return_value.filter.return_value
        modal.count.return_value = 2
        with patch.object(appeal, "safe_click"):
            with self.assertRaisesRegex(RuntimeError, "表单数量异常：2"):
                appeal.open_aftersale_appeal_modal(page)


class AftersaleEntryBrowserTests(unittest.TestCase):
    """离线复现两种售后入口及弹窗，不访问平台或提交申诉。"""

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
        self.enterContext(
            patch.object(
                appeal,
                "safe_click",
                side_effect=lambda _page, locator, _label: locator.click(),
            )
        )

    def render(
        self,
        button_title,
        modal_title,
        order_no=ORDER_NO,
        appeal_item="货款申诉",
        hidden=False,
    ):
        hidden_button = (
            '<button style="display:none">维权申诉</button>' if hidden else ""
        )
        self.page.set_content(f"""<!doctype html><meta charset="utf-8">
            <h2>退款申请单</h2>{hidden_button}
            <button id="appeal-entry" onclick="window.appealClicks++;document.querySelector('#form').style.display='block'">{escape(button_title)}</button>
            <button onclick="window.otherClicks++">查看维权申诉记录</button>
            <div data-testid="beast-core-modal-innerWrapper" style="display:none">维权申诉：旧的隐藏表单</div>
            <div id="form" data-testid="beast-core-modal-innerWrapper" style="display:none">
                <h2>{escape(modal_title)}</h2>
                <div data-testid="beast-core-form-item"><label>申诉订单号</label><span>{escape(order_no)}</span></div>
                <div data-testid="beast-core-form-item"><label>申诉项</label><span>{escape(appeal_item)}</span></div>
            </div>
            <script>window.appealClicks=0;window.otherClicks=0;</script>""")

    def test_original_dispute_entry_still_opens_and_verifies_form(self):
        self.render("维权申诉", "维权申诉")
        modal = appeal.open_aftersale_appeal_modal(self.page)
        appeal.verify_modal_context(
            modal, {"order_no": ORDER_NO, "appeal_item": "货款申诉"}
        )
        self.assertEqual(modal.get_attribute("id"), "form")
        self.assertEqual(self.page.evaluate("window.appealClicks"), 1)
        self.assertEqual(self.page.evaluate("window.otherClicks"), 0)

    def test_fast_refund_entry_accepts_both_possible_modal_titles(self):
        for title in ("维权申诉", "极速退款申诉"):
            with self.subTest(modal_title=title):
                self.render("极速退款申诉", title, hidden=True)
                modal = appeal.open_aftersale_appeal_modal(self.page)
                appeal.verify_modal_context(
                    modal, {"order_no": ORDER_NO, "appeal_item": "货款申诉"}
                )
                self.assertEqual(modal.get_attribute("id"), "form")
                self.assertEqual(self.page.evaluate("window.appealClicks"), 1)
                self.assertEqual(self.page.evaluate("window.otherClicks"), 0)

    def test_delayed_fast_refund_entry_is_waited_for(self):
        self.render("极速退款申诉", "极速退款申诉")
        self.page.evaluate("""() => {
            const button=document.querySelector('#appeal-entry');
            button.style.display='none';
            setTimeout(() => button.style.display='inline-block', 120);
        }""")
        self.assertEqual(
            appeal.open_aftersale_appeal_modal(self.page).get_attribute("id"), "form"
        )

    def test_two_visible_entries_only_click_dispute_regardless_of_order(self):
        for fast_refund_first in (False, True):
            with self.subTest(fast_refund_first=fast_refund_first):
                self.render("维权申诉", "维权申诉")
                self.page.evaluate("""fastFirst => {
                    const fast=document.createElement('button');
                    fast.innerHTML='<span>极速退款申诉</span>';
                    fast.onclick=() => window.otherClicks++;
                    const dispute=document.querySelector('#appeal-entry');
                    dispute.innerHTML='<span>维权申诉</span>';
                    if (fastFirst) dispute.before(fast); else dispute.after(fast);
                }""", fast_refund_first)
                modal = appeal.open_aftersale_appeal_modal(self.page)
                appeal.verify_modal_context(
                    modal, {"order_no": ORDER_NO, "appeal_item": "货款申诉"}
                )
                self.assertEqual(self.page.evaluate("window.appealClicks"), 1)
                self.assertEqual(self.page.evaluate("window.otherClicks"), 0)

    def test_duplicate_dispute_entries_still_stop_without_clicking(self):
        self.render("维权申诉", "维权申诉")
        self.page.evaluate("""() => {
            const duplicate=document.createElement('button');duplicate.textContent='维权申诉';
            duplicate.onclick=() => window.otherClicks++;document.body.append(duplicate);
        }""")
        with self.assertRaisesRegex(RuntimeError, "按钮数量异常：2"):
            appeal.open_aftersale_appeal_modal(self.page)
        self.assertEqual(
            self.page.evaluate("window.appealClicks+window.otherClicks"), 0
        )

    def test_duplicate_fast_refund_entries_still_stop_without_clicking(self):
        self.render("极速退款申诉", "极速退款申诉")
        self.page.evaluate("""() => {
            const duplicate=document.createElement('button');duplicate.textContent='极速退款申诉';
            duplicate.onclick=() => window.otherClicks++;document.body.append(duplicate);
        }""")
        with self.assertRaisesRegex(RuntimeError, "按钮数量异常：2"):
            appeal.open_aftersale_appeal_modal(self.page)
        self.assertEqual(
            self.page.evaluate("window.appealClicks+window.otherClicks"), 0
        )

    def test_disabled_dispute_does_not_fall_back_to_fast_refund(self):
        self.render("维权申诉", "维权申诉")
        self.page.evaluate("""() => {
            document.querySelector('#appeal-entry').disabled=true;
            const fast=document.createElement('button');fast.textContent='极速退款申诉';
            fast.onclick=() => window.otherClicks++;document.body.append(fast);
        }""")
        with patch.object(appeal, "safe_click", side_effect=lambda _page, button, _label: button.click(timeout=200)):
            with self.assertRaises(appeal.PlaywrightTimeoutError):
                appeal.open_aftersale_appeal_modal(self.page)
        self.assertEqual(
            self.page.evaluate("window.appealClicks+window.otherClicks"), 0
        )

    def test_fast_refund_form_still_rejects_wrong_order_or_item(self):
        for order_no, item, message in (
            ("261001-999999999", "货款申诉", "订单不一致"),
            (ORDER_NO, "运费申诉", "申诉项不一致"),
        ):
            with self.subTest(order_no=order_no, appeal_item=item):
                self.render(
                    "极速退款申诉", "极速退款申诉", order_no=order_no, appeal_item=item
                )
                modal = appeal.open_aftersale_appeal_modal(self.page)
                with self.assertRaisesRegex(RuntimeError, message):
                    appeal.verify_modal_context(
                        modal, {"order_no": ORDER_NO, "appeal_item": "货款申诉"}
                    )


if __name__ == "__main__":
    unittest.main()
