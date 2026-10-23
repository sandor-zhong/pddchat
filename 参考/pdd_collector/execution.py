"""Durable task execution with bounded parallel login checks."""
from uuid import uuid4

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

from .worker import CollectorWorker
from .features import task_name


LOGIN_CHECK_CONCURRENCY = 3


class ExecutionController(QObject):
    changed = pyqtSignal()
    log = pyqtSignal(str)
    login_ready = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, db, data_dir, parent=None, worker_factory=CollectorWorker):
        super().__init__(parent)
        self.db, self.data_dir, self.worker_factory = db, data_dir, worker_factory
        self.worker = None
        self.workers = {}
        self.pending = []
        self.retry_pending = []
        self.current = None
        self.batch_id = None
        self.busy = False
        self.stopping = False
        self.mode = "collect"
        self.headless = False
        self.task_options = {}

    @property
    def active_items(self):
        return [state["item"] for state in self.workers.values()]

    def start(self, keys, mode="collect", headless=False, task_options=None):
        if self.busy:
            raise RuntimeError("已有任务正在执行，请先等待结束或安全停止。")
        batch_id = uuid4().hex
        pending = self.db.create_jobs(batch_id, keys, mode)
        if not pending:
            raise ValueError("没有可执行的店铺，请勾选至少一家已启用店铺。")
        for item in pending:
            item["login_check_attempt"] = 1
        self.batch_id, self.pending = batch_id, pending
        self.retry_pending = []
        self.mode, self.headless = mode, headless
        self.task_options = dict(task_options or {})
        self.stopping, self.busy = False, True
        self.changed.emit()
        QTimer.singleShot(0, self._pump)

    def _pump(self):
        if not self.busy:
            return
        limit = LOGIN_CHECK_CONCURRENCY if self.mode == "login_check" else 1
        while not self.stopping and len(self.workers) < limit:
            if self.pending:
                item = self.pending.pop(0)
            elif self.retry_pending:
                if any(state["item"].get("login_check_attempt", 1) == 1
                       for state in self.workers.values()):
                    break
                item = self.retry_pending.pop(0)
            else:
                break
            self._start_item(item)
        self._sync_current()
        if not self.workers and (self.stopping or not self.pending and not self.retry_pending):
            self.busy = False
            self.current = None
            self.worker = None
            self.changed.emit()
            self.finished.emit()

    def _start_item(self, item):
        job_id = item["job_id"]
        attempt = item.get("login_check_attempt", 1)
        message = ("正在二次复检登录状态"
                   if self.mode == "login_check" and attempt > 1
                   else "正在打开独立浏览器会话")
        self.db.update_job(job_id, status="running", message=message)
        suffix = "（二次复检）" if self.mode == "login_check" and attempt > 1 else ""
        self.log.emit(f"[{item['display_name']}] 开始{task_name(self.mode)}{suffix}")
        worker = None
        try:
            worker = self.worker_factory(item["shop_key"], self.db.path, self.data_dir,
                item["channel"], item["max_pages"], parent=self, mode=self.mode, headless=self.headless)
            worker.task_options = {**self.task_options, "login_check_attempt": attempt}
            self.workers[worker] = {"item": item, "has_outcome": False, "retry": False}
            worker.log.connect(lambda text, current=worker: self._on_log(current, text))
            worker.progress.connect(lambda pages, count, current=worker: self._on_progress(current, pages, count))
            worker.browser_ready.connect(lambda current=worker: self._on_ready(current))
            worker.outcome.connect(lambda status, text, current=worker: self._on_outcome(current, status, text))
            if hasattr(worker, "waiting_verification"):
                worker.waiting_verification.connect(
                    lambda text, current=worker: self._on_verification(current, text))
            worker.finished.connect(lambda current=worker: self._on_finished(current))
            self._sync_current()
            self.changed.emit()
            worker.start()
        except Exception as exc:
            if worker:
                self.workers.pop(worker, None)
                worker.deleteLater()
            self.db.update_job(job_id, status="failed", message=str(exc))
            if self.mode == "login_check":
                self.db.set_login_status(item["shop_key"], "needs_login")
            self.log.emit(f"[{item['display_name']}] 启动失败：{exc}")

    def _state(self, worker):
        return self.workers.get(worker)

    def _sync_current(self):
        if self.workers:
            worker, state = next(iter(self.workers.items()))
            self.worker, self.current = worker, state["item"]
        else:
            self.worker = self.current = None

    def _on_log(self, worker, message):
        state = self._state(worker)
        if not state:
            return
        item = state["item"]
        self.log.emit(f"[{item['display_name']}] {message}")
        self.db.update_job(item["job_id"], message=message)

    def _on_progress(self, worker, pages, count):
        state = self._state(worker)
        if not state:
            return
        self.db.update_job(state["item"]["job_id"], pages=pages, count=count)
        self.changed.emit()

    def _on_ready(self, worker):
        state = self._state(worker)
        if not state or self.stopping:
            return
        item = state["item"]
        self.db.update_job(item["job_id"], message="等待手动登录并确认店铺")
        self.login_ready.emit(item["display_name"])
        self.changed.emit()

    def _on_verification(self, worker, message):
        state = self._state(worker)
        if not state:
            return
        self.db.update_job(state["item"]["job_id"], status="waiting_verification", message=message)
        self.changed.emit()

    def _on_outcome(self, worker, status, message):
        state = self._state(worker)
        if not state:
            return
        state["has_outcome"] = True
        item = state["item"]
        if status == "retry" and self.mode == "login_check" and item.get("login_check_attempt", 1) == 1:
            state["retry"] = True
            self.db.update_job(item["job_id"], message=f"首次检查异常，等待二次复检：{message}")
            self.log.emit(f"[{item['display_name']}] 首次检查异常，将在第一轮结束后二次复检：{message}")
        else:
            if status == "retry":
                status = "failed"
                message = f"二次复检仍未完成：{message}"
            self.db.update_job(item["job_id"], status=status, message=message)
            if self.mode == "login_check":
                if status == "authenticated":
                    self.db.set_login_status(item["shop_key"], "verified")
                elif status in ("failed", "needs_login", "interrupted"):
                    self.db.set_login_status(item["shop_key"], "needs_login")
            self.log.emit(f"[{item['display_name']}] {message}")
        self.changed.emit()

    def _on_finished(self, worker):
        state = self.workers.pop(worker, None)
        if not state:
            return
        item = state["item"]
        if state["retry"]:
            if self.stopping:
                self.db.update_job(item["job_id"], status="cancelled", message="登录复检已停止。")
            else:
                item["login_check_attempt"] = 2
                self.retry_pending.append(item)
        elif not state["has_outcome"]:
            self.db.update_job(item["job_id"], status="cancelled" if self.stopping else "failed",
                               message="任务已停止" if self.stopping else "工作线程退出但未返回结果")
            if self.mode == "login_check" and not self.stopping:
                self.db.set_login_status(item["shop_key"], "needs_login")
        worker.deleteLater()
        self._sync_current()
        QTimer.singleShot(0, self._pump)

    def confirm_login(self):
        if self.worker and self.mode == "login" and not self.stopping:
            self.worker.begin_collection()

    def stop(self):
        if not self.busy:
            return
        self.stopping = True
        for item in self.pending + self.retry_pending:
            self.db.update_job(item["job_id"], status="cancelled", message="队列已停止，尚未执行。")
        self.pending.clear()
        self.retry_pending.clear()
        for worker in list(self.workers):
            worker.stop()
        self.changed.emit()

    def retry_keys(self, mode="collect"):
        return [job["shop_key"] for job in self.db.latest_collection_jobs(mode)
                if job["status"] in ("failed", "needs_login", "interrupted", "partial")]
