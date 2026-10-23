"""QMainWindow：左侧导航和各业务页面。"""

from __future__ import annotations

import time

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QStackedWidget,
    QWidget,
)

from .pages.auxiliary_queries_page import AuxiliaryQueriesPage
from .pages.dashboard_page import DashboardPage
from .pages.reports_page import ReportsPage
from .pages.run_page import RunPage
from .pages.shops_page import ShopsPage
from .services import msgbox
from .services.project_paths import PROJECT_PATHS

_NAV_ITEMS = ["总览仪表盘", "店铺管理", "运行控制", "申诉数据报表", "辅助查询功能"]

# 关闭时等待批量任务优雅停止的最长时间——超过这个时长会再问一次要不要
# 强制关闭（接受孤儿进程风险）还是继续等。
_SHUTDOWN_WAIT_TIMEOUT_SEC = 30.0
_SHUTDOWN_POLL_INTERVAL_MS = 500


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("拼多多申诉工具 — 运营控制台")
        self.resize(1200, 800)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QHBoxLayout(central)

        self._nav = QListWidget()
        self._nav.setFixedWidth(160)
        for text in _NAV_ITEMS:
            QListWidgetItem(text, self._nav)
        layout.addWidget(self._nav)

        self._stack = QStackedWidget()
        self._dashboard_page = DashboardPage()
        self._shops_page = ShopsPage()
        self._run_page = RunPage()
        self._reports_page = ReportsPage()
        self._auxiliary_page = AuxiliaryQueriesPage()
        for page in (
            self._dashboard_page,
            self._shops_page,
            self._run_page,
            self._reports_page,
            self._auxiliary_page,
        ):
            self._stack.addWidget(page)
        layout.addWidget(self._stack, stretch=1)

        self._nav.currentRowChanged.connect(self._on_navigation_changed)
        self._nav.setCurrentRow(0)

        self.statusBar().showMessage(f"主项目目录：{PROJECT_PATHS.root}")

        # 关闭确认+优雅停止用：轮询定时器存在且在跑，本身就是"正在等待
        # 停止完成"这个状态的标志，不额外加一个布尔标志位。
        self._shutdown_poll_timer: QTimer | None = None
        self._shutdown_wait_started: float = 0.0

    def _on_navigation_changed(self, index: int) -> None:
        self._stack.setCurrentIndex(index)
        self._reports_page.set_polling_active(index == 3)
        self._auxiliary_page.set_active(index == 4)

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 覆写方法命名规范)
        if self._auxiliary_page.is_exporting():
            self.statusBar().showMessage("辅助查询结果正在导出，请完成后再关闭程序。")
            event.ignore()
            return
        if self._shops_page._login_check is not None:
            self._shops_page.stop_login_check()
            self.statusBar().showMessage("正在停止登录检测，请完成后再关闭程序。")
            event.ignore()
            return
        if self._shops_page._batch_is_running():
            self._shops_page._stop_batch_login()
            self.statusBar().showMessage("正在停止批量登录并关闭浏览器，请完成后再关闭程序。")
            event.ignore()
            return
        if self._run_page.is_exporting_captcha_diagnostics():
            self.statusBar().showMessage("验证码诊断包正在导出，请完成后再关闭窗口。")
            event.ignore()
            return
        if (
            self._shutdown_poll_timer is not None
            and self._shutdown_poll_timer.isActive()
        ):
            # 正在等待优雅停止的过程中用户又点了一次关闭（比如等得不耐烦
            # 又点了一次 X）——直接忽略，不重复弹确认框，等轮询自己收尾。
            event.ignore()
            return

        if (
            not self._run_page.is_running()
        ):
            event.accept()
            return

        event.ignore()
        confirmed = msgbox.confirm_yes_no(
            self,
            "确认关闭",
            "当前有批量任务正在运行，直接关闭可能导致浏览器进程没有被正常清理。"
            "选「仍然关闭」会先尝试优雅停止（不打断正在处理的订单，最多等待 30 秒），"
            "停止完成后才会真正关闭窗口。",
            yes_text="仍然关闭",
            no_text="取消",
        )
        if not confirmed:
            return

        self._run_page.begin_shutdown_stop()
        self.statusBar().showMessage("正在停止批量任务，请稍候……")
        self._begin_shutdown_poll()

    def _begin_shutdown_poll(self) -> None:
        self._shutdown_wait_started = time.monotonic()
        timer = QTimer(self)
        timer.timeout.connect(self._on_shutdown_poll)
        self._shutdown_poll_timer = timer
        timer.start(_SHUTDOWN_POLL_INTERVAL_MS)

    def _stop_shutdown_poll(self) -> None:
        if self._shutdown_poll_timer is not None:
            self._shutdown_poll_timer.stop()
            self._shutdown_poll_timer = None

    def _on_shutdown_poll(self) -> None:
        if (
            not self._run_page.is_running()
        ):
            self._stop_shutdown_poll()
            self.close()  # 重新触发 closeEvent，这次 is_running() 是 False，会走正常关闭
            return

        elapsed = time.monotonic() - self._shutdown_wait_started
        if elapsed < _SHUTDOWN_WAIT_TIMEOUT_SEC:
            return

        self._stop_shutdown_poll()
        force = msgbox.confirm_yes_no(
            self,
            "仍在停止中",
            "已经等待 30 秒，批量任务还没有完全停止。"
            "选「强制关闭」会直接结束程序，可能留下没清理干净的浏览器进程，"
            "需要之后手动在任务管理器里检查；选「继续等待」会再等 30 秒。",
            yes_text="强制关闭",
            no_text="继续等待",
        )
        if force:
            QApplication.quit()
            return

        self._begin_shutdown_poll()
