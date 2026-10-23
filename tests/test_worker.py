"""Exercise the production worker with real, isolated, fully routed Chromium profiles."""
from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from playwright.sync_api import sync_playwright

from pdd_collector.database import Database
from pdd_collector.worker import CollectorWorker
from tests.test_collector import FIXTURES, SHOP_HTML


class WorkerIntegrationTests(unittest.TestCase):
    def test_account_login_validates_identity_and_does_not_collect(self):
        from tests.test_accounts import LOGIN_HTML
        html = '<meta charset="utf-8">' + SHOP_HTML + (FIXTURES / 'search.html').read_text(encoding='utf-8-sig')
        login_html = LOGIN_HTML.replace('ACTION', "location.href='/home/';")
        profiles, shown = [], []

        @contextmanager
        def routed():
            with sync_playwright() as real:
                def launch(**options):
                    profiles.append(options['user_data_dir'])
                    shown.append(not options['headless'])
                    options['headless'] = True
                    context = real.chromium.launch_persistent_context(**options)
                    context.route('**/*', lambda route: route.fulfill(content_type='text/html',
                        body=login_html if '/login/' in route.request.url else html)
                        if route.request.is_navigation_request() else route.abort())
                    return context
                yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

        with TemporaryDirectory() as folder:
            data_dir = Path(folder)
            db = Database(data_dir / 'test.sqlite3')
            db.add_shops([dict(shop_key=key, display_name=key, username='test-' + key, password='test-only')
                          for key in ('one', 'two')])
            with patch('pdd_collector.worker.sync_playwright', routed):
                for key in ('one', 'two'):
                    worker = CollectorWorker(key, db.path, data_dir, os.environ.get('TEST_BROWSER_CHANNEL', ''),
                                             0, mode='account_login', headless=True)
                    outcomes, prompts = [], []
                    worker.outcome.connect(lambda status, message: outcomes.append((status, message)))
                    worker.browser_ready.connect(lambda: prompts.append(True))
                    worker.run()
                    self.assertEqual(outcomes[-1][0], 'authenticated', outcomes)
                    self.assertFalse(prompts)
            self.assertNotEqual(profiles[0], profiles[1])
            self.assertTrue(all(shown), 'Account login must remain visible for human verification')
            self.assertEqual(db.query()[0], 0)
            self.assertEqual({s['login_status'] for s in db.managed_shops()}, {'verified'})

    def test_login_then_automatic_collection_with_two_isolated_profiles(self):
        html = ("<meta charset='utf-8'>" + SHOP_HTML +
                (FIXTURES / "search.html").read_text(encoding="utf-8-sig") +
                (FIXTURES / "table.html").read_text(encoding="utf-8-sig") + """<script>
                document.querySelector('#status [data-testid="beast-core-select-header"]').onclick = () => {
                    const option = document.createElement('div');
                    option.setAttribute('role','option'); option.textContent='申诉通过';
                    option.onclick=()=>{document.querySelector('#status input').value='申诉通过';option.remove();};
                    document.body.appendChild(option);
                };
                document.querySelector('button').onclick = () => setTimeout(() => {
                    const tbody=document.querySelector('tbody'); tbody.innerHTML=tbody.innerHTML;
                },100);
                </script>""")
        profiles = []

        @contextmanager
        def routed_playwright():
            with sync_playwright() as real:
                def launch(**options):
                    profiles.append(options["user_data_dir"])
                    options["headless"] = True
                    context = real.chromium.launch_persistent_context(**options)
                    context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html", body=html)
                                  if route.request.resource_type == "document" else route.abort())
                    return context
                yield SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))

        with TemporaryDirectory() as folder:
            data_dir = Path(folder)
            db = Database(data_dir / "appeals.sqlite3")
            db.add_shops([dict(shop_key=key, display_name=key) for key in ("one", "two")])
            with patch("pdd_collector.worker.sync_playwright", routed_playwright):
                for key, mode in (("one", "login"), ("one", "login_check"),
                                  ("one", "collect"), ("two", "collect")):
                    worker = CollectorWorker(key, db.path, data_dir, os.environ.get("TEST_BROWSER_CHANNEL", ""),
                                             0, mode=mode, headless=True)
                    results, prompts = [], []
                    worker.outcome.connect(lambda status, message: results.append((status, message)))
                    worker.browser_ready.connect(lambda: prompts.append(True))
                    if mode == "login":
                        worker.begin_collection()  # Simulate the user's GUI confirmation.
                    worker.run()
                    expected = "authenticated" if mode in ("login", "login_check") else "completed"
                    self.assertEqual(results[-1][0], expected, results)
                    self.assertEqual(len(prompts), 1 if mode == "login" else 0)
                    if mode in ("login", "login_check"):
                        self.assertEqual(db.query()[0], 0)
            self.assertEqual(profiles[0], profiles[1])
            self.assertEqual(profiles[1], profiles[2])
            self.assertNotEqual(profiles[2], profiles[3])
            self.assertEqual(db.query(shop_key="one")[0], 8)
            self.assertEqual(db.query(shop_key="two")[0], 8)
            self.assertEqual({s["login_status"] for s in db.managed_shops()}, {"verified"})


if __name__ == "__main__":
    unittest.main()
