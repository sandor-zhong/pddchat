"""Regression for handles retained across orders on a persistent chat page."""
import gc
import unittest
from unittest.mock import MagicMock, patch

import aftersale_appeal
import pdd_chat
from playwright.sync_api import sync_playwright


class WaitHandleTests(unittest.TestCase):
    def test_aftersale_state_read_failure_still_disposes(self):
        page = MagicMock()
        handle = page.wait_for_function.return_value
        handle.json_value.side_effect = ValueError('invalid state')
        with self.assertRaises(ValueError):
            aftersale_appeal.wait_for_aftersale_list_ready(page)
        handle.dispose.assert_called_once_with()

    def test_chat_image_wait_releases_handle(self):
        page = MagicMock()
        pdd_chat.wait_for_chat_images(page)
        page.wait_for_function.return_value.dispose.assert_called_once_with()

    def test_persistent_chat_page_does_not_accumulate_handles(self):
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            try:
                page = browser.new_page()
                page.set_content('<div class="new-message-list"></div>')
                connection = page._impl_obj._connection
                def handles():
                    return sum(type(obj).__name__ == 'JSHandle'
                               for obj in connection._objects.values())
                baseline = handles()
                for _ in range(300):
                    pdd_chat.wait_for_chat_images(page)
                gc.collect()
                self.assertEqual(handles(), baseline)
            finally:
                browser.close()


if __name__ == '__main__':
    unittest.main()
