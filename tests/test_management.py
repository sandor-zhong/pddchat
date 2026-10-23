from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import time
import unittest

from PyQt5.QtCore import QCoreApplication, QThread, pyqtSignal

from pdd_collector.database import Database
from pdd_collector.dialogs import parse_shop_import
from pdd_collector.execution import ExecutionController
from pdd_collector.models import Appeal, CollectionError, Shop


class FakeWorker(QThread):
    log = pyqtSignal(str)
    browser_ready = pyqtSignal()
    progress = pyqtSignal(int, int)
    outcome = pyqtSignal(str, str)
    waiting_verification = pyqtSignal(str)
    outcomes = {}
    starts = []
    live = 0
    max_live = 0
    delay = 3

    def __init__(self, key, database, data_dir, channel, max_pages, parent=None, mode="collect", headless=False):
        super().__init__(parent)
        self.key, self.mode = key, mode
        self.stop_event, self.start_event = Event(), Event()

    def begin_collection(self):
        self.start_event.set()

    def stop(self):
        self.stop_event.set()

    def run(self):
        cls = type(self)
        cls.live += 1
        cls.max_live = max(cls.max_live, cls.live)
        cls.starts.append(self.key)
        try:
            if self.mode == "account_login":
                self.waiting_verification.emit("等待人工短信验证，剩余 300 秒")
            if self.mode == "login":
                self.browser_ready.emit()
                while not self.start_event.is_set() and not self.stop_event.is_set():
                    self.msleep(2)
            self.msleep(cls.delay)
            configured = cls.outcomes.get(
                self.key, "authenticated" if self.mode in ("login", "account_login", "login_check") else "completed")
            if isinstance(configured, list):
                occurrence = cls.starts.count(self.key) - 1
                configured = configured[min(occurrence, len(configured) - 1)]
            status = "cancelled" if self.stop_event.is_set() else configured
            if status == "completed":
                self.progress.emit(1, 8)
            self.outcome.emit(status, f"模拟任务 {status}")
            # Outcome precedes actual cleanup, just like the real browser worker.
            self.msleep(5)
        finally:
            cls.live -= 1


class ManagementTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QCoreApplication.instance() or QCoreApplication([])

    def setUp(self):
        self.temp = TemporaryDirectory()
        self.folder = Path(self.temp.name)
        self.db = Database(self.folder / "appeals.sqlite3")
        FakeWorker.starts = []
        FakeWorker.outcomes = {}
        FakeWorker.live = FakeWorker.max_live = 0
        FakeWorker.delay = 3
        self.controller = ExecutionController(self.db, self.folder, worker_factory=FakeWorker)

    def tearDown(self):
        if self.controller.busy:
            self.controller.stop()
            self.wait_until(lambda: not self.controller.busy)
        self.controller.deleteLater()
        self.app.processEvents()
        self.temp.cleanup()

    def add(self, n):
        self.db.add_shops([dict(shop_key=f"shop-{i:03d}", display_name=f"店铺 {i:03d}", group_name=f"组{i % 4}") for i in range(n)])
        return [f"shop-{i:03d}" for i in range(n)]

    def wait_until(self, predicate, timeout=15):
        end = time.monotonic() + timeout
        while not predicate() and time.monotonic() < end:
            self.app.processEvents()
            time.sleep(0.002)
        self.assertTrue(predicate(), "Qt 队列未在预期时间完成")

    def test_eighty_shop_queue_continues_after_failures_without_overlap(self):
        keys = self.add(80)
        FakeWorker.outcomes = {keys[1]: "failed", keys[4]: "needs_login", keys[6]: "partial"}
        self.controller.start(keys)
        with self.assertRaises(RuntimeError):
            self.controller.start(keys)
        self.wait_until(lambda: not self.controller.busy)
        jobs = self.db.jobs(self.controller.batch_id)
        self.assertEqual(len(jobs), 80)
        self.assertEqual(sum(j["status"] == "completed" for j in jobs), 77)
        self.assertEqual(FakeWorker.starts, keys)
        self.assertEqual(FakeWorker.max_live, 1)
        self.assertEqual(set(self.controller.retry_keys()), {keys[1], keys[4], keys[6]})
        self.assertTrue(all(j["finished_at"] for j in jobs))

    def test_stop_cancels_pending_and_keeps_completed(self):
        keys = self.add(10)
        FakeWorker.delay = 25
        self.controller.start(keys)
        self.wait_until(lambda: len(FakeWorker.starts) >= 2)
        self.controller.stop()
        self.wait_until(lambda: not self.controller.busy)
        jobs = self.db.jobs(self.controller.batch_id)
        statuses = {j["shop_key"]: j["status"] for j in jobs}
        self.assertEqual(statuses[keys[0]], "completed")
        self.assertEqual(statuses[keys[-1]], "cancelled")
        self.assertLess(len(FakeWorker.starts), 10)
        self.assertFalse(any(j["status"] in ("pending", "running") for j in jobs))

    def test_account_login_waiting_continues_and_retries_separately(self):
        keys = self.add(3)
        FakeWorker.delay = 150
        FakeWorker.outcomes[keys[0]] = "needs_login"
        self.controller.start(keys, mode="account_login")
        self.wait_until(lambda: any(j["status"] == "waiting_verification" for j in self.db.jobs()))
        active = next(j for j in self.db.jobs() if j["status"] == "waiting_verification")
        self.assertIsNone(active["finished_at"])
        self.assertTrue(self.controller.busy)
        self.wait_until(lambda: not self.controller.busy)
        self.assertEqual(FakeWorker.starts, keys)
        self.assertEqual(FakeWorker.max_live, 1)
        self.assertEqual(self.controller.retry_keys("account_login"), [keys[0]])
        self.assertEqual(self.controller.retry_keys(), [])

    def test_login_confirmation_and_retry_survives_login_batch(self):
        keys = self.add(2)
        FakeWorker.outcomes[keys[0]] = "needs_login"
        self.controller.start(keys)
        self.wait_until(lambda: not self.controller.busy)
        FakeWorker.outcomes.clear()
        ready = []
        self.controller.login_ready.connect(ready.append)
        self.controller.start([keys[0]], mode="login")
        self.wait_until(lambda: bool(ready))
        self.assertTrue(self.controller.busy)
        self.controller.confirm_login()
        self.wait_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.retry_keys(), [keys[0]])
        self.controller.start(self.controller.retry_keys())
        self.wait_until(lambda: not self.controller.busy)
        self.assertEqual(self.controller.retry_keys(), [])

    def test_disabled_and_duplicate_keys_not_executed(self):
        keys = self.add(3)
        self.db.update_shops([keys[1]], enabled=0)
        self.controller.start(keys + keys)
        self.wait_until(lambda: not self.controller.busy)
        self.assertEqual(FakeWorker.starts, [keys[0], keys[2]])
        FakeWorker.starts = []
        FakeWorker.outcomes[keys[0]] = "failed"
        self.controller.start(keys + keys, mode="login_check", headless=True)
        self.wait_until(lambda: not self.controller.busy)
        self.assertEqual(FakeWorker.starts, [keys[0], keys[2]])
        jobs = {job["shop_key"]: job for job in self.db.jobs(self.controller.batch_id)}
        self.assertEqual(jobs[keys[0]]["status"], "failed")
        self.assertEqual(jobs[keys[2]]["status"], "authenticated")
        shops = {shop["shop_key"]: shop for shop in self.db.managed_shops()}
        self.assertEqual(shops[keys[0]]["login_status"], "needs_login")
        self.assertEqual(shops[keys[2]]["login_status"], "verified")
        self.assertTrue(shops[keys[0]]["login_checked_at"])
        self.assertTrue(shops[keys[2]]["login_checked_at"])
        FakeWorker.outcomes.clear()

    def test_login_check_runs_three_at_once_and_retries_transient_failure(self):
        keys = self.add(7)
        FakeWorker.delay = 30
        FakeWorker.outcomes[keys[0]] = ["retry", "authenticated"]
        self.controller.start(keys, mode="login_check", headless=True)
        self.wait_until(lambda: not self.controller.busy)
        self.assertEqual(FakeWorker.max_live, 3)
        self.assertEqual(FakeWorker.starts.count(keys[0]), 2)
        self.assertTrue(all(FakeWorker.starts.count(key) == 1 for key in keys[1:]))
        self.assertGreater(FakeWorker.starts.index(keys[0], 1),
                           max(FakeWorker.starts.index(key) for key in keys[1:]))
        self.assertTrue(all(job["status"] == "authenticated"
                            for job in self.db.jobs(self.controller.batch_id)))

    def test_import_atomic_and_edit_does_not_modify_identity(self):
        keys = self.add(1)
        self.db.bind_shop(Shop(keys[0], "神***店", "神***"))
        with self.assertRaises(CollectionError):
            self.db.add_shops([dict(shop_key="new", display_name="新店"), dict(shop_key=keys[0], display_name="重复")])
        self.assertEqual(len(self.db.managed_shops()), 1)
        self.db.update_shops(keys, display_name="新备注名", group_name="直营", max_pages=3)
        self.assertEqual(self.db.managed_shops()[0]["shop_name"], "神***店")
        self.assertEqual(self.db.managed_shops()[0]["display_name"], "新备注名")
        with self.assertRaises(CollectionError):
            self.db.bind_shop(Shop(keys[0], "另一个店铺", "不同账号"))

    def test_existing_data_migrated_and_report_filters(self):
        shop = Shop("legacy", "老店", "老账号")
        run = self.db.start_run(shop, "2026-09-01 ~ 2026-09-03", "fixture")
        record = Appeal("260828-123456789", "1234", "商品", "", "商品问题", 300, "3", "-",
                        "2026-09-02 12:00:00", "申诉通过", "查看详情", "", {})
        self.db.save_page(shop.key, run, [record], 1, 1)
        self.db.finish_run(run, "completed", "完成")
        migrated = Database(self.db.path)
        self.assertEqual(migrated.managed_shops()[0]["shop_key"], "legacy")
        self.assertEqual(migrated.query(shop_key="legacy", since="2026-09-02", until="2026-09-02")[0], 1)
        self.assertEqual(migrated.query(since="2026-09-03")[0], 0)
        self.assertEqual(migrated.query(shop_key="other")[0], 0)
        migrated.update_shops(["legacy"], display_name="新名字")
        self.assertEqual(migrated.query(keyword="新名字")[0], 1)

    def test_interrupted_jobs_recovery_is_explicit(self):
        keys = self.add(2)
        jobs = self.db.create_jobs("old", keys)
        self.db.update_job(jobs[0]["job_id"], status="running", pages=1, count=8)
        self.db.recover_interrupted_jobs()
        self.assertEqual({j["status"] for j in self.db.jobs()}, {"interrupted"})
        self.assertEqual(self.db.jobs()[-1]["row_count"], 8)
        self.assertEqual(set(self.controller.retry_keys()), set(keys))

    def test_deleted_shop_stays_deleted_and_keeps_backup_and_history(self):
        key = self.add(1)[0]
        shop = Shop(key, '历史店铺', '历史账号')
        run = self.db.start_run(shop, '2026-09-03', 'fixture')
        record = Appeal('260903-123456789', '123', '商品', '', '商品问题', 100, '1', '-',
                        '2026-09-03 12:00:00', '申诉通过', '查看详情', '', {})
        self.db.save_page(key, run, [record], 1, 1)
        self.db.finish_run(run, 'completed', '完成')
        job = self.db.create_jobs('old', [key])[0]
        self.db.update_job(job['job_id'], status='failed')
        self.db.delete_shops([key])
        restarted = Database(self.db.path)
        self.assertEqual(restarted.managed_shops(), [])
        self.assertEqual(restarted.query(shop_key=key)[0], 1)
        self.assertEqual(len(restarted.jobs()), 1)
        self.assertTrue(restarted.report_shops()[0]['deleted_at'])
        self.assertEqual(restarted.create_jobs('new', [key]), [])
        self.assertEqual(self.controller.retry_keys(), [])
        self.assertTrue(restarted.shop_exists(key))

    def test_delete_active_batch_is_rejected_atomically(self):
        keys = self.add(2)
        self.db.create_jobs('pending', [keys[1]])
        with self.assertRaises(CollectionError):
            self.db.delete_shops(keys)
        self.assertEqual(len(self.db.managed_shops()), 2)

    def test_csv_and_excel_import(self):
        rows = parse_shop_import('店铺标识,店铺名称,所属组\na,"含,逗号的店铺",直营\nb,第二家,分销')
        self.assertEqual(rows[0]["display_name"], "含,逗号的店铺")
        self.assertEqual(parse_shop_import("a\t店一\t一组\nb\t店二\t二组")[1]["group_name"], "二组")
        with self.assertRaises(ValueError):
            parse_shop_import("a,店一\na,店二")


if __name__ == "__main__":
    unittest.main()
