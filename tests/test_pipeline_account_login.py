import os
import unittest
from threading import Event

from playwright.sync_api import sync_playwright

from login import account_login

_LOGIN_HTML = """<meta charset="utf-8">
<button data-tracking-click-viewid="password_log" onclick="document.querySelector('.password-section').hidden=false">账号登录</button>
<div class="password-section" hidden>
<input id="usernameId" value="old"><input id="passwordId" type="password" value="old">
<button data-tracking-click-viewid="account_login_confirmation" onclick="submitLogin()">登录</button></div>
<section class="verify-phone-container" hidden><input placeholder="请输入短信验证码">
<button id="confirm" onclick="location.href='/home/'">确认</button></section>
<script>function submitLogin(){ ACTION }</script>"""


class PipelineAccountLoginTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(
            headless=True, channel=os.environ.get("TEST_BROWSER_CHANNEL") or None
        )

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def tearDown(self):
        self.context.close()

    def _route(self, action):
        html = _LOGIN_HTML.replace("ACTION", action)
        self.context.route("**/*", lambda route: route.fulfill(content_type="text/html", body=html))

    def test_fills_and_submits_credentials(self):
        self._route("location.href='/home/'")
        success, _message = account_login(
            self.context, self.page, "account", "password", Event()
        )
        self.assertTrue(success)
        self.assertTrue(self.page.url.endswith("/home/"))

    def test_waits_for_manual_sms_confirmation(self):
        self._route(
            "document.querySelector('.password-section').hidden=true;"
            "document.querySelector('.verify-phone-container').hidden=false"
        )
        notices = []

        def confirm(message):
            notices.append(message)
            if "短信验证" in message:
                self.page.get_by_placeholder("请输入短信验证码").fill("123456")
                self.page.locator("#confirm").click()

        success, _message = account_login(
            self.context, self.page, "account", "password", Event(), progress=confirm
        )
        self.assertTrue(success)
        self.assertTrue(any("短信验证" in message for message in notices))


if __name__ == "__main__":
    unittest.main()
