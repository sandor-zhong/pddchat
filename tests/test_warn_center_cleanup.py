import unittest
from unittest.mock import MagicMock

from stealth import create_stealth_context, install_warn_center_cleanup


class WarnCenterCleanupTests(unittest.TestCase):
    def page(self, url):
        page = MagicMock()
        page.url = url
        page.is_closed.return_value = False
        return page

    def test_existing_target_closed_other_pages_preserved(self):
        context = MagicMock()
        urls = [
            "https://mms.pinduoduo.com/orders/warnCenter?warnTabType=2&msfrom=EventCenter",
            "https://mms.pinduoduo.com/orders/warnCenter?warnTabType=1",
            "https://mms.pinduoduo.com/orders/appeal",
            "https://example.com/orders/warnCenter",
            "https://mms.pinduoduo.com/orders/warnCenterOther",
            "about:blank",
        ]
        context.pages = [self.page(url) for url in urls]
        install_warn_center_cleanup(context)
        for index, page in enumerate(context.pages):
            self.assertEqual(page.close.called, index < 2)

    def test_blank_popup_navigation_and_listener_cleanup(self):
        context = MagicMock()
        context.pages = []
        install_warn_center_cleanup(context)
        watch = context.on.call_args.args[1]
        page = self.page("about:blank")
        watch(page)
        callbacks = dict(call.args for call in page.on.call_args_list)
        page.url = "https://mms.pinduoduo.com/orders/warnCenter"
        callbacks["framenavigated"](object())
        page.close.assert_not_called()
        callbacks["framenavigated"](page.main_frame)
        page.close.assert_called_once_with(run_before_unload=False)
        callbacks["close"]()
        self.assertEqual(page.remove_listener.call_count, 2)

    def test_close_error_does_not_escape(self):
        context = MagicMock()
        page = self.page("https://mms.pinduoduo.com/orders/warnCenter")
        page.close.side_effect = RuntimeError("already closed")
        context.pages = [page]
        install_warn_center_cleanup(context)

    def test_cleanup_opt_in_not_passed_to_browser(self):
        playwright = MagicMock()
        context = playwright.chromium.launch_persistent_context.return_value
        context.pages = []
        create_stealth_context(playwright, "profile", close_warn_center=True)
        self.assertNotIn(
            "close_warn_center",
            playwright.chromium.launch_persistent_context.call_args.kwargs,
        )
        context.on.assert_called_once()


if __name__ == "__main__":
    unittest.main()
