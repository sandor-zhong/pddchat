import hashlib
import os
from pathlib import Path
from threading import Event
import traceback
from datetime import datetime
from time import monotonic
from urllib.parse import urlparse

from PyQt5.QtCore import QThread, pyqtSignal
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

from .database import Database
from .models import Cancelled
from .scraper import Collector, TARGET_URL
from .login import account_login, LoginError, VerificationTimeout
from .service_status import ServiceStatusCollector, ServiceLoginRequired, SERVICE_STATUS_URL
from .violation import ViolationLoginRequired
from .customer_service import (CustomerServiceCollector, CustomerServiceLoginRequired,
                               CUSTOMER_SERVICE_URL)
from .appeal_amount import (AppealAmountCollector, AppealAmountLoginRequired,
                            APPEAL_AMOUNT_URL)
from .work_order import (WorkOrderCollector, WorkOrderLoginRequired, WORK_ORDER_URL)


class CollectorWorker(QThread):
    log = pyqtSignal(str)
    browser_ready = pyqtSignal()
    progress = pyqtSignal(int, int)
    shop_detected = pyqtSignal(str, str, str)
    outcome = pyqtSignal(str, str)
    waiting_verification = pyqtSignal(str)

    def __init__(self, shop_key: str, database_path: Path, data_dir: Path, channel: str, max_pages: int,
                 parent=None, mode="manual", headless=False):
        super().__init__(parent)
        self.shop_key, self.database_path, self.data_dir = shop_key, database_path, data_dir
        self.channel, self.max_pages = channel, max_pages
        self.start_event, self.stop_event = Event(), Event()
        self.mode, self.headless = mode, headless
        self.task_options = {}

    def begin_collection(self):
        self.start_event.set()

    def stop(self):
        self.stop_event.set()

    def run(self):
        # Playwright and SQLite connections are created and used in this thread only.
        context = page = None
        try:
            profile_id = hashlib.sha256(self.shop_key.encode("utf-8")).hexdigest()[:24]
            profile_path = self.data_dir / "browser_profiles" / profile_id
            profile_path.mkdir(parents=True, exist_ok=True)
            with sync_playwright() as playwright:
                try:
                    self.log.emit("正在打开浏览器……")
                    if self.stop_event.is_set():
                        raise Cancelled()
                    options = dict(user_data_dir=str(profile_path), headless=self.headless and self.mode in ("collect", "service_status", "customer_service", "appeal_amount", "work_order_screenshot", "login_check"),
                                   viewport={"width": 1440, "height": 1000}, locale="zh-CN")
                    if self.channel:
                        options["channel"] = self.channel
                    try:
                        context = playwright.chromium.launch_persistent_context(**options)
                    except Exception:
                        if not self.channel or os.environ.get("PDD_BUNDLED_CHROMIUM") != "1":
                            raise
                        self.log.emit("所选 Chrome/Edge 不可用，正在切换安装包内置 Chromium……")
                        options.pop("channel", None)
                        context = playwright.chromium.launch_persistent_context(**options)
                    context.set_default_timeout(10000)
                    page = context.pages[0] if context.pages else context.new_page()
                    if self.mode == "service_status":
                        page.goto(SERVICE_STATUS_URL, wait_until="domcontentloaded", timeout=45000)
                        db = Database(self.database_path)
                        collector = ServiceStatusCollector(page, db, self.shop_key, self.stop_event,
                            log=self.log.emit, progress=self.progress.emit)
                        result = collector.collect()
                        self.outcome.emit(result.status, result.message)
                        return
                    if self.mode == "customer_service":
                        page.goto(CUSTOMER_SERVICE_URL, wait_until="domcontentloaded", timeout=45000)
                        db = Database(self.database_path)
                        collector = CustomerServiceCollector(page, db, self.shop_key, self.stop_event,
                            log=self.log.emit, progress=self.progress.emit)
                        result = collector.collect()
                        self.outcome.emit(result.status, result.message)
                        return
                    if self.mode == "appeal_amount":
                        page.goto(APPEAL_AMOUNT_URL, wait_until="domcontentloaded", timeout=45000)
                        db = Database(self.database_path)
                        collector = AppealAmountCollector(page, db, self.shop_key, self.stop_event,
                            log=self.log.emit, progress=self.progress.emit)
                        result = collector.collect()
                        self.outcome.emit(result.status, result.message)
                        return
                    if self.mode == "work_order_screenshot":
                        page.goto(WORK_ORDER_URL, wait_until="domcontentloaded", timeout=45000)
                        db = Database(self.database_path)
                        collector = WorkOrderCollector(page, db, self.shop_key, self.stop_event,
                            log=self.log.emit, progress=self.progress.emit,
                            screenshot_dir=self.data_dir / "work_order_screenshots",
                            start_date=self.task_options.get("start_date"),
                            end_date=self.task_options.get("end_date"))
                        result = collector.collect()
                        self.outcome.emit(result.status, result.message)
                        return
                    if self.mode == "account_login":
                        db = Database(self.database_path)
                        db.set_login_status(self.shop_key, "needs_login")
                        credentials = db.get_credentials(self.shop_key)
                        try:
                            account_login(page, credentials[0], credentials[1], self.stop_event,
                                          waiting=self.waiting_verification.emit)
                        finally:
                            del credentials
                        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
                        self.start_event.set()
                    else:
                        try:
                            page.goto(TARGET_URL, wait_until="domcontentloaded",
                                      timeout=15000 if self.mode == "login_check" else 45000)
                        except PlaywrightTimeout:
                            if self.mode == "login_check":
                                self.outcome.emit("retry", "页面首次加载超时")
                                return
                            raise
                    if self.mode in ("collect", "login_check"):
                        self.start_event.set()
                    elif self.mode != "account_login":
                        self.log.emit("请完成登录并核对当前店铺，然后在控制台点击“确认已登录”。")
                        self.browser_ready.emit()
                    while not self.start_event.is_set():
                        if self.stop_event.is_set():
                            raise Cancelled()
                        if page.is_closed():
                            raise RuntimeError("浏览器页面已关闭，请重新打开。")
                        page.wait_for_timeout(150)
                    if self.stop_event.is_set():
                        raise Cancelled()
                    # Login may have landed on the home page or a new tab.
                    if self.mode != "collect":
                        target_pages = [p for p in context.pages if "aftersales/customer_complain_appeal" in p.url]
                        if target_pages:
                            page = target_pages[-1]
                    if (self.mode != "login_check" and
                            "aftersales/customer_complain_appeal" not in page.url):
                        page.goto(TARGET_URL, wait_until="domcontentloaded", timeout=45000)
                    if not self.headless:
                        page.bring_to_front()
                    db = Database(self.database_path)
                    if self.mode == "login_check":
                        try:
                            authenticated = self._wait_for_login_check(page, timeout_seconds=8)
                        except PlaywrightTimeout:
                            self.outcome.emit("retry", "登录标识加载超时")
                            return
                        if not authenticated:
                            db.set_login_status(self.shop_key, "needs_login")
                            self.outcome.emit("needs_login", "登录已失效，请在店铺管理中重新登录。")
                            return
                    else:
                        try:
                            page.locator('#status').wait_for(state="visible", timeout=12000)
                        except PlaywrightTimeout:
                            if self.stop_event.is_set():
                                raise Cancelled()
                            db.set_login_status(self.shop_key, "needs_login")
                            self.outcome.emit("needs_login", "未能进入申诉列表，请在店铺管理中重新登录；批量队列继续下一家。")
                            return
                    collector = Collector(
                        page, db, self.shop_key, self.stop_event,
                        log=self.log.emit, progress=self.progress.emit,
                        shop_callback=lambda shop, dates: self.shop_detected.emit(shop.name, shop.account, dates),
                        max_pages=self.max_pages,
                    )
                    collector.check()
                    shop = collector.read_shop()
                    db.bind_shop(shop)
                    db.set_login_status(self.shop_key, "verified")
                    if self.mode in ("login", "account_login", "login_check"):
                        self.shop_detected.emit(shop.name, shop.account, collector.read_filters()[0])
                        message = (f"登录状态检查通过：{shop.name} / {shop.account}。"
                                   if self.mode == "login_check" else
                                   f"登录已验证：{shop.name} / {shop.account}；登录状态已保存。")
                        self.outcome.emit("authenticated", message)
                        return
                    result = collector.collect()
                    self.outcome.emit(result.status, result.message)
                except Cancelled:
                    self.outcome.emit("cancelled", "已停止；已入库的数据保留，下次可重新采集并自动去重。")
                except VerificationTimeout as exc:
                    self.outcome.emit("needs_login", str(exc))
                except (ServiceLoginRequired, ViolationLoginRequired, CustomerServiceLoginRequired,
                        AppealAmountLoginRequired, WorkOrderLoginRequired) as exc:
                    Database(self.database_path).set_login_status(self.shop_key, "needs_login")
                    self.outcome.emit("needs_login", str(exc))
                except LoginError as exc:
                    self.outcome.emit("failed", str(exc))
                except Exception as exc:
                    if self.stop_event.is_set():
                        self.outcome.emit("cancelled", "已停止，已入库的数据保留。")
                    elif self.mode == "login_check":
                        self.outcome.emit("retry", "页面或网络检查异常")
                    else:
                        self._diagnostics(page)
                        message = str(exc)
                        if "Executable doesn't exist" in message:
                            message = "尚未安装所选浏览器。可选择已安装的 Chrome/Edge，或执行 uv run playwright install chromium。"
                        elif self.mode == "account_login":
                            message = "账号登录或店铺核对失败，请检查账号配置并重试，或使用单店手动登录核对。"
                        self.outcome.emit("failed", message)
                finally:
                    if context:
                        try:
                            context.close()
                        except Exception:
                            pass
        except Exception as exc:
            if self.mode == "login_check":
                self.outcome.emit("retry", "浏览器启动或配置读取异常")
            else:
                self.outcome.emit("failed", f"浏览器启动失败：{exc}")

    def _wait_for_login_check(self, page, timeout_seconds=8):
        """Return quickly for a known authenticated or logged-out page; retry ambiguous pages."""
        deadline = monotonic() + timeout_seconds
        status = page.locator("#status")
        login_controls = page.locator(
            '#usernameId, #passwordId, [data-tracking-click-viewid="password_log"]')
        while monotonic() < deadline:
            if self.stop_event.is_set():
                raise Cancelled()
            if page.is_closed():
                raise PlaywrightTimeout("page closed during login check")
            if status.count() and status.first.is_visible():
                return True
            location = urlparse(page.url)
            if (location.path.startswith("/login") or
                    any(login_controls.nth(index).is_visible()
                        for index in range(login_controls.count()))):
                return False
            page.wait_for_timeout(200)
        raise PlaywrightTimeout("login check timed out")

    def _diagnostics(self, page):
        if self.mode == "account_login":
            return  # Login traces and screenshots can contain credentials or verification information.
        folder = self.data_dir / "diagnostics" / datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        try:
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "error.txt").write_text(traceback.format_exc(), encoding="utf-8")
            if page and not page.is_closed():
                # Save only the visible page, never cookies or browser storage in diagnostics.
                page.screenshot(path=str(folder / "page.png"), full_page=True, timeout=5000)
            self.log.emit(f"错误详情已保存：{folder}")
        except Exception:
            pass
