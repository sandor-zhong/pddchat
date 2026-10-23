import os
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from pdd_collector.account_import import normalize_accounts, rows_from_text
from pdd_collector.credentials import protect, unprotect
from pdd_collector.database import Database
from pdd_collector.login import account_login, LoginError, VerificationTimeout, LOGIN_URL
from pdd_collector.models import Cancelled, CollectionError


LOGIN_HTML = """<meta charset="utf-8">
<button data-tracking-click-viewid="password_log" onclick="document.querySelector('.password-section').hidden=false">账号登录</button>
<div class="password-section" hidden>
<input id="usernameId" value="old-account"><input id="passwordId" type="password" value="old-password">
<button data-tracking-click-viewid="account_login_confirmation" onclick="submitLogin()">登录</button></div>
<section class="verify-phone-container" hidden>请验证手机号后继续登录
<input placeholder="请输入短信验证码"><button id="confirm" onclick="location.href='/home/'">确认</button></section>
<script>
window.values=[];
for (const id of ['usernameId','passwordId']) document.getElementById(id).addEventListener('input',e=>window.values.push([id,e.target.value]));
function submitLogin(){
 sessionStorage.setItem('submitted', JSON.stringify(window.values));
 ACTION
}
</script>"""

SLIDER_HTML = (Path(__file__).parent / 'fixtures' / 'slider-captcha.html').read_text(encoding='utf-8')
SHOW_SLIDER = "document.querySelector('[data-testid=beast-core-modal-inner]').hidden=false;"
HIDE_SLIDER = "document.querySelector('[data-testid=beast-core-modal-inner]').hidden=true;"


class AccountStorageTests(unittest.TestCase):
    def test_paste_headers_order_csv_and_password_whitespace(self):
        records = normalize_accounts(rows_from_text("店铺名\t密码\t账号\t分组\n店一\t leading trailing \tuser1\t一组\n店二\t123456\tuser2\t"))
        self.assertEqual(records[0]["password"], " leading trailing ")
        self.assertEqual(records[1]["username"], "user2")
        records = normalize_accounts(rows_from_text('user,"pass,comma","shop,name"'))
        self.assertEqual(records[0]["password"], "pass,comma")
        for text in ('user,password', '账号\t密码\nuser\tpassword', '密码\t账号\npassword\tuser'):
            optional = normalize_accounts(rows_from_text(text))
            self.assertEqual(optional[0]['username'], 'user')
            self.assertEqual(optional[0]['display_name'], '')
        duplicates = normalize_accounts([[' user ', 'secret', 'old'], ['user', 'different', 'new']])
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]['display_name'], 'new')
        self.assertEqual(duplicates[0]['password'], 'different')
        for rows in ([['user', '', 'name']],):
            with self.assertRaises(ValueError) as caught:
                normalize_accounts(rows)
            self.assertNotIn('secret', str(caught.exception))

    @unittest.skipUnless(os.name == "nt", "Windows DPAPI")
    def test_encrypted_roundtrip_upsert_and_atomic_rollback(self):
        secret = " Test-密码-012345! "
        encrypted = protect(secret)
        self.assertNotIn(secret.encode(), encrypted)
        self.assertEqual(unprotect(encrypted), secret)
        with TemporaryDirectory() as folder:
            db = Database(Path(folder) / "test.sqlite3")
            db.add_shops([dict(shop_key="existing", display_name="店一", group_name="原分组", max_pages=7,
                               username='user1', password=secret)])
            rows = normalize_accounts([['user1', secret, '店一'], ['user2', 'test-only']])
            keys = db.import_accounts(rows)
            self.assertEqual(keys[0], "existing")
            self.assertEqual(db.get_credentials(keys[0]), ('user1', secret))
            self.assertNotIn(secret, str(db.managed_shops()))
            with db.connect() as conn:
                blob = conn.execute('SELECT password_encrypted FROM shop_credentials WHERE shop_key=?', (keys[0],)).fetchone()[0]
                self.assertNotIn(secret.encode(), blob)
            self.assertNotIn(secret.encode(), db.path.read_bytes())
            self.assertEqual(db.import_accounts(rows), keys)
            self.assertEqual(len(db.managed_shops()), 2)
            unnamed = next(s for s in db.managed_shops() if s['shop_key'] == keys[1])
            self.assertEqual(unnamed['display_name'], 'user2')
            db.import_accounts(normalize_accounts([['user1', secret]]))
            existing = next(s for s in db.managed_shops() if s['shop_key'] == keys[0])
            self.assertEqual(existing['display_name'], '店一')
            self.assertEqual((existing['max_pages'], existing['group_name']), (7, '原分组'))
            db.update_shops([keys[0]], username='user1', password='')
            self.assertEqual(db.get_credentials(keys[0])[1], secret)
            with self.assertRaises(ValueError):
                db.import_accounts([dict(username='new', password='test', display_name='新增'),
                                    dict(username='other', password='', display_name='店一')])
            self.assertEqual(len(db.managed_shops()), 2)
            with self.assertRaises(CollectionError):
                db.update_shops([keys[0]], username='changed', password='')
            self.assertEqual(db.get_credentials(keys[0])[0], 'user1')
            db.delete_shops([keys[1]])
            with self.assertRaises(CollectionError):
                db.get_credentials(keys[1])
            restored = db.import_accounts(normalize_accounts([['user2', 'replacement-test']]))
            self.assertEqual(restored, [keys[1]])
            self.assertEqual(db.get_credentials(keys[1])[1], 'replacement-test')
            self.assertEqual(len(db.managed_shops()), 2)

    @patch('pdd_collector.database.protect', side_effect=lambda value: value.encode())
    @patch('pdd_collector.database.unprotect', side_effect=lambda value: value.decode())
    def test_account_identity_dedup_and_conflicting_edit_are_atomic(self, *_):
        with TemporaryDirectory() as folder:
            db = Database(Path(folder) / 'dedup.sqlite3')
            keys = db.import_accounts([
                dict(username='account-a', password='first', display_name='同名店铺'),
                dict(username='account-b', password='second', display_name='同名店铺'),
                dict(username=' account-a ', password='last', display_name='改名店铺'),
            ])
            self.assertEqual(len(keys), 2)
            self.assertEqual(len(db.managed_shops()), 2)
            self.assertEqual(db.get_credentials(keys[0]), ('account-a', 'last'))
            self.assertEqual(db.import_accounts([dict(username='account-a', password='updated')]), [keys[0]])
            self.assertEqual(len(db.managed_shops()), 2)
            before = db.managed_shops()
            with self.assertRaises(CollectionError):
                db.add_shops([dict(shop_key='another-key', username=' account-a ', password='test')])
            with self.assertRaises(CollectionError):
                db.update_shops([keys[1]], display_name='不应保存', username=' account-a ', password='test')
            self.assertEqual(db.managed_shops(), before)
            self.assertEqual(db.get_credentials(keys[1]), ('account-b', 'second'))
            self.assertEqual(len(Database(db.path).managed_shops()), 2)


class AccountLoginTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(headless=True, channel=os.environ.get("TEST_BROWSER_CHANNEL") or None)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.page = self.context.new_page()
        self.stop = Event()

    def tearDown(self):
        self.context.close()

    def route(self, action):
        html = LOGIN_HTML.replace('ACTION', action) + SLIDER_HTML
        self.context.route('**/*', lambda route: route.fulfill(content_type='text/html', body=html)
                           if route.request.is_navigation_request() else route.abort())

    def test_clear_both_inputs_then_submit_and_redirect(self):
        self.route("location.href='/home/';")
        account_login(self.page, 'test-user', 'new-password', self.stop)
        import json
        submitted = json.loads(self.page.evaluate("sessionStorage.getItem('submitted')"))
        self.assertEqual(submitted, [['usernameId', ''], ['passwordId', ''],
                                     ['usernameId', 'test-user'], ['passwordId', 'new-password']])
        self.assertTrue(self.page.url.endswith('/home/'))

    def test_manual_sms_confirmation_resumes(self):
        self.route("document.querySelector('.password-section').hidden=true;document.querySelector('.verify-phone-container').hidden=false;")
        notices = []
        def human_confirmation(message):
            notices.append(message)
            self.page.get_by_placeholder('请输入短信验证码').fill('123456')
            self.page.locator('#confirm').click()
        account_login(self.page, 'test-user', 'test-password', self.stop, waiting=human_confirmation)
        self.assertEqual(len(notices), 1)
        self.assertIn('300', notices[0])
        self.assertTrue(self.page.url.endswith('/home/'))

    def test_sms_timeout_is_bounded_even_if_panel_disappears(self):
        self.route("document.querySelector('.verify-phone-container').hidden=false;")
        def hide_panel(message):
            self.page.evaluate("document.querySelector('.verify-phone-container').hidden=true")
        with self.assertRaises(VerificationTimeout):
            account_login(self.page, 'test-user', 'test-password', self.stop, waiting=hide_panel, verification_timeout=.3)

    def test_stop_during_sms_wait(self):
        self.route("document.querySelector('.verify-phone-container').hidden=false;")
        with self.assertRaises(Cancelled):
            account_login(self.page, 'test-user', 'test-password', self.stop, waiting=lambda _: self.stop.set())

    def test_wrong_password_does_not_retry(self):
        self.route("document.body.insertAdjacentHTML('beforeend','<div>账号或密码错误</div>');")
        with self.assertRaisesRegex(LoginError, '账号或密码错误'):
            account_login(self.page, 'test-user', 'test-password', self.stop)

    def test_slider_waits_for_human_despite_background_header(self):
        self.route(SHOW_SLIDER + "document.body.insertAdjacentHTML('beforeend','<div class=mms-header__user-info>后台标题</div>');")
        notices = []
        def human_completes_slider(message):
            notices.append(message)
            self.assertIn('滑块拼图', message)
            self.assertIn('300', message)
            # Simulate the website's successful response after human verification, not a slider solver.
            self.page.evaluate(HIDE_SLIDER + "location.href='/home/';")
        account_login(self.page, 'test-user', 'test-password', self.stop, waiting=human_completes_slider)
        self.assertEqual(len(notices), 1)
        self.assertTrue(self.page.url.endswith('/home/'))

    def test_slider_then_sms_each_has_its_own_budget(self):
        self.route(SHOW_SLIDER)
        clock = [0]
        notices = []
        def human_verifies(message):
            notices.append(message)
            if '滑块拼图' in message:
                clock[0] = 299
                self.page.evaluate(HIDE_SLIDER + "document.querySelector('.verify-phone-container').hidden=false;")
            else:
                self.assertIn('短信验证', message)
                self.assertIn('300', message)
                clock[0] = 598
                self.page.get_by_placeholder('请输入短信验证码').fill('123456')
                self.page.locator('#confirm').click()
        with patch('pdd_collector.login.monotonic', side_effect=lambda: clock[0]):
            account_login(self.page, 'test-user', 'test-password', self.stop, waiting=human_verifies)
        self.assertEqual(len(notices), 2)

    def test_slider_reappearing_does_not_restart_timeout(self):
        self.route(SHOW_SLIDER)
        clock = [0]
        notices = []
        def close_then_reopen(message):
            notices.append(message)
            if len(notices) == 1:
                self.page.evaluate(HIDE_SLIDER)
                clock[0] = 299
            else:
                self.page.evaluate(SHOW_SLIDER)
                clock[0] = 301
        with patch('pdd_collector.login.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaisesRegex(VerificationTimeout, '滑块拼图'):
                account_login(self.page, 'test-user', 'test-password', self.stop, waiting=close_then_reopen)
        self.assertEqual(len(notices), 2)

    def test_stop_during_slider_wait(self):
        self.route(SHOW_SLIDER)
        with self.assertRaises(Cancelled):
            account_login(self.page, 'test-user', 'test-password', self.stop, waiting=lambda _: self.stop.set())

    def test_password_never_in_playwright_error_result(self):
        self.route("location.href='/home/';")
        from playwright.sync_api import Locator
        original = Locator.fill
        def fail_password(locator, value, **kwargs):
            if value == 'sensitive-test-password':
                raise RuntimeError('Playwright fill log: sensitive-test-password')
            return original(locator, value, **kwargs)
        with patch.object(Locator, 'fill', fail_password):
            with self.assertRaises(LoginError) as caught:
                account_login(self.page, 'test-user', 'sensitive-test-password', self.stop)
            self.assertNotIn('sensitive-test-password', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
