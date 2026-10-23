from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

import login


class LoginPageWaitTest(unittest.TestCase):
    def test_visible_login_stops_immediately_without_waiting_or_navigating(self) -> None:
        page, content = MagicMock(), MagicMock()
        with patch.object(login, "is_login_page", return_value=True), self.assertRaisesRegex(
            login.LoginRequiredError, "扫码登录 / 账号登录"
        ):
            login.wait_for_content_or_login(page, content)
        content.filter.assert_not_called()
        page.goto.assert_not_called()

    def test_delayed_login_panel_wins_over_missing_business_content(self) -> None:
        page, content = MagicMock(), MagicMock()
        with patch.object(login, "is_login_page", side_effect=[False, True]), self.assertRaises(
            login.LoginRequiredError
        ):
            login.wait_for_content_or_login(page, content)
        content.filter.return_value.or_.return_value.first.wait_for.assert_called_once()

    def test_normal_content_continues_without_navigating(self) -> None:
        page, content = MagicMock(), MagicMock()
        with patch.object(login, "is_login_page", return_value=False):
            login.wait_for_content_or_login(page, content)
        content.filter.return_value.or_.return_value.first.wait_for.assert_called_once()
        page.goto.assert_not_called()

    def test_generic_load_timeout_is_not_reported_as_confirmed_login_failure(self) -> None:
        page, content = MagicMock(), MagicMock()
        content.filter.return_value.or_.return_value.first.wait_for.side_effect = PlaywrightTimeoutError("加载超时")
        with patch.object(login, "is_login_page", return_value=False), self.assertRaises(PlaywrightTimeoutError):
            login.wait_for_content_or_login(page, content, timeout=1)

    def test_login_url_variants_are_recognized_without_page_content(self):
        for url in (
            "https://mms.pinduoduo.com/login/",
            "https://mms.pinduoduo.com/login",
            "https://mms.pinduoduo.com/login/?redirect=https%3A%2F%2Fmms.pinduoduo.com%2Forders#password",
            "https://MMS.PINDUODUO.COM/login/",
        ):
            with self.subTest(url=url):
                page = MagicMock(url=url)
                self.assertTrue(login.is_login_page(page))
                page.locator.assert_not_called()

    def test_business_urls_are_not_confused_with_login_redirect_parameters(self):
        for url in (
            "https://mms.pinduoduo.com/orders?redirect=/login/",
            "https://mms.pinduoduo.com/orders#/login/",
            "https://mms.pinduoduo.com/login-help/",
            "https://other.example/login/",
            "https://mms.pinduoduo.com.other.example/login/",
        ):
            with self.subTest(url=url):
                self.assertFalse(login.is_login_url(url))

    def test_business_url_still_checks_inline_login_panel(self):
        page = MagicMock(url="https://mms.pinduoduo.com/orders")
        page.locator.return_value.first.is_visible.return_value = True
        self.assertTrue(login.is_login_page(page))


if __name__ == "__main__":
    unittest.main()
