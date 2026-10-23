"""C. 运行控制：启动/停止批量处理表单 + 实时状态 + 真实提交二次确认。

跟 scheduler_core.Scheduler 的集成走 QThread + SchedulerWorker
（workers/scheduler_thread.py）——本页只负责收集表单参数、展示
Scheduler 广播出来的事件（SchedulerEvent），不直接碰任何调度逻辑。
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime

from PyQt5.QtCore import Qt, QDateTime, QThread, QTime, QTimer, pyqtSignal
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QFrame,
    QFileDialog,
    QMessageBox,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

import aftersale_result_settings
import browser_settings
import memory_monitor
import result_query_settings
import scheduler_core  # 主项目根目录已被 project_paths 加进 sys.path


class CaptchaExportWorker(QThread):
    result_ready = pyqtSignal(object)

    def __init__(self, root, destination, parent=None):
        super().__init__(parent)
        self.root, self.destination = root, destination

    def run(self):
        import captcha_diagnostics
        try:
            self.result_ready.emit(captcha_diagnostics.export_bundle(self.root, self.destination))
        except Exception as exc:
            self.result_ready.emit(str(exc))

from ..dialogs.confirm_submit_dialog import confirm_real_submit
from ..services import (
    appeal_result_schedule,
    auto_schedule,
    msgbox,
    rest_settings,
    shop_repo,
)
from ..services.app_logging import LOG_DIR, logger
from ..services.project_paths import PROJECT_PATHS
from ..workers.aftersale_appeal_worker import AftersaleAppealWorker
from ..workers.aftersale_result_check_worker import AftersaleResultCheckWorker
from ..workers.appeal_result_check_worker import AppealResultCheckWorker
from ..workers.db_poll_worker import DbPoller
from ..workers.reconsideration_result_check_worker import (
    ReconsiderationResultCheckWorker,
)
from ..workers.reconsideration_worker import ReconsiderationWorker
from ..workers.scheduler_thread import SchedulerWorker
from .pipeline_ui import PipelineMixin

# 定时自动运行：每隔这么久检查一次"现在该不该在跑"，不需要精确到秒。
_AUTO_SCHEDULE_CHECK_INTERVAL_MS = 30_000

_LOG_MAX_BLOCKS = 4000
_LOG_VIEW_MAX_CHARS_PER_EVENT = 20_000
_LOG_FILE_MAX_CHARS_PER_EVENT = 100_000


def _limit_log_text(text: object, limit: int) -> str:
    """限制单条日志总长度；行数上限无法约束一条超长 Base64/HTML。"""
    value = str(text)
    if len(value) <= limit:
        return value
    omitted = len(value) - limit
    return value[:limit] + f"\n…[该条日志过长，已省略 {omitted} 个字符]"

# SchedulerEvent.type -> 用来更新"当前在跑"迷你表格那一行"状态"列的文案；
# 没列出的事件类型（round_start/round_complete/idle_wait/interrupted/
# closing_start/closed_all 之类跟具体某家店铺无关的事件）只进日志区，
# 不改这张表。
_ROW_STATUS_TEXT = {
    "worker_init_start": "正在启动浏览器…",
    "worker_ready": "已就绪",
    "worker_empty_queue": "暂无可处理订单",
    "order_turn": "处理中…",
    "worker_done": "本轮已完成",
    "worker_comm_lost": "通信中断，已关闭",
    "worker_init_failed": "启动失败",
    "worker_too_many_failures": "连续失败过多，已关闭",
}


class RunPage(PipelineMixin, QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._pipeline_active = False
        self._pipeline_cancelled = False

        self._thread: QThread | None = None
        self._worker: SchedulerWorker | None = None
        self._row_index: dict[str, int] = {}  # shop -> 迷你表格行号
        self._browser_buttons: dict[str, QPushButton] = {}
        self._topmost_shop: str | None = None
        self._stopping = False  # 已经发出优雅停止请求、还没收到 stopped 信号
        self._run_shops: list[str] = []
        self._run_terminal_shops: set[str] = set()
        self._run_failed_shops: set[str] = set()
        self._run_initial_totals: dict[str, int] = {}
        self._run_remaining: dict[str, int] = {}
        self._run_processed_orders = 0
        self._run_started_monotonic: float | None = None
        self._run_finished_elapsed: float | None = None
        self._run_eta_seconds: float | None = None
        self._run_progress_max_seen = 0.0
        self._run_checked_by_shop: dict[str, int] = {}
        self._task_shop_started_at: dict[str, float] = {}
        self._task_order_started_at: dict[str, float] = {}
        self._max_log_event_chars_seen = 0

        # "查询申诉结果"按钮背后的状态——跟上面的批量任务（_thread/_worker）
        # 是两套完全独立的 QThread/worker，不复用彼此的字段。
        self._check_results_thread: QThread | None = None
        self._check_results_worker: AppealResultCheckWorker | None = None
        self._check_results_total_updated = 0
        self._check_results_skipped: dict[str, str] = {}
        self._check_results_errors: dict[str, str] = {}
        self._result_check_skipped: dict[str, str] = {}
        self._result_check_issues: dict[str, str] = {}
        self._check_results_lock_id: str | None = None
        self._check_results_source = "单次"

        self._check_reconsider_results_thread: QThread | None = None
        self._check_reconsider_results_worker: ReconsiderationResultCheckWorker | None = None
        self._check_reconsider_results_lock_id: str | None = None
        self._check_reconsider_results_updated = 0
        self._check_reconsider_result_counts: dict[str, int] = {}

        self._check_aftersale_results_thread: QThread | None = None
        self._check_aftersale_results_worker: AftersaleResultCheckWorker | None = None
        self._check_aftersale_results_lock_id: str | None = None
        self._check_aftersale_results_updated = 0
        self._check_aftersale_result_counts: dict[str, int] = {}

        self._reconsider_thread: QThread | None = None
        self._reconsider_worker: ReconsiderationWorker | None = None
        self._reconsider_lock_id: str | None = None
        self._reconsider_processed = 0
        self._reconsider_submitted = 0
        self._reconsider_pending_confirmation = 0
        self._reconsider_failed = 0
        self._reconsider_stopping = False

        self._aftersale_thread: QThread | None = None
        self._aftersale_worker: AftersaleAppealWorker | None = None
        self._aftersale_lock_id: str | None = None
        self._aftersale_processed = 0
        self._aftersale_succeeded = 0
        self._aftersale_failed = 0
        self._aftersale_stopping = False

        # 定时自动运行的设置：先从磁盘读（重开 GUI 后延续上次的开关/时间），
        # _build_form_group() 会用这几个值初始化对应控件的初始状态。
        self._auto_settings = auto_schedule.load_settings()
        self._auto_started_date: date | None = None  # 今天有没有已经自动启动过

        # 查询申诉结果有两个入口：按钮单次执行，以及每天固定时间自动执行。
        # 两者最终共用 _start_check_results()，不会形成两套互相绕开的并发状态。
        self._check_result_schedule_settings = appeal_result_schedule.load_settings()
        try:
            self._check_results_started_date = date.fromisoformat(
                self._check_result_schedule_settings.last_run_date
            )
        except ValueError:
            self._check_results_started_date = None

        # 拟人休息设置：同样先从磁盘读，同样由 _build_form_group() 用来
        # 初始化对应控件。
        self._rest_settings = rest_settings.load_settings()

        self.setObjectName("runPage")
        self.setStyleSheet(
            """
            QWidget#runPage {
                background: #f5f7fb;
                color: #1f2937;
            }
            QWidget#runPage QLabel,
            QWidget#runPage QCheckBox,
            QWidget#runPage QSpinBox,
            QWidget#runPage QTimeEdit,
            QWidget#runPage QListWidget,
            QWidget#runPage QTableWidget,
            QWidget#runPage QPlainTextEdit {
                color: #1f2937;
            }
            QGroupBox {
                background: white;
                border: 1px solid #dfe3eb;
                border-radius: 8px;
                margin-top: 10px;
                padding: 10px;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 5px;
                color: #344054;
                font-weight: 600;
            }
            QTabWidget::pane {
                background: white;
                border: 1px solid #dfe3eb;
                border-radius: 0 8px 8px 8px;
                top: -1px;
            }
            QTabBar::tab {
                background: #e9edf5;
                border: 1px solid #dfe3eb;
                padding: 8px 24px;
                min-width: 90px;
            }
            QTabBar::tab:selected {
                background: white;
                color: #2563eb;
                font-weight: 600;
                border-bottom-color: white;
            }
            QPushButton {
                color: #344054;
                min-height: 28px;
                padding: 0 14px;
                border: 1px solid #cbd2dc;
                border-radius: 5px;
                background: #ffffff;
            }
            QPushButton:hover { background: #f2f5fa; }
            QPushButton:disabled {
                color: #98a2b3;
                background: #eaecf0;
                border-color: #d0d5dd;
            }
            QPushButton#primaryAction {
                color: white;
                background: #2563eb;
                border-color: #2563eb;
                font-weight: 600;
                min-width: 110px;
            }
            QPushButton#primaryAction:hover { background: #1d4ed8; }
            QPushButton#primaryAction:disabled {
                color: #98a2b3;
                background: #e4e7ec;
                border-color: #d0d5dd;
            }
            QPushButton#dangerAction {
                color: #b42318;
                border-color: #f0b7b2;
                background: #fff7f6;
                min-width: 90px;
            }
            QPushButton#dangerAction:disabled {
                color: #98a2b3;
                background: #eaecf0;
                border-color: #d0d5dd;
            }
            QLabel#pageSubtitle, QLabel#mutedText { color: #667085; }
            QLabel#statusChip {
                color: #475467;
                background: #f2f4f7;
                border-radius: 5px;
                padding: 5px 8px;
            }
            QFrame#advancedPanel {
                background: #f8fafc;
                border: 1px solid #e5e9f0;
                border-radius: 6px;
            }
            QListWidget, QTableWidget, QPlainTextEdit {
                background: white;
                border: 1px solid #d7dce5;
                border-radius: 5px;
            }
            QProgressBar {
                min-height: 18px;
                color: #344054;
                text-align: center;
                background: #eef2f7;
                border: 1px solid #d7dce5;
                border-radius: 6px;
            }
            QProgressBar::chunk {
                background: #3b82f6;
                border-radius: 5px;
            }
            """
        )

        root = QVBoxLayout(self)
        root.setContentsMargins(18, 14, 18, 16)
        root.setSpacing(10)

        header_row = QHBoxLayout()
        header_text = QVBoxLayout()
        header = QLabel("运行控制")
        header_font = header.font()
        header_font.setPointSize(14)
        header_font.setBold(True)
        header.setFont(header_font)
        header_text.addWidget(header)
        subtitle = QLabel("选择任务类型并配置必要参数，高级设置按需展开")
        subtitle.setObjectName("pageSubtitle")
        header_text.addWidget(subtitle)
        header_row.addLayout(header_text)
        header_row.addStretch(1)
        open_log_button = QPushButton("打开日志文件夹")
        open_log_button.setToolTip("打开按天保存的完整运行日志。")
        open_log_button.clicked.connect(self._on_open_log_folder_clicked)
        header_row.addWidget(open_log_button)
        diagnostic_button = QPushButton("导出内存诊断包")
        diagnostic_button.setToolTip("仅导出内存诊断记录，不包含登录资料、数据库或订单正文。")
        diagnostic_button.clicked.connect(self._on_export_memory_diagnostics)
        header_row.addWidget(diagnostic_button)
        self._captcha_export_button = QPushButton("导出验证码诊断包")
        self._captcha_export_button.setToolTip("导出字符/点选自动通过统计及失败图片、元素 HTML、识别过程。")
        self._captcha_export_button.clicked.connect(self._on_export_captcha_diagnostics)
        header_row.addWidget(self._captcha_export_button)
        root.addLayout(header_row)

        splitter = QSplitter(Qt.Vertical)
        root.addWidget(splitter, stretch=1)

        splitter.addWidget(self._build_form_group())
        splitter.addWidget(self._build_status_group())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 410])

        # 店铺勾选列表实时跟 B 页的增删/停用/登录状态同步——跟 A/B 两页
        # 用的是同一套 DbPoller（4 秒轮询 shop_repo.list_shop_summaries），
        # 不需要用户手动刷新或者等点"启动"才看到最新列表。
        self._shop_poller = DbPoller(fetch=shop_repo.list_shop_summaries, interval_ms=4000)
        self._shop_poller.data_ready.connect(self._on_shop_data)
        self._shop_poller.fetch_error.connect(self._on_shop_fetch_error)
        self._shop_poller.start()

        # 定时自动运行：每 30 秒检查一次"现在该不该在跑"，立刻先查一次
        # 不用等第一个 30 秒（比如 GUI 重开时刚好落在窗口内，应该马上动）。
        self._auto_schedule_timer = QTimer(self)
        self._auto_schedule_timer.setInterval(_AUTO_SCHEDULE_CHECK_INTERVAL_MS)
        self._auto_schedule_timer.timeout.connect(self._check_auto_schedule)
        self._auto_schedule_timer.timeout.connect(self._check_result_schedule)
        self._auto_schedule_timer.start()
        self._run_progress_timer = QTimer(self)
        self._run_progress_timer.setInterval(1000)
        self._run_progress_timer.timeout.connect(self._refresh_run_progress)
        self._run_progress_timer.start()
        self._check_auto_schedule()
        self._check_result_schedule()

    # ---- 构建 UI ----

    def _build_form_group(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        shop_group = QGroupBox("参与店铺")
        shop_layout = QVBoxLayout(shop_group)
        shop_layout.setSpacing(6)
        shop_toolbar = QHBoxLayout()
        self._shop_selection_label = QLabel("已选择 0 家")
        self._shop_selection_label.setObjectName("mutedText")
        shop_toolbar.addWidget(self._shop_selection_label)
        shop_toolbar.addStretch(1)
        select_all_btn = QPushButton("全选")
        select_none_btn = QPushButton("全不选")
        select_all_btn.clicked.connect(lambda: self._set_all_shops_checked(True))
        select_none_btn.clicked.connect(lambda: self._set_all_shops_checked(False))
        shop_toolbar.addWidget(select_all_btn)
        shop_toolbar.addWidget(select_none_btn)
        shop_layout.addLayout(shop_toolbar)

        self._shop_list = QListWidget()
        self._shop_list.setMaximumHeight(72)
        self._shop_list.itemChanged.connect(self._update_shop_selection_label)
        shop_layout.addWidget(self._shop_list)
        layout.addWidget(shop_group)

        self._task_tabs = QTabWidget()
        self._task_tabs.setDocumentMode(True)
        self._task_tabs.addTab(self._build_batch_task_tab(), "负向申诉")
        self._task_tabs.addTab(self._build_reconsider_task_tab(), "发起复议")
        self._task_tabs.addTab(self._build_aftersale_task_tab(), "售后申诉")
        self._task_tabs.addTab(self._build_result_task_tab(), "结果回查")
        self._pipeline_tab_index = self._task_tabs.addTab(self._build_pipeline_tab(), "流水线运行")
        layout.addWidget(self._task_tabs)
        return container

    def _build_batch_task_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(9)

        hint = QLabel("自动采集、分析并提交可申诉订单，勾选店铺跑完一轮后停止。")
        hint.setObjectName("mutedText")
        layout.addWidget(hint)

        settings_row = QHBoxLayout()
        settings_row.addWidget(QLabel("同时运行店铺数"))
        self._pool_size_spin = QSpinBox()
        self._pool_size_spin.setRange(1, 10)
        self._pool_size_spin.setValue(scheduler_core.DEFAULT_MAX_CONCURRENT)
        self._pool_size_spin.setSuffix(" 家")
        settings_row.addWidget(self._pool_size_spin)
        settings_row.addSpacing(8)

        self._auto_status_label = QLabel()
        self._auto_status_label.setObjectName("statusChip")
        settings_row.addWidget(self._auto_status_label)
        settings_row.addStretch(1)

        advanced_active = self._auto_settings.enabled or self._rest_settings.enabled
        advanced_button = QPushButton(
            "高级设置（已启用） ▸" if advanced_active else "高级设置 ▸"
        )
        advanced_button.setCheckable(True)
        advanced_button.setChecked(False)
        advanced_button.setMaximumWidth(155)
        settings_row.addWidget(advanced_button)
        layout.addLayout(settings_row)

        self._native_properties_check = QCheckBox("保留浏览器原生属性")
        self._native_properties_check.setChecked(browser_settings.native_properties_enabled())
        self._native_properties_check.setToolTip(
            "不注入属性伪装，不隐藏 webdriver。所有新启动的业务浏览器生效；"
            "已打开的浏览器需停止任务并重新启动。设置自动保存。"
        )
        self._native_properties_check.toggled.connect(self._save_native_properties)
        layout.addWidget(self._native_properties_check)

        engine_row = QHBoxLayout()
        engine_row.addWidget(QLabel("任务浏览器"))
        self._browser_engine_combo = QComboBox()
        self._browser_engine_combo.addItem("Chrome / Chromium", "chromium")
        self._browser_engine_combo.addItem("Firefox", "firefox")
        self._browser_engine_combo.setCurrentIndex(
            self._browser_engine_combo.findData(browser_settings.browser_engine())
        )
        self._browser_engine_combo.setToolTip(
            "先停止任务和登录窗口再切换。Firefox 使用独立登录目录，首次切换需要重新登录店铺。"
        )
        engine_row.addWidget(self._browser_engine_combo)
        self._browser_engine_hint = QLabel()
        self._browser_engine_hint.setWordWrap(True)
        engine_row.addWidget(self._browser_engine_hint, 1)
        layout.addLayout(engine_row)

        browser_row = QHBoxLayout()
        browser_row.addWidget(QLabel("本地 Google Chrome"))
        self._chrome_path_edit = QLineEdit(browser_settings.chrome_executable())
        self._chrome_path_edit.setReadOnly(True)
        self._chrome_path_edit.setPlaceholderText("请选择 chrome.exe；已有浏览器需重启后生效")
        browser_row.addWidget(self._chrome_path_edit, 1)
        choose_chrome = QPushButton("选择 exe…")
        choose_chrome.clicked.connect(self._choose_chrome_executable)
        browser_row.addWidget(choose_chrome)
        clear_chrome = QPushButton("恢复默认")
        clear_chrome.clicked.connect(lambda: self._set_chrome_executable(""))
        browser_row.addWidget(clear_chrome)
        self._chrome_controls = (self._chrome_path_edit, choose_chrome, clear_chrome)
        layout.addLayout(browser_row)
        self._update_browser_controls()
        self._browser_engine_combo.currentIndexChanged.connect(self._save_browser_engine)

        advanced_panel = QFrame()
        advanced_panel.setObjectName("advancedPanel")
        advanced_layout = QVBoxLayout(advanced_panel)
        advanced_layout.setContentsMargins(12, 10, 12, 10)

        auto_row = QHBoxLayout()
        self._auto_enable_check = QCheckBox("每日自动运行")
        self._auto_enable_check.setChecked(self._auto_settings.enabled)
        auto_row.addWidget(self._auto_enable_check)
        auto_row.addWidget(QLabel("时间"))
        self._auto_start_edit = QTimeEdit()
        self._auto_start_edit.setDisplayFormat("HH:mm")
        self._auto_start_edit.setTime(
            QTime.fromString(self._auto_settings.start_time, "HH:mm")
        )
        auto_row.addWidget(self._auto_start_edit)
        auto_row.addWidget(QLabel("至"))
        self._auto_end_edit = QTimeEdit()
        self._auto_end_edit.setDisplayFormat("HH:mm")
        self._auto_end_edit.setTime(
            QTime.fromString(self._auto_settings.end_time, "HH:mm")
        )
        auto_row.addWidget(self._auto_end_edit)
        auto_row.addStretch(1)
        advanced_layout.addLayout(auto_row)

        rest_row = QHBoxLayout()
        self._rest_enable_check = QCheckBox("自动休息")
        self._rest_enable_check.setChecked(self._rest_settings.enabled)
        rest_row.addWidget(self._rest_enable_check)
        rest_row.addWidget(QLabel("连续运行"))
        self._rest_min_hours_spin = QSpinBox()
        self._rest_min_hours_spin.setRange(1, 12)
        self._rest_min_hours_spin.setValue(self._rest_settings.min_hours)
        rest_row.addWidget(self._rest_min_hours_spin)
        rest_row.addWidget(QLabel("–"))
        self._rest_max_hours_spin = QSpinBox()
        self._rest_max_hours_spin.setRange(1, 12)
        self._rest_max_hours_spin.setValue(self._rest_settings.max_hours)
        rest_row.addWidget(self._rest_max_hours_spin)
        rest_row.addWidget(QLabel("小时，休息"))
        self._rest_min_minutes_spin = QSpinBox()
        self._rest_min_minutes_spin.setRange(1, 120)
        self._rest_min_minutes_spin.setValue(self._rest_settings.min_minutes)
        rest_row.addWidget(self._rest_min_minutes_spin)
        rest_row.addWidget(QLabel("–"))
        self._rest_max_minutes_spin = QSpinBox()
        self._rest_max_minutes_spin.setRange(1, 120)
        self._rest_max_minutes_spin.setValue(self._rest_settings.max_minutes)
        rest_row.addWidget(self._rest_max_minutes_spin)
        rest_row.addWidget(QLabel("分钟"))
        rest_row.addStretch(1)
        advanced_layout.addLayout(rest_row)
        layout.addWidget(advanced_panel)

        def toggle_advanced(checked: bool) -> None:
            advanced_panel.setVisible(checked)
            active = (
                self._auto_enable_check.isChecked()
                or self._rest_enable_check.isChecked()
            )
            suffix = "（已启用）" if active else ""
            advanced_button.setText(
                f"高级设置{suffix} {'▾' if checked else '▸'}"
            )

        advanced_button.toggled.connect(toggle_advanced)
        toggle_advanced(advanced_button.isChecked())

        self._rest_min_hours_spin.valueChanged.connect(
            self._rest_max_hours_spin.setMinimum
        )
        self._rest_max_hours_spin.valueChanged.connect(
            self._rest_min_hours_spin.setMaximum
        )
        self._rest_min_minutes_spin.valueChanged.connect(
            self._rest_max_minutes_spin.setMinimum
        )
        self._rest_max_minutes_spin.valueChanged.connect(
            self._rest_min_minutes_spin.setMaximum
        )
        self._rest_enable_check.stateChanged.connect(self._on_rest_settings_changed)
        self._rest_enable_check.stateChanged.connect(
            lambda _state: toggle_advanced(advanced_button.isChecked())
        )
        self._rest_min_hours_spin.valueChanged.connect(self._on_rest_settings_changed)
        self._rest_max_hours_spin.valueChanged.connect(self._on_rest_settings_changed)
        self._rest_min_minutes_spin.valueChanged.connect(self._on_rest_settings_changed)
        self._rest_max_minutes_spin.valueChanged.connect(self._on_rest_settings_changed)
        self._auto_enable_check.stateChanged.connect(self._on_auto_enable_toggled)
        self._auto_enable_check.stateChanged.connect(
            lambda _state: toggle_advanced(advanced_button.isChecked())
        )
        self._auto_start_edit.timeChanged.connect(self._on_auto_time_changed)
        self._auto_end_edit.timeChanged.connect(self._on_auto_time_changed)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self._stop_button = QPushButton("优雅停止")
        self._stop_button.setObjectName("dangerAction")
        self._stop_button.setEnabled(False)
        self._stop_button.clicked.connect(self._on_stop_clicked)
        self._start_button = QPushButton("开始负向申诉")
        self._start_button.setObjectName("primaryAction")
        self._start_button.clicked.connect(self._on_start_clicked)
        action_row.addWidget(self._stop_button)
        action_row.addWidget(self._start_button)
        layout.addLayout(action_row)
        layout.addStretch(1)
        return tab

    def _build_result_task_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        hint = QLabel("启用提交时间范围后，只回查范围内记录；负向申诉优先查详情，复议和售后查齐后结束翻页。")
        hint.setObjectName("mutedText")
        layout.addWidget(hint)

        settings = result_query_settings.load_settings()
        time_row = QHBoxLayout()
        self._result_since_enabled = QCheckBox("按提交时间查询（三类结果共用）")
        self._result_since_enabled.setChecked(settings["enabled"])
        self._result_since_enabled.setToolTip("已知提交时间更早的记录跳过；缺少提交时间的历史记录仍保留查询，避免漏查。")
        time_row.addWidget(self._result_since_enabled)
        time_row.addWidget(QLabel("从"))
        self._result_since_edit = QDateTimeEdit()
        self._result_since_edit.setCalendarPopup(True)
        self._result_since_edit.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        local_time = result_query_settings.parse_time(settings["submitted_since"])
        self._result_since_edit.setDateTime(QDateTime.fromString(local_time.strftime("%Y-%m-%d %H:%M:%S"), "yyyy-MM-dd HH:mm:ss"))
        self._result_since_edit.setEnabled(settings["enabled"])
        time_row.addWidget(self._result_since_edit)
        time_row.addWidget(QLabel("起（北京时间，含该时间；自动保存）"))
        time_row.addStretch()
        layout.addLayout(time_row)
        self._result_since_enabled.toggled.connect(self._on_result_since_changed)
        self._result_since_edit.dateTimeChanged.connect(self._on_result_since_changed)

        schedule_row = QHBoxLayout()
        self._check_result_schedule_enable = QCheckBox("每日自动查询")
        self._check_result_schedule_enable.setChecked(
            self._check_result_schedule_settings.enabled
        )
        schedule_row.addWidget(self._check_result_schedule_enable)
        schedule_row.addWidget(QLabel("执行时间"))
        self._check_result_schedule_time = QTimeEdit()
        self._check_result_schedule_time.setDisplayFormat("HH:mm")
        self._check_result_schedule_time.setTime(
            QTime.fromString(self._check_result_schedule_settings.run_time, "HH:mm")
        )
        schedule_row.addWidget(self._check_result_schedule_time)
        schedule_row.addStretch(1)
        self._check_results_button = QPushButton("立即查询申诉结果")
        self._check_results_button.setObjectName("primaryAction")
        self._check_results_button.setToolTip(
            "立即对所选店铺执行一次结果查询，并避让其它运行任务。"
        )
        self._check_results_button.clicked.connect(self._on_check_results_clicked)
        schedule_row.addWidget(self._check_results_button)
        self._check_reconsider_results_button = QPushButton("批量查询复议结果")
        self._check_reconsider_results_button.setObjectName("primaryAction")
        self._check_reconsider_results_button.setToolTip(
            "按所选提交时间范围匹配本地复议记录，更新独立的复议状态。"
        )
        self._check_reconsider_results_button.clicked.connect(
            self._on_check_reconsider_results_clicked
        )
        schedule_row.addWidget(self._check_reconsider_results_button)
        self._check_aftersale_results_button = QPushButton("批量查询售后结果")
        self._check_aftersale_results_button.setObjectName("primaryAction")
        self._check_aftersale_results_button.setToolTip(
            "查询售后维权申诉审核状态；部分通过取详情页返还金额，其余取列表金额。"
        )
        self._check_aftersale_results_button.clicked.connect(
            self._on_check_aftersale_results_clicked
        )
        schedule_row.addWidget(self._check_aftersale_results_button)
        layout.addLayout(schedule_row)

        interval_row = QHBoxLayout()
        interval_row.addWidget(QLabel("售后结果查询间隔"))
        self._aftersale_query_interval_spin = QSpinBox()
        self._aftersale_query_interval_spin.setRange(
            aftersale_result_settings.MIN_QUERY_INTERVAL_SECONDS,
            aftersale_result_settings.MAX_QUERY_INTERVAL_SECONDS,
        )
        self._aftersale_query_interval_spin.setSuffix(" 秒")
        self._aftersale_query_interval_spin.setValue(aftersale_result_settings.load_query_interval())
        self._aftersale_query_interval_spin.setToolTip("每次翻页、打开详情及切换店铺前等待，修改后自动保存。")
        self._aftersale_query_interval_spin.valueChanged.connect(self._on_aftersale_query_interval_changed)
        interval_row.addWidget(self._aftersale_query_interval_spin)
        interval_hint = QLabel("翻页、打开详情及切换店铺前等待；下次查询生效")
        interval_hint.setObjectName("mutedText")
        interval_row.addWidget(interval_hint)
        interval_row.addStretch(1)
        layout.addLayout(interval_row)

        self._check_result_schedule_status = QLabel()
        self._check_result_schedule_status.setObjectName("statusChip")
        layout.addWidget(self._check_result_schedule_status)
        self._check_result_schedule_enable.stateChanged.connect(
            self._on_check_result_schedule_changed
        )
        self._check_result_schedule_time.timeChanged.connect(
            self._on_check_result_schedule_changed
        )
        self._update_check_result_schedule_status()

        layout.addStretch(1)
        return tab

    def _build_reconsider_task_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        hint = QLabel(
            "扫描全部可复议订单；已有材料直接复用，材料缺失时自动采集聊天并生成。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        limit_row = QHBoxLayout()
        limit_row.addWidget(QLabel("同时运行店铺数"))
        self._reconsider_pool_size_spin = QSpinBox()
        self._reconsider_pool_size_spin.setRange(1, 10)
        self._reconsider_pool_size_spin.setValue(
            scheduler_core.DEFAULT_MAX_CONCURRENT
        )
        self._reconsider_pool_size_spin.setSuffix(" 家")
        self._reconsider_pool_size_spin.setToolTip(
            "最多保持这些店铺的浏览器打开；订单操作仍然一次只执行一家。"
        )
        limit_row.addWidget(self._reconsider_pool_size_spin)
        limit_row.addSpacing(16)
        limit_row.addWidget(QLabel("每家店铺本次最多复议"))
        self._reconsider_limit_spin = QSpinBox()
        self._reconsider_limit_spin.setRange(1, 999)
        self._reconsider_limit_spin.setValue(20)
        self._reconsider_limit_spin.setSuffix(" 单")
        self._reconsider_limit_spin.setToolTip(
            "完整扫描后，按截止时间优先处理不超过该数量的待处理订单。"
        )
        limit_row.addWidget(self._reconsider_limit_spin)
        limit_row.addStretch(1)
        layout.addLayout(limit_row)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self._stop_reconsider_button = QPushButton("停止复议")
        self._stop_reconsider_button.setObjectName("dangerAction")
        self._stop_reconsider_button.setEnabled(False)
        self._stop_reconsider_button.setToolTip(
            "在安全边界停止；已点击提交的订单会先完成验证码和结果确认。"
        )
        self._stop_reconsider_button.clicked.connect(self._on_stop_reconsider_clicked)
        self._reconsider_button = QPushButton("开始发起复议")
        self._reconsider_button.setObjectName("primaryAction")
        self._reconsider_button.setToolTip(
            "对所选店铺扫描可复议订单并真实提交复议。"
        )
        self._reconsider_button.clicked.connect(self._on_reconsider_clicked)
        action_row.addWidget(self._stop_reconsider_button)
        action_row.addWidget(self._reconsider_button)
        layout.addLayout(action_row)
        layout.addStretch(1)
        return tab

    def _build_aftersale_task_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        hint = QLabel(
            "保存退款申请单和聊天记录作为凭证并真实提交维权申诉。可同时保持"
            "多家店铺浏览器打开，但订单会在店铺之间逐单轮转，不会并发操作页面。"
        )
        hint.setObjectName("mutedText")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        row = QHBoxLayout()
        row.addWidget(QLabel("同时运行店铺数"))
        self._aftersale_pool_size_spin = QSpinBox()
        self._aftersale_pool_size_spin.setRange(1, 10)
        self._aftersale_pool_size_spin.setValue(
            scheduler_core.DEFAULT_MAX_CONCURRENT
        )
        self._aftersale_pool_size_spin.setSuffix(" 家")
        self._aftersale_pool_size_spin.setToolTip(
            "最多保持这些店铺的浏览器打开；订单操作仍然一次只执行一家。"
        )
        row.addWidget(self._aftersale_pool_size_spin)
        row.addSpacing(16)
        row.addWidget(QLabel("每家店铺本次最多处理"))
        self._aftersale_limit_spin = QSpinBox()
        self._aftersale_limit_spin.setRange(1, 999)
        self._aftersale_limit_spin.setValue(10)
        self._aftersale_limit_spin.setSuffix(" 单")
        row.addWidget(self._aftersale_limit_spin)
        row.addStretch(1)
        self._stop_aftersale_button = QPushButton("停止售后申诉")
        self._stop_aftersale_button.setObjectName("dangerAction")
        self._stop_aftersale_button.setEnabled(False)
        self._stop_aftersale_button.setToolTip(
            "在当前订单的安全边界停止，已点击提交的订单会先完成结果确认。"
        )
        self._stop_aftersale_button.clicked.connect(
            self._on_stop_aftersale_clicked
        )
        row.addWidget(self._stop_aftersale_button)
        self._aftersale_submit_button = QPushButton("真实提交售后申诉")
        self._aftersale_submit_button.setObjectName("primaryAction")
        self._aftersale_submit_button.clicked.connect(self._on_aftersale_clicked)
        row.addWidget(self._aftersale_submit_button)
        layout.addLayout(row)
        layout.addStretch(1)
        return tab

    def _build_status_group(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        progress_group = QGroupBox("总体进度")
        progress_layout = QVBoxLayout(progress_group)
        progress_layout.setSpacing(5)
        self._run_progress_bar = QProgressBar()
        self._run_progress_bar.setRange(0, 1000)
        self._run_progress_bar.setValue(0)
        self._run_progress_bar.setFormat("尚未开始")
        progress_layout.addWidget(self._run_progress_bar)
        self._run_progress_summary = QLabel("尚未开始")
        progress_layout.addWidget(self._run_progress_summary)
        progress_note = QLabel(
            "预计剩余时间根据已完成订单的实际速度动态估算，"
            "刚启动或遇到验证码时可能波动。"
        )
        progress_note.setObjectName("mutedText")
        progress_layout.addWidget(progress_note)
        layout.addWidget(progress_group)

        body = QSplitter(Qt.Horizontal)
        body.setChildrenCollapsible(False)

        left_group = QGroupBox("当前任务")
        left = QVBoxLayout(left_group)
        self._status_table = QTableWidget(0, 4)
        self._status_table.setHorizontalHeaderLabels(["店铺", "当前动作", "本次剩余", "浏览器"])
        status_header = self._status_table.horizontalHeader()
        status_header.setMinimumSectionSize(60)
        status_header.setSectionResizeMode(0, QHeaderView.Stretch)
        status_header.setSectionResizeMode(1, QHeaderView.Fixed)
        status_header.setSectionResizeMode(2, QHeaderView.Fixed)
        status_header.setSectionResizeMode(3, QHeaderView.Fixed)
        status_header.resizeSection(1, 190)
        status_header.resizeSection(2, 78)
        status_header.resizeSection(3, 72)
        self._status_table.verticalHeader().setVisible(False)
        self._status_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._status_table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        left.addWidget(self._status_table)
        self._follow_browser_check = QCheckBox("自动跟随表格中选中的店铺")
        self._follow_browser_check.setToolTip("默认关闭；开启后只在选中店铺开始处理订单时前置其浏览器。")
        left.addWidget(self._follow_browser_check)
        self._topmost_browser_check = QCheckBox("处理订单时自动置顶当前浏览器")
        self._topmost_browser_check.setChecked(True)
        self._topmost_browser_check.setToolTip(
            "轮到某家店铺处理时置顶它的浏览器；本单结束或切换店铺时自动取消。"
        )
        self._topmost_browser_check.toggled.connect(self._on_topmost_toggled)
        left.addWidget(self._topmost_browser_check)
        left_group.setMinimumWidth(350)
        left_group.setMaximumWidth(560)
        body.addWidget(left_group)

        right_group = QGroupBox("运行日志")
        right = QVBoxLayout(right_group)
        self._log_view = QPlainTextEdit()
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(_LOG_MAX_BLOCKS)
        self._log_view.setLineWrapMode(QPlainTextEdit.WidgetWidth)
        self._log_view.setMinimumHeight(190)
        right.addWidget(self._log_view)
        body.addWidget(right_group)
        body.setStretchFactor(0, 0)
        body.setStretchFactor(1, 1)
        body.setSizes([370, 600])
        layout.addWidget(body, stretch=1)

        return container

    # ---- 店铺勾选列表 ----

    def _on_shop_data(self, summaries: list[shop_repo.ShopSummary]) -> None:
        self._refresh_shop_list(summaries)

    def _on_shop_fetch_error(self, message: str) -> None:
        logger.warning(f"[RunPage] 刷新店铺列表失败：{message}")

    def _refresh_shop_list(self, summaries: list[shop_repo.ShopSummary]) -> None:
        """用最新数据重建店铺勾选列表，但**保留用户已经手动设置过的勾选
        状态**——不能像早期版本那样每次重建都无脑全部设成已勾选，那样会
        在下一次刷新（包括点"启动"前触发的那一次）悄悄清空用户手动取消
        的勾选。之前列表里没出现过的店铺（新增/重新登录成功）才默认勾上；
        停用/删除/掉线导致消失的店铺自然从列表里被过滤掉，对应的历史
        勾选状态也就跟着丢弃了，不需要特殊处理。
        """
        previous_state = {
            self._shop_list.item(i).text(): self._shop_list.item(i).checkState()
            for i in range(self._shop_list.count())
        }

        self._shop_list.clear()
        for summary in summaries:
            if summary.disabled or not summary.has_profile or summary.login_expired:
                continue  # 停用的、没登录过的、或今天已确认登录失效的店铺不参与批量处理
            item = QListWidgetItem(summary.shop)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(previous_state.get(summary.shop, Qt.Checked))
            self._shop_list.addItem(item)
        self._update_shop_selection_label()

    def _set_all_shops_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        for i in range(self._shop_list.count()):
            self._shop_list.item(i).setCheckState(state)

    def _update_shop_selection_label(self, *_args) -> None:
        total = self._shop_list.count()
        checked = sum(
            self._shop_list.item(i).checkState() == Qt.Checked
            for i in range(total)
        )
        self._shop_selection_label.setText(f"已选择 {checked}/{total} 家")

    def _checked_shops(self) -> list[str]:
        shops = []
        for i in range(self._shop_list.count()):
            item = self._shop_list.item(i)
            if item.checkState() == Qt.Checked:
                shops.append(item.text())
        return shops

    # ---- 启动/停止 ----

    def _on_start_clicked(self) -> None:
        # 不在这里再单独查一次库——店铺勾选列表已经由 self._shop_poller
        # 每 4 秒自动刷新（且保留了用户的勾选状态），直接读界面上当前的
        # 勾选结果就是最新的，不需要为了"确保最新"额外阻塞一次数据库查询。
        shops = self._checked_shops()
        if not shops:
            msgbox.warning(self, "没有勾选店铺", "请至少勾选一家店铺再启动。")
            return

        if self._check_results_worker is not None:
            msgbox.warning(self, "正在查询申诉结果", "当前正在查询申诉结果，请等它结束后再启动批量任务。")
            return
        if self._check_reconsider_results_worker is not None:
            msgbox.warning(self, "正在查询复议结果", "当前正在查询复议结果，请等它结束后再启动批量任务。")
            return
        if self._check_aftersale_results_worker is not None:
            msgbox.warning(self, "正在查询售后结果", "当前正在查询售后结果，请等它结束后再启动批量任务。")
            return
        if self._reconsider_worker is not None:
            msgbox.warning(self, "正在发起复议", "当前正在处理复议，请等它结束后再启动批量任务。")
            return

        active_lock = scheduler_core.active_operation_lock()
        if active_lock is not None:
            operation = active_lock.get("operation") or "其它任务"
            conflict_shops = "、".join(active_lock.get("shops") or []) or "未知店铺"
            msgbox.warning(
                self,
                "存在运行冲突",
                f"检测到{operation}正在运行（店铺：{conflict_shops}），"
                "为避免抢占店铺登录目录，当前不能启动批量任务。",
            )
            return

        # 启用了"每日自动运行"时，这个时间段不只管自动触发/自动停止，
        # 手动点"启动"也要一起受它管——不管手动还是自动，都不应该在
        # 设定时间段之外运行。开关关掉时不受任何限制，跟原来行为一样。
        if self._auto_settings.enabled and not self._currently_in_auto_window():
            start, end = self._auto_settings.start_time, self._auto_settings.end_time
            msgbox.warning(
                self,
                "不在允许运行的时间段内",
                f"已启用「每日自动运行」（{start}–{end}），当前时间不在这个时间段内，"
                "不允许启动——无论手动点击还是自动触发，都不应该在这个时间段之外运行。\n\n"
                "如果需要现在启动，请先在上方关闭「启用每日自动运行」，或者调整时间段后再试。",
            )
            return

        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return

        # 固定行为（不给运营选，见 _build_form_group 里的说明）：永远真实
        # 提交、永远自动破解验证码优先、永远不强制重新分析、勾选的店铺
        # 跑完这一轮就自然停止。
        pool_size = self._pool_size_spin.value()

        if not confirm_real_submit(self, shops, pool_size, manual_captcha=False):
            return

        self._append_log(f"[GUI] 启动：{len(shops)} 家店铺，并发上限 {pool_size}，真实提交，只跑一轮")
        self._start_run(shops, pool_size)

    def _start_run(self, shops: list[str], pool_size: int, max_orders: int | None = None) -> None:
        """真正构造 SchedulerWorker + 起 QThread 的部分，从 _on_start_clicked
        抽出来单独给"定时任务自动触发"复用（见 _check_auto_schedule）——
        人工点"启动"已经在调用这个方法之前弹过 confirm_real_submit 确认；
        定时任务的确认发生在"打开自动运行开关"那一刻（见
        _on_auto_enable_toggled），到点自动触发时不会再弹一次，两条路径
        都不在这个方法内部重复判断要不要弹窗。
        """
        extra = ["--confirm"]
        if max_orders is not None:
            extra.extend(["--max-orders", str(max_orders)])
        response_timeout = scheduler_core.DEFAULT_RESPONSE_TIMEOUT

        self._set_form_enabled(False)
        self._stop_button.setEnabled(True)
        self._stopping = False
        self._begin_progress_tracking(shops, "等待浏览器名额")

        self._thread = QThread(self)
        self._worker = SchedulerWorker(
            shops=shops,
            extra=extra,
            pool_size=pool_size,
            max_consecutive_errors=scheduler_core.DEFAULT_MAX_CONSECUTIVE_ERRORS,
            response_timeout=response_timeout,
            python_executable=PROJECT_PATHS.python,
            run_forever=False,
            rest_enabled=self._rest_settings.enabled,
            rest_min_hours=self._rest_settings.min_hours,
            rest_max_hours=self._rest_settings.max_hours,
            rest_min_minutes=self._rest_settings.min_minutes,
            rest_max_minutes=self._rest_settings.max_minutes,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.event_received.connect(self._on_event)
        self._worker.error.connect(self._on_scheduler_error)
        self._worker.browser_focus_result.connect(self._on_browser_focus_result)
        self._worker.stopped.connect(self._on_stopped)
        self._worker.stopped.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._pipeline_watch(self._thread)
        self._thread.start()
        self._update_auto_status_label()

    def is_running(self) -> bool:
        """给 MainWindow.closeEvent 用：判断关闭 GUI 前要不要拦一下。"""
        return (
            self._pipeline_active
            or
            self._worker is not None
            or self._check_results_worker is not None
            or self._check_reconsider_results_worker is not None
            or self._check_aftersale_results_worker is not None
            or self._reconsider_worker is not None
            or self._aftersale_worker is not None
        )

    def begin_shutdown_stop(self) -> None:
        """GUI 要关闭、且当前有任务在跑时调用——跟点"优雅停止"按钮走
        同一条路径，不新增停止逻辑本身，只是换了个调用入口。"""
        if self._pipeline_active:
            self._pipeline_cancelled = True
        self._request_stop(reason="GUI 关闭")
        if self._check_results_worker is not None:
            self._check_results_worker.cancel()
        if self._check_reconsider_results_worker is not None:
            self._check_reconsider_results_worker.cancel()
        if self._check_aftersale_results_worker is not None:
            self._check_aftersale_results_worker.cancel()
        if self._reconsider_worker is not None:
            self._request_reconsider_stop(reason="GUI 关闭")
        if self._aftersale_worker is not None:
            self._request_aftersale_stop(reason="GUI 关闭")

    # ---- 发起复议 ----

    def _request_reconsider_stop(self, reason: str = "用户操作") -> None:
        if self._reconsider_worker is None or self._reconsider_stopping:
            return
        self._reconsider_stopping = True
        self._stop_reconsider_button.setEnabled(False)
        self._stop_reconsider_button.setText("停止中…")
        self._append_log(
            f"[发起复议] 已请求停止（{reason}），将在当前安全步骤结束后停止。"
        )
        self._reconsider_worker.cancel()

    def _on_stop_reconsider_clicked(self) -> None:
        self._request_reconsider_stop()

    def _on_reconsider_clicked(self) -> None:
        shops = self._checked_shops()
        max_orders = self._reconsider_limit_spin.value()
        if not shops:
            msgbox.warning(self, "没有勾选店铺", "请至少勾选一家店铺再发起复议。")
            return
        pool_size = min(self._reconsider_pool_size_spin.value(), len(shops))
        if (
            self._worker is not None
            or self._check_results_worker is not None
            or self._check_reconsider_results_worker is not None
        ):
            msgbox.warning(
                self, "存在运行冲突",
                "当前有批量申诉或申诉结果查询正在运行，请等待任务结束后再发起复议。",
            )
            return
        if self._reconsider_worker is not None:
            msgbox.warning(self, "正在发起复议", "上一次复议任务还没有结束。")
            return

        confirmed = msgbox.confirm_yes_no(
            self,
            "确认真实提交复议",
            f"将对 {len(shops)} 家店铺扫描全部可复议订单，每家本次最多处理 "
            f"{max_orders} 条，同时保持最多 {pool_size} 家店铺浏览器，订单会"
            "在店铺之间逐单轮转，不会同时操作页面，并真实提交复议。\n\n"
            "已有材料会复用首次申诉原因、文案和凭证；平台页面提示复议凭证"
            "应与首次申诉不同，按当前规则复用可能影响通过率。材料缺失的订单"
            "会先重新采集聊天并生成材料。\n\n确定继续吗？",
        )
        if not confirmed:
            return

        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return

        self._start_reconsider(shops, pool_size, max_orders)

    def _start_reconsider(self, shops, pool_size, max_orders):
        lock_id, conflict = scheduler_core.acquire_operation_lock(
            shops, "单次发起复议"
        )
        if lock_id is None:
            conflict = conflict or {}
            operation = conflict.get("operation") or "其它任务"
            conflict_shops = "、".join(conflict.get("shops") or []) or "未知店铺"
            if self._pipeline_active:
                raise RuntimeError(f"发起复议启动失败：{operation}正在运行（店铺：{conflict_shops}）。")
            msgbox.warning(
                self,
                "存在运行冲突",
                f"检测到{operation}正在运行（店铺：{conflict_shops}）。"
                "为避免抢占店铺登录目录，本次复议不会启动。",
            )
            return

        self._reconsider_lock_id = lock_id
        self._reconsider_processed = 0
        self._reconsider_submitted = 0
        self._reconsider_pending_confirmation = 0
        self._reconsider_failed = 0
        self._reconsider_stopping = False
        self._append_log(
            f"[GUI] 开始发起复议：{len(shops)} 家店铺，"
            f"浏览器池 {pool_size} 家，每店最多 {max_orders} 条，逐单轮转，真实提交"
        )
        self._begin_progress_tracking(shops, "等待扫描")
        self._start_button.setEnabled(False)
        self._check_results_button.setEnabled(False)
        self._check_reconsider_results_button.setEnabled(False)
        self._reconsider_button.setEnabled(False)
        self._reconsider_pool_size_spin.setEnabled(False)
        self._reconsider_limit_spin.setEnabled(False)
        self._stop_reconsider_button.setText("停止复议")
        self._stop_reconsider_button.setEnabled(True)

        self._reconsider_thread = QThread(self)
        self._reconsider_worker = ReconsiderationWorker(
            pddchat_python=PROJECT_PATHS.python,
            main_py=PROJECT_PATHS.main_py,
            pddchat_root=PROJECT_PATHS.root,
            shops=shops,
            max_orders=max_orders,
            pool_size=pool_size,
        )
        self._reconsider_worker.moveToThread(self._reconsider_thread)
        self._reconsider_thread.started.connect(self._reconsider_worker.start)
        self._reconsider_worker.shop_started.connect(self._on_reconsider_shop_started)
        self._reconsider_worker.shop_progress.connect(self._on_reconsider_shop_progress)
        self._reconsider_worker.shop_activity.connect(self._on_shop_activity)
        self._reconsider_worker.browser_focus_result.connect(self._on_browser_focus_result)
        self._reconsider_worker.shop_order_result.connect(
            self._on_reconsider_shop_order_result
        )
        self._reconsider_worker.shop_finished.connect(self._on_reconsider_shop_finished)
        self._reconsider_worker.all_finished.connect(self._on_reconsider_all_finished)
        self._reconsider_worker.all_finished.connect(self._reconsider_thread.quit)
        self._reconsider_worker.all_finished.connect(self._reconsider_worker.deleteLater)
        self._reconsider_thread.finished.connect(self._on_reconsider_thread_finished)
        self._reconsider_thread.finished.connect(self._reconsider_thread.deleteLater)
        self._pipeline_watch(self._reconsider_thread)
        try:
            self._reconsider_thread.start()
        except Exception:
            scheduler_core.release_operation_lock(self._reconsider_lock_id)
            self._reconsider_lock_id = None
            self._reconsider_worker = None
            self._reconsider_thread = None
            self._start_button.setEnabled(True)
            self._check_results_button.setEnabled(True)
            self._check_reconsider_results_button.setEnabled(True)
            self._reconsider_button.setEnabled(True)
            self._reconsider_pool_size_spin.setEnabled(True)
            self._reconsider_limit_spin.setEnabled(True)
            self._stop_reconsider_button.setEnabled(False)
            raise

    def _on_reconsider_shop_started(self, shop: str) -> None:
        self._task_shop_started_at[shop] = time.monotonic()
        self._append_log(f"[发起复议][{shop}] 开始扫描可复议订单……")
        self._set_reconsider_status(shop, "正在扫描…", "")

    def _on_reconsider_shop_progress(
        self, shop: str, stage: str, current: int, total: int
    ) -> None:
        if stage == "扫描":
            self._append_log(
                f"[发起复议][{shop}] 第 {current} 次滚动，已发现 {total} 条"
            )
            self._set_reconsider_status(shop, "正在扫描…", str(total))
        elif stage == "等待轮转":
            self._run_initial_totals[shop] = max(0, total)
            self._run_remaining[shop] = max(0, total)
            self._refresh_run_progress()
            self._set_reconsider_status(shop, "等待下一轮", str(total))
        elif stage == "切换等待":
            self._run_remaining[shop] = max(0, total)
            self._refresh_run_progress()
            self._append_log(
                f"[发起复议][{shop}] 本单已完成，等待 1–3 秒后切换店铺……"
            )
            self._set_reconsider_status(shop, "等待切换店铺", str(total))
        else:
            self._task_order_started_at[shop] = time.monotonic()
            self._run_initial_totals.setdefault(shop, max(0, total))
            self._run_remaining[shop] = max(0, total - current + 1)
            self._refresh_run_progress()
            self._append_log(
                f"[发起复议][{shop}] 正在处理第 {current}/{total} 条"
            )
            self._set_reconsider_status(
                shop,
                f"正在处理 {current}/{total}",
                str(max(0, total - current + 1)),
            )

    def _on_reconsider_shop_order_result(
        self, shop: str, order_no: str, result: str, remaining: int
    ) -> None:
        self._pipeline_order(shop, result, order_no)
        self._release_topmost_browser(shop)
        order_started = self._task_order_started_at.pop(shop, None)
        duration = time.monotonic() - order_started if order_started is not None else 0.0
        self._run_processed_orders += 1
        self._run_remaining[shop] = max(0, remaining)
        self._refresh_run_progress()
        self._append_log(
            f"[发起复议][{shop}] 订单 {order_no}：{result}"
            f"（耗时 {scheduler_core.format_duration(duration)}）"
        )
        self._set_reconsider_status(shop, result, str(remaining))

    def _on_reconsider_shop_finished(
        self,
        shop: str,
        success: bool,
        processed: int,
        submitted: int,
        pending_confirmation: int,
        failed: int,
        message: str,
    ) -> None:
        self._pipeline_shop_result(shop, success, message)
        self._release_topmost_browser(shop)
        self._run_terminal_shops.add(shop)
        if not success:
            self._run_failed_shops.add(shop)
        duration = time.monotonic() - self._task_shop_started_at.pop(
            shop, self._run_started_monotonic or time.monotonic()
        )
        self._refresh_run_progress()
        self._reconsider_processed += processed
        self._reconsider_submitted += submitted
        self._reconsider_pending_confirmation += pending_confirmation
        self._reconsider_failed += failed
        if success:
            self._append_log(
                f"[发起复议][{shop}] 完成：处理 {processed}，已提交 {submitted}，"
                f"待确认 {pending_confirmation}，失败 {failed}，"
                f"耗时 {scheduler_core.format_duration(duration)}。"
            )
            self._set_reconsider_status(shop, "已完成", "0")
        else:
            self._append_log(
                f"[发起复议][{shop}] 失败：{message}"
                f"（耗时 {scheduler_core.format_duration(duration)}）"
            )
            status = (
                "连续3单失败，已跳过店铺"
                if "连续 3 条" in message else "失败"
            )
            self._set_reconsider_status(shop, status, "-")

    def _set_reconsider_status(self, shop: str, status: str, remaining: str) -> None:
        row = self._ensure_status_row(shop)
        self._status_table.item(row, 1).setText(status)
        self._status_table.item(row, 1).setToolTip(status)
        self._status_table.item(row, 2).setText(remaining)

    def _ensure_status_row(self, shop: str) -> int:
        row = self._row_index.get(shop)
        if row is None:
            row = self._status_table.rowCount()
            self._status_table.insertRow(row)
            shop_item = QTableWidgetItem(shop)
            shop_item.setToolTip(shop)
            self._status_table.setItem(row, 0, shop_item)
            for column in range(1, 3):
                self._status_table.setItem(row, column, QTableWidgetItem(""))
            button = QPushButton("查看")
            button.setToolTip(f"定位并前置店铺“{shop}”当前使用的浏览器窗口")
            button.clicked.connect(lambda _checked=False, name=shop: self._focus_shop_browser(name))
            self._status_table.setCellWidget(row, 3, button)
            self._browser_buttons[shop] = button
            self._row_index[shop] = row
        return row

    def _on_shop_activity(self, shop: str, stage: str, order_no: str) -> None:
        detail = f"{stage}｜{order_no}" if order_no else stage
        self._set_reconsider_status(shop, detail, self._status_table.item(
            self._ensure_status_row(shop), 2
        ).text())
        self._maybe_follow_browser(shop)
        self._activate_topmost_browser(shop)

    def _focus_shop_browser(self, shop: str) -> None:
        row = self._ensure_status_row(shop)
        self._status_table.setCurrentCell(row, 0)
        worker = self._reconsider_worker or self._aftersale_worker or self._worker
        if worker is None:
            self._on_browser_focus_result(shop, False)
            return
        worker.request_browser_focus(shop)

    def _maybe_follow_browser(self, shop: str) -> None:
        if not self._follow_browser_check.isChecked():
            return
        row = self._status_table.currentRow()
        selected = self._status_table.item(row, 0) if row >= 0 else None
        if selected is not None and selected.text() == shop:
            self._focus_shop_browser(shop)

    def _on_browser_focus_result(self, shop: str, success: bool) -> None:
        if not success:
            self._append_log(
                f"[浏览器定位][{shop}] 暂未找到可见浏览器窗口；可能仍在启动、已经关闭，或 Windows 阻止了窗口前置。"
            )

    def _active_browser_worker(self):
        return self._reconsider_worker or self._aftersale_worker or self._worker

    def _activate_topmost_browser(self, shop: str) -> None:
        if not self._topmost_browser_check.isChecked():
            return
        worker = self._active_browser_worker()
        if worker is None:
            return
        if self._topmost_shop and self._topmost_shop != shop:
            worker.request_browser_topmost(self._topmost_shop, False)
        self._topmost_shop = shop
        worker.request_browser_topmost(shop, True)

    def _release_topmost_browser(self, shop: str | None = None) -> None:
        target = self._topmost_shop
        if target is None or (shop is not None and shop != target):
            return
        worker = self._active_browser_worker()
        if worker is not None:
            worker.request_browser_topmost(target, False)
        self._topmost_shop = None

    def _on_topmost_toggled(self, enabled: bool) -> None:
        if not enabled:
            self._release_topmost_browser()

    def _on_reconsider_all_finished(self) -> None:
        if self._pipeline_active:
            self._pipeline_phase_complete = True
        was_stopping = self._reconsider_stopping
        finish_text = "已停止" if was_stopping else "全部完成"
        self._complete_progress_tracking(normal=not was_stopping)
        elapsed = scheduler_core.format_duration(self._run_finished_elapsed or 0.0)
        self._append_log(
            f"[发起复议] {finish_text}：处理 {self._reconsider_processed}，"
            f"已提交 {self._reconsider_submitted}，"
            f"待确认 {self._reconsider_pending_confirmation}，"
            f"失败 {self._reconsider_failed}，总耗时 {elapsed}。"
        )
        scheduler_core.release_operation_lock(self._reconsider_lock_id)
        self._reconsider_lock_id = None
        self._reconsider_worker = None
        self._reconsider_thread = None
        self._reconsider_stopping = False
        self._reconsider_button.setEnabled(True)
        self._reconsider_pool_size_spin.setEnabled(True)
        self._reconsider_limit_spin.setEnabled(True)
        self._stop_reconsider_button.setText("停止复议")
        self._stop_reconsider_button.setEnabled(False)
        self._check_results_button.setEnabled(True)
        self._check_reconsider_results_button.setEnabled(True)
        if self._worker is None:
            self._start_button.setEnabled(True)
        if self._pipeline_active:
            return
        msgbox.information(
            self,
            "复议已停止" if was_stopping else "复议完成",
            f"已提交 {self._reconsider_submitted} 条，"
            f"结果待确认 {self._reconsider_pending_confirmation} 条，"
            f"失败 {self._reconsider_failed} 条。",
        )

    def _on_reconsider_thread_finished(self) -> None:
        if self._reconsider_worker is None:
            return
        self._append_log("[发起复议] 工作线程异常结束，已释放运行锁。")
        self._complete_progress_tracking(normal=False)
        scheduler_core.release_operation_lock(self._reconsider_lock_id)
        self._reconsider_lock_id = None
        self._reconsider_worker = None
        self._reconsider_thread = None
        self._reconsider_stopping = False
        self._reconsider_button.setEnabled(True)
        self._reconsider_pool_size_spin.setEnabled(True)
        self._reconsider_limit_spin.setEnabled(True)
        self._stop_reconsider_button.setText("停止复议")
        self._stop_reconsider_button.setEnabled(False)
        self._check_results_button.setEnabled(True)
        self._check_reconsider_results_button.setEnabled(True)
        if self._worker is None:
            self._start_button.setEnabled(True)

    # ---- 售后申诉（多店铺浏览器池，逐单真实提交） ----

    def _request_aftersale_stop(self, reason: str = "用户操作") -> None:
        if self._aftersale_worker is None or self._aftersale_stopping:
            return
        self._aftersale_stopping = True
        self._stop_aftersale_button.setEnabled(False)
        self._stop_aftersale_button.setText("停止中…")
        self._append_log(
            f"[售后申诉] 已请求停止（{reason}），将在当前订单安全结束后停止。"
        )
        self._aftersale_worker.cancel()

    def _on_stop_aftersale_clicked(self) -> None:
        self._request_aftersale_stop()

    def _on_aftersale_clicked(self) -> None:
        shops = self._checked_shops()
        if not shops:
            msgbox.warning(self, "没有勾选店铺", "请至少勾选一家店铺再启动。")
            return
        if self.is_running():
            msgbox.warning(
                self,
                "存在运行冲突",
                "当前有其它浏览器任务正在运行，请等待任务结束后再启动售后申诉。",
            )
            return
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        max_orders = self._aftersale_limit_spin.value()
        pool_size = min(self._aftersale_pool_size_spin.value(), len(shops))
        if not confirm_real_submit(
            self,
            shops,
            pool_size=pool_size,
            manual_captcha=False,
        ):
            return
        self._start_aftersale(shops, pool_size, max_orders)

    def _start_aftersale(self, shops, pool_size, max_orders):
        lock_id, conflict = scheduler_core.acquire_operation_lock(
            shops, "售后申诉真实提交"
        )
        if lock_id is None:
            conflict = conflict or {}
            if self._pipeline_active:
                raise RuntimeError(
                    f"售后申诉启动失败：{conflict.get('operation') or '其它任务'}正在运行。"
                )
            msgbox.warning(
                self,
                "存在运行冲突",
                f"检测到{conflict.get('operation') or '其它任务'}正在运行。",
            )
            return

        self._aftersale_lock_id = lock_id
        self._aftersale_processed = 0
        self._aftersale_succeeded = 0
        self._aftersale_failed = 0
        self._aftersale_stopping = False
        self._begin_progress_tracking(shops, "等待浏览器名额")
        self._aftersale_submit_button.setEnabled(False)
        self._stop_aftersale_button.setText("停止售后申诉")
        self._stop_aftersale_button.setEnabled(True)
        self._aftersale_pool_size_spin.setEnabled(False)
        self._aftersale_limit_spin.setEnabled(False)
        self._start_button.setEnabled(False)
        self._reconsider_button.setEnabled(False)
        self._check_results_button.setEnabled(False)
        self._check_reconsider_results_button.setEnabled(False)
        self._append_log(
            f"[售后申诉] 开始：{len(shops)} 家店铺，浏览器池 {pool_size} 家，"
            f"每店最多 {max_orders} 单，逐单轮转，真实提交"
        )

        self._aftersale_thread = QThread(self)
        self._aftersale_worker = AftersaleAppealWorker(
            pddchat_python=PROJECT_PATHS.python,
            main_py=PROJECT_PATHS.main_py,
            pddchat_root=PROJECT_PATHS.root,
            shops=shops,
            max_orders=max_orders,
            pool_size=pool_size,
        )
        self._aftersale_worker.moveToThread(self._aftersale_thread)
        self._aftersale_thread.started.connect(self._aftersale_worker.start)
        self._aftersale_worker.shop_started.connect(self._on_aftersale_shop_started)
        self._aftersale_worker.shop_progress.connect(self._on_aftersale_shop_progress)
        self._aftersale_worker.shop_activity.connect(self._on_shop_activity)
        self._aftersale_worker.browser_focus_result.connect(self._on_browser_focus_result)
        self._aftersale_worker.shop_order_result.connect(
            self._on_aftersale_shop_order_result
        )
        self._aftersale_worker.shop_finished.connect(self._on_aftersale_shop_finished)
        self._aftersale_worker.all_finished.connect(self._on_aftersale_all_finished)
        self._aftersale_worker.all_finished.connect(self._aftersale_thread.quit)
        self._aftersale_worker.all_finished.connect(self._aftersale_worker.deleteLater)
        self._aftersale_thread.finished.connect(self._on_aftersale_thread_finished)
        self._aftersale_thread.finished.connect(self._aftersale_thread.deleteLater)
        self._pipeline_watch(self._aftersale_thread)
        try:
            self._aftersale_thread.start()
        except Exception:
            self._finish_aftersale_ui()
            raise

    def _on_aftersale_shop_started(self, shop: str) -> None:
        self._task_shop_started_at[shop] = time.monotonic()
        self._set_reconsider_status(shop, "正在初始化…", "")
        self._append_log(f"[售后申诉][{shop}] 正在打开浏览器并读取订单……")

    def _on_aftersale_shop_progress(
        self,
        shop: str,
        stage: str,
        current: int,
        total: int,
    ) -> None:
        if stage == "等待轮转":
            self._run_initial_totals[shop] = max(0, total)
            self._run_remaining[shop] = max(0, total)
            self._refresh_run_progress()
            self._set_reconsider_status(shop, "等待下一单", str(total))
        elif stage == "切换等待":
            self._run_remaining[shop] = max(0, total)
            self._refresh_run_progress()
            self._set_reconsider_status(shop, "等待切换店铺", str(total))
        else:
            self._set_reconsider_status(
                shop,
                stage or f"正在处理 {current}/{total}",
                str(max(0, total - current + 1)),
            )

    def _on_aftersale_shop_order_result(
        self,
        shop: str,
        order_no: str,
        result: str,
        remaining: int,
    ) -> None:
        self._pipeline_order(shop, result, order_no)
        self._release_topmost_browser(shop)
        self._run_processed_orders += 1
        self._run_remaining[shop] = max(0, remaining)
        self._refresh_run_progress()
        self._set_reconsider_status(shop, result, str(remaining))
        self._append_log(f"[售后申诉][{shop}] 订单 {order_no}：{result}")

    def _on_aftersale_shop_finished(
        self, shop: str, success: bool, payload: object, message: str
    ) -> None:
        self._pipeline_shop_result(shop, success, message)
        self._release_topmost_browser(shop)
        result = dict(payload) if isinstance(payload, dict) else {}
        processed = int(result.get("processed", 0))
        succeeded = int(result.get("succeeded", 0))
        failed = int(result.get("failed", 0))
        self._aftersale_processed += processed
        self._aftersale_succeeded += succeeded
        self._aftersale_failed += failed if success or failed else 1
        self._run_terminal_shops.add(shop)
        if not success:
            self._run_failed_shops.add(shop)
        stopped = bool(result.get("stopped"))
        empty = bool(result.get("empty"))
        self._set_reconsider_status(
            shop,
            "已停止"
            if stopped
            else (
                "暂无可处理订单"
                if empty
                else (
                    "已完成" if success else (
                        "连续3单失败，已跳过店铺"
                        if "连续 3 条" in message else "失败"
                    )
                )
            ),
            "0",
        )
        if success:
            self._append_log(
                f"[售后申诉][{shop}] "
                f"{'已停止' if stopped else ('暂无可处理订单' if empty else '完成')}："
                f"成功 {succeeded}，失败 {failed}。"
            )
        else:
            self._append_log(
                f"[售后申诉][{shop}] 失败：{message or f'成功 {succeeded}，失败 {failed}'}"
            )

    def _finish_aftersale_ui(self) -> None:
        scheduler_core.release_operation_lock(self._aftersale_lock_id)
        self._aftersale_lock_id = None
        self._aftersale_worker = None
        self._aftersale_thread = None
        self._aftersale_stopping = False
        self._aftersale_submit_button.setEnabled(True)
        self._stop_aftersale_button.setText("停止售后申诉")
        self._stop_aftersale_button.setEnabled(False)
        self._aftersale_pool_size_spin.setEnabled(True)
        self._aftersale_limit_spin.setEnabled(True)
        self._start_button.setEnabled(True)
        self._reconsider_button.setEnabled(True)
        self._check_results_button.setEnabled(True)
        self._check_reconsider_results_button.setEnabled(True)

    def _on_aftersale_all_finished(self) -> None:
        if self._pipeline_active:
            self._pipeline_phase_complete = True
        was_stopping = self._aftersale_stopping
        was_empty = (
            self._aftersale_processed == 0
            and self._aftersale_failed == 0
            and not self._run_failed_shops
            and not was_stopping
        )
        self._complete_progress_tracking(
            normal=self._aftersale_failed == 0 and not was_stopping
        )
        self._append_log(
            f"[售后申诉] {'已停止' if was_stopping else '全部完成'}："
            f"处理 {self._aftersale_processed}，"
            f"成功 {self._aftersale_succeeded}，失败 {self._aftersale_failed}。"
        )
        succeeded = self._aftersale_succeeded
        failed = self._aftersale_failed
        self._finish_aftersale_ui()
        if self._pipeline_active:
            return
        msgbox.information(
            self,
            "售后申诉已停止"
            if was_stopping
            else (
                "暂无可处理订单"
                if was_empty
                else "售后申诉提交完成"
            ),
            (
                "平台列表明确显示暂无可处理的售后申诉订单。"
                if was_empty
                else f"已提交 {succeeded} 单，失败 {failed} 单。"
            ),
        )

    def _on_aftersale_thread_finished(self) -> None:
        if self._aftersale_worker is None:
            return
        self._append_log("[售后申诉] 工作线程异常结束，已释放运行锁。")
        self._complete_progress_tracking(normal=False)
        self._finish_aftersale_ui()

    # ---- 查询申诉结果 ----

    def _on_check_results_clicked(self) -> None:
        shops = self._checked_shops()
        if not shops:
            msgbox.warning(self, "没有勾选店铺", "请至少勾选一家店铺再查询。")
            return
        if self._worker is not None:
            msgbox.warning(self, "批量任务正在运行", "当前有批量任务正在运行，请先停止后再查询申诉结果。")
            return
        if self._check_results_worker is not None:
            msgbox.warning(self, "正在查询", "上一次查询还没结束，请稍候。")
            return
        if self._check_reconsider_results_worker is not None:
            msgbox.warning(self, "正在查询复议结果", "请等待复议结果查询结束后再查询申诉结果。")
            return
        if self._reconsider_worker is not None:
            msgbox.warning(self, "正在发起复议", "请等待复议任务结束后再查询申诉结果。")
            return

        if self._start_check_results(shops, source="单次"):
            # 如果今天的每日定时查询已经到点，这次人工查询可视为今天已经
            # 执行过，避免刚查完后 30 秒定时器又把同一批店铺查一遍。
            try:
                scheduled_time = auto_schedule.parse_hhmm(
                    self._check_result_schedule_settings.run_time
                )
            except ValueError:
                scheduled_time = None
            if (
                self._check_result_schedule_settings.enabled
                and scheduled_time is not None
                and datetime.now().time() >= scheduled_time
            ):
                self._check_results_started_date = date.today()
                self._update_check_result_schedule_status()

    def _start_check_results(self, shops: list[str], source: str) -> bool:
        """单次按钮和每日定时查询共用的启动入口。

        先取得与 Scheduler 相同的全局运行锁，再创建子进程。拿不到锁时
        单次执行给出提示，定时执行保持待执行状态，稍后自动重试。
        """
        if self._check_reconsider_results_worker is not None:
            if source == "单次":
                msgbox.warning(self, "正在查询复议结果", "请等待复议结果查询结束后再查询申诉结果。")
            return False
        problems = PROJECT_PATHS.validate()
        if problems:
            message = "\n".join(problems)
            if source == "单次":
                msgbox.critical(self, "环境配置有误", message)
            else:
                self._append_log(f"[定时查询申诉结果] 环境配置有误，等待下次重试：{message}")
            return False

        lock_id, conflict = scheduler_core.acquire_operation_lock(
            shops,
            f"{source}查询申诉结果",
        )
        if lock_id is None:
            conflict = conflict or {}
            operation = conflict.get("operation") or "其它批量任务"
            conflict_shops = "、".join(conflict.get("shops") or []) or "未知店铺"
            message = (
                f"检测到{operation}正在运行（店铺：{conflict_shops}）。"
                "为避免多个浏览器抢占店铺登录目录，本次查询不会并发启动。"
            )
            if source == "单次":
                msgbox.warning(self, "存在运行冲突", message)
            else:
                self._update_check_result_schedule_status("等待其它任务结束")
            return False

        self._check_results_lock_id = lock_id
        self._check_results_source = source
        prefix = "定时" if source == "定时" else "GUI"
        self._append_log(f"[{prefix}] 开始查询申诉结果：{len(shops)} 家店铺")
        self._append_log(f"[结果回查] 提交时间：{self._result_query_since() or '不限制'}（北京时间；本批次固定使用此范围）")
        self._begin_progress_tracking(shops, "等待查询")
        self._check_results_total_updated = 0
        self._check_results_skipped.clear()
        self._check_results_errors.clear()
        self._check_results_button.setEnabled(False)
        self._check_reconsider_results_button.setEnabled(False)
        self._reconsider_button.setEnabled(False)
        self._start_button.setEnabled(False)  # 避免查询期间又启动批量任务抢同一批店铺的浏览器用户目录

        self._check_results_thread = QThread(self)
        self._check_results_worker = AppealResultCheckWorker(
            pddchat_python=PROJECT_PATHS.python,
            main_py=PROJECT_PATHS.main_py,
            pddchat_root=PROJECT_PATHS.root,
            shops=shops,
            submitted_since=self._result_query_since(),
        )
        self._check_results_worker.moveToThread(self._check_results_thread)
        self._check_results_thread.started.connect(self._check_results_worker.start)
        self._check_results_worker.shop_started.connect(self._on_check_results_shop_started)
        self._check_results_worker.shop_progress.connect(self._on_check_results_shop_progress)
        self._check_results_worker.shop_skipped.connect(self._on_check_results_shop_skipped)
        self._check_results_worker.shop_finished.connect(self._on_check_results_shop_finished)
        self._check_results_worker.all_finished.connect(self._on_check_results_all_finished)
        self._check_results_worker.all_finished.connect(self._check_results_thread.quit)
        self._check_results_thread.finished.connect(
            self._on_check_results_thread_finished
        )
        self._check_results_thread.finished.connect(self._check_results_thread.deleteLater)
        try:
            self._check_results_thread.start()
        except Exception:
            scheduler_core.release_operation_lock(self._check_results_lock_id)
            self._check_results_lock_id = None
            self._check_results_worker = None
            self._check_results_thread = None
            self._check_results_button.setEnabled(True)
            self._check_reconsider_results_button.setEnabled(True)
            self._reconsider_button.setEnabled(True)
            self._start_button.setEnabled(True)
            raise
        self._update_check_result_schedule_status()
        return True

    def _on_check_results_shop_started(self, shop: str) -> None:
        self._task_shop_started_at[shop] = time.monotonic()
        self._set_reconsider_status(shop, "正在查询…", "")
        self._append_log(f"[查询申诉结果][{shop}] 开始……")

    def _on_check_results_shop_progress(self, shop: str, page: int, checked: int, matched: int) -> None:
        self._run_checked_by_shop[shop] = max(0, checked)
        self._run_processed_orders = sum(self._run_checked_by_shop.values())
        self._set_reconsider_status(shop, "逐单详情回查" if page == 0 else f"查询第 {page} 页", f"已查 {checked}")
        self._refresh_run_progress()
        if page == 0:
            self._append_log(f"[查询申诉结果][{shop}] 逐单详情回查：已处理 {checked}/{matched} 个订单")
        else:
            self._append_log(f"[查询申诉结果][{shop}] 第 {page} 页，已检查 {checked} 条，匹配 {matched} 条")

    def _on_check_results_shop_skipped(self, shop: str, reason: str) -> None:
        self._check_results_skipped[shop] = reason

    def _on_check_results_shop_finished(self, shop: str, success: bool, updated_count: int, message: str) -> None:
        duration = time.monotonic() - self._task_shop_started_at.pop(
            shop, self._run_started_monotonic or time.monotonic()
        )
        self._run_terminal_shops.add(shop)
        if not success:
            self._run_failed_shops.add(shop)
            self._check_results_errors[shop] = message or "查询失败，未收到具体原因。"
        skipped = success and shop in self._check_results_skipped
        status = "无需查询" if skipped else ("已完成" if success else "查询失败")
        self._set_reconsider_status(shop, status, "")
        self._refresh_run_progress()
        if skipped:
            self._append_log(f"[查询申诉结果][{shop}] 无需查询：{self._check_results_skipped[shop]}")
        elif success:
            self._append_log(
                f"[查询申诉结果][{shop}] 完成，更新 {updated_count} 条订单结果，"
                f"耗时 {scheduler_core.format_duration(duration)}。"
            )
            self._check_results_total_updated += updated_count
        else:
            self._append_log(
                f"[查询申诉结果][{shop}] 失败：{message}"
                f"（耗时 {scheduler_core.format_duration(duration)}）"
            )

    def _on_check_results_all_finished(self) -> None:
        source = self._check_results_source
        self._complete_progress_tracking()
        elapsed = scheduler_core.format_duration(self._run_finished_elapsed or 0.0)
        self._append_log(
            f"[查询申诉结果] 本批次结束，共更新 "
            f"{self._check_results_total_updated} 条订单结果，"
            f"无需查询 {len(self._check_results_skipped)} 家，"
            f"失败 {len(self._check_results_errors)} 家，总耗时 {elapsed}。"
        )
        if self._check_results_started_date == date.today():
            self._check_result_schedule_settings.last_run_date = (
                self._check_results_started_date.isoformat()
            )
            appeal_result_schedule.save_settings(
                self._check_result_schedule_settings
            )
        scheduler_core.release_operation_lock(self._check_results_lock_id)
        self._check_results_lock_id = None
        self._check_results_button.setEnabled(True)
        self._check_reconsider_results_button.setEnabled(True)
        self._reconsider_button.setEnabled(True)
        if self._worker is None:
            self._start_button.setEnabled(True)
        self._check_results_worker = None
        self._check_results_thread = None
        self._update_check_result_schedule_status()
        if source == "单次":
            summary = f"共更新 {self._check_results_total_updated} 条订单的申诉结果。"
            if self._check_results_errors:
                details = "\n\n".join(f"{shop}：{reason}" for shop, reason in self._check_results_errors.items())
                msgbox.warning(self, "申诉结果查询失败或未完成", f"{summary}\n有 {len(self._check_results_errors)} 家店铺查询失败：\n\n{details}")
            elif self._check_results_skipped:
                details = "\n\n".join(f"{shop}：{reason}" for shop, reason in self._check_results_skipped.items())
                if len(self._check_results_skipped) == len(self._run_shops):
                    msgbox.information(self, "没有符合条件的待查订单", f"以下店铺没有符合条件的本地待查记录，未打开浏览器：\n\n{details}")
                else:
                    msgbox.information(self, "查询完成", f"{summary}\n以下店铺无需查询：\n\n{details}")
            else:
                msgbox.information(self, "查询完成", summary)

    def _on_check_results_thread_finished(self) -> None:
        """异常退出兜底：正常路径已在 all_finished 槽里释放锁。"""
        if self._check_results_worker is None:
            return
        self._append_log("[查询申诉结果] 工作线程异常结束，已释放运行锁。")
        self._complete_progress_tracking(normal=False)
        scheduler_core.release_operation_lock(self._check_results_lock_id)
        self._check_results_lock_id = None
        self._check_results_worker = None
        self._check_results_thread = None
        if (
            self._check_results_started_date == date.today()
            and self._check_result_schedule_settings.last_run_date
            != date.today().isoformat()
        ):
            # 本次线程没有走到正常 all_finished，不把今天的定时名额消费掉，
            # 让下一个 timer tick 可以重试。
            self._check_results_started_date = None
        self._check_results_button.setEnabled(True)
        self._check_reconsider_results_button.setEnabled(True)
        self._reconsider_button.setEnabled(True)
        if self._worker is None:
            self._start_button.setEnabled(True)
        self._update_check_result_schedule_status()

    # ---- 查询复议结果 ----

    def _on_check_reconsider_results_clicked(self) -> None:
        shops = self._checked_shops()
        if not shops:
            msgbox.warning(self, "没有勾选店铺", "请至少勾选一家店铺再查询复议结果。")
            return
        if (
            self._worker is not None
            or self._check_results_worker is not None
            or self._reconsider_worker is not None
            or self._check_reconsider_results_worker is not None
        ):
            msgbox.warning(self, "存在运行冲突", "当前有其它店铺任务正在运行，请等待结束后再查询复议结果。")
            return
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        lock_id, conflict = scheduler_core.acquire_operation_lock(
            shops, "批量查询复议结果"
        )
        if lock_id is None:
            conflict = conflict or {}
            operation = conflict.get("operation") or "其它任务"
            conflict_shops = "、".join(conflict.get("shops") or []) or "未知店铺"
            msgbox.warning(
                self,
                "存在运行冲突",
                f"检测到{operation}正在运行（店铺：{conflict_shops}），本次查询不会启动。",
            )
            return

        self._check_reconsider_results_lock_id = lock_id
        self._check_reconsider_results_updated = 0
        self._check_reconsider_result_counts = {}
        self._append_log(f"[查询复议结果] 开始：{len(shops)} 家店铺")
        self._append_log(f"[结果回查] 提交时间：{self._result_query_since() or '不限制'}（北京时间；本批次固定使用此范围）")
        self._begin_progress_tracking(shops, "等待查询")
        self._check_reconsider_results_button.setEnabled(False)
        self._check_results_button.setEnabled(False)
        self._reconsider_button.setEnabled(False)
        self._start_button.setEnabled(False)

        self._check_reconsider_results_thread = QThread(self)
        self._check_reconsider_results_worker = ReconsiderationResultCheckWorker(
            pddchat_python=PROJECT_PATHS.python,
            main_py=PROJECT_PATHS.main_py,
            pddchat_root=PROJECT_PATHS.root,
            shops=shops,
            submitted_since=self._result_query_since(),
        )
        self._check_reconsider_results_worker.moveToThread(
            self._check_reconsider_results_thread
        )
        self._check_reconsider_results_thread.started.connect(
            self._check_reconsider_results_worker.start
        )
        self._check_reconsider_results_worker.shop_started.connect(
            self._on_check_reconsider_shop_started
        )
        self._check_reconsider_results_worker.shop_progress.connect(
            self._on_check_reconsider_shop_progress
        )
        self._check_reconsider_results_worker.shop_finished.connect(
            self._on_check_reconsider_shop_finished
        )
        self._check_reconsider_results_worker.all_finished.connect(
            self._on_check_reconsider_all_finished
        )
        self._check_reconsider_results_worker.all_finished.connect(
            self._check_reconsider_results_thread.quit
        )
        self._check_reconsider_results_thread.finished.connect(
            self._on_check_reconsider_thread_finished
        )
        self._check_reconsider_results_thread.finished.connect(
            self._check_reconsider_results_thread.deleteLater
        )
        try:
            self._check_reconsider_results_thread.start()
        except Exception:
            scheduler_core.release_operation_lock(
                self._check_reconsider_results_lock_id
            )
            self._check_reconsider_results_lock_id = None
            self._check_reconsider_results_worker = None
            self._check_reconsider_results_thread = None
            self._restore_buttons_after_reconsider_check()
            raise

    def _on_check_reconsider_shop_started(self, shop: str) -> None:
        self._task_shop_started_at[shop] = time.monotonic()
        self._set_reconsider_status(shop, "正在查询…", "")
        self._append_log(f"[查询复议结果][{shop}] 开始……")

    def _on_check_reconsider_shop_progress(
        self, shop: str, page: int, checked: int, matched: int, updated: int
    ) -> None:
        self._run_checked_by_shop[shop] = max(0, checked)
        self._run_processed_orders = sum(self._run_checked_by_shop.values())
        self._set_reconsider_status(shop, f"查询第 {page} 页", f"已查 {checked}")
        self._refresh_run_progress()
        self._append_log(
            f"[查询复议结果][{shop}] 第 {page} 页，检查 {checked} 条，"
            f"匹配 {matched} 条，更新 {updated} 条"
        )

    def _on_check_reconsider_shop_finished(
        self, shop: str, success: bool, result: dict, message: str
    ) -> None:
        duration = time.monotonic() - self._task_shop_started_at.pop(
            shop, self._run_started_monotonic or time.monotonic()
        )
        self._run_terminal_shops.add(shop)
        incomplete = success and (result.get("complete") is False or bool(result.get("unmatched_local_count")))
        skipped = success and bool(result.get("skipped"))
        if skipped:
            self._result_check_skipped[shop] = message or result.get("message") or "没有符合条件的待查复议记录。"
        if not success or incomplete:
            issue = message or result.get("message") or ("部分复议记录未匹配，请核对平台筛选范围。" if incomplete else "未收到具体错误原因。")
            self._result_check_issues[shop] = ("未查齐：" if incomplete else "查询失败：") + issue
            self._run_failed_shops.add(shop)
        self._set_reconsider_status(shop, "查询失败" if not success else "无需查询" if skipped else "未查齐" if incomplete else "已完成", "")
        self._refresh_run_progress()
        if skipped:
            self._append_log(f"[查询复议结果][{shop}] 无需查询：{self._result_check_skipped[shop]}")
            return
        if not success:
            self._append_log(
                f"[查询复议结果][{shop}] 失败：{message}"
                f"（耗时 {scheduler_core.format_duration(duration)}）"
            )
            return
        updates = list(result.get("updates") or [])
        self._check_reconsider_results_updated += len(updates)
        for item in updates:
            status = str(item.get("status") or "复议结果待确认")
            self._check_reconsider_result_counts[status] = (
                self._check_reconsider_result_counts.get(status, 0) + 1
            )
        self._append_log(
            f"[查询复议结果][{shop}] 完成：更新 {len(updates)} 条，"
            f"本地未匹配 {int(result.get('unmatched_local_count', 0))} 条，"
            f"歧义 {int(result.get('ambiguous_count', 0))} 条，"
            f"耗时 {scheduler_core.format_duration(duration)}。"
        )
        if message:
            self._append_log(f"[查询复议结果][{shop}] {message}")

    def _restore_buttons_after_reconsider_check(self) -> None:
        self._check_reconsider_results_button.setEnabled(True)
        self._check_results_button.setEnabled(True)
        self._reconsider_button.setEnabled(True)
        if self._worker is None:
            self._start_button.setEnabled(True)

    def _on_check_reconsider_all_finished(self) -> None:
        self._complete_progress_tracking()
        elapsed = scheduler_core.format_duration(self._run_finished_elapsed or 0.0)
        scheduler_core.release_operation_lock(
            self._check_reconsider_results_lock_id
        )
        self._check_reconsider_results_lock_id = None
        self._check_reconsider_results_worker = None
        self._check_reconsider_results_thread = None
        self._restore_buttons_after_reconsider_check()
        self._show_result_check_summary(
            "复议", self._check_reconsider_results_updated,
            self._check_reconsider_result_counts, elapsed,
        )

    def _on_check_reconsider_thread_finished(self) -> None:
        if self._check_reconsider_results_worker is None:
            return
        self._append_log("[查询复议结果] 工作线程异常结束，已释放运行锁。")
        self._complete_progress_tracking(normal=False)
        scheduler_core.release_operation_lock(
            self._check_reconsider_results_lock_id
        )
        self._check_reconsider_results_lock_id = None
        self._check_reconsider_results_worker = None
        self._check_reconsider_results_thread = None
        self._restore_buttons_after_reconsider_check()

    # ---- 查询售后申诉结果 ----

    def _result_query_since(self) -> str | None:
        if not self._result_since_enabled.isChecked():
            return None
        return result_query_settings.normalize_since(
            self._result_since_edit.dateTime().toString("yyyy-MM-dd HH:mm:ss")
        )

    def _on_result_since_changed(self, *_args) -> None:
        self._result_since_edit.setEnabled(self._result_since_enabled.isChecked())
        try:
            result_query_settings.save_settings(
                self._result_since_enabled.isChecked(),
                self._result_since_edit.dateTime().toString("yyyy-MM-dd HH:mm:ss"),
            )
        except (OSError, ValueError) as exc:
            msgbox.warning(self, "提交时间设置未保存", f"本次设置仍有效，但下次启动可能恢复默认值：{exc}")

    def _on_aftersale_query_interval_changed(self, seconds: int) -> None:
        try:
            aftersale_result_settings.save_query_interval(seconds)
        except OSError as exc:
            msgbox.warning(self, "查询间隔未保存", f"本次设置仍会生效，但下次启动可能恢复默认值：{exc}")

    def _on_check_aftersale_results_clicked(self) -> None:
        shops = self._checked_shops()
        if not shops:
            msgbox.warning(self, "没有勾选店铺", "请至少勾选一家店铺再查询售后结果。")
            return
        if any((
            self._worker,
            self._check_results_worker,
            self._reconsider_worker,
            self._check_reconsider_results_worker,
            self._aftersale_worker,
            self._check_aftersale_results_worker,
        )):
            msgbox.warning(self, "存在运行冲突", "当前有其它店铺任务正在运行，请等待结束。")
            return
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        lock_id, conflict = scheduler_core.acquire_operation_lock(
            shops, "批量查询售后申诉结果"
        )
        if lock_id is None:
            conflict = conflict or {}
            operation = conflict.get("operation") or "其它任务"
            conflict_shops = "、".join(conflict.get("shops") or []) or "未知店铺"
            msgbox.warning(
                self, "存在运行冲突",
                f"检测到{operation}正在运行（店铺：{conflict_shops}），本次查询不会启动。",
            )
            return

        self._check_aftersale_results_lock_id = lock_id
        self._append_log(f"[结果回查] 提交时间：{self._result_query_since() or '不限制'}（北京时间；本批次固定使用此范围）")
        self._check_aftersale_results_updated = 0
        self._check_aftersale_result_counts = {}
        query_interval = self._aftersale_query_interval_spin.value()
        self._append_log(
            f"[查询售后结果] 开始：{len(shops)} 家店铺；"
            f"翻页、详情及店铺切换间隔 {query_interval} 秒"
        )
        self._begin_progress_tracking(shops, "等待查询")
        self._check_aftersale_results_button.setEnabled(False)
        self._aftersale_query_interval_spin.setEnabled(False)
        self._check_results_button.setEnabled(False)
        self._check_reconsider_results_button.setEnabled(False)
        self._start_button.setEnabled(False)

        self._check_aftersale_results_thread = QThread(self)
        self._check_aftersale_results_worker = AftersaleResultCheckWorker(
            pddchat_python=PROJECT_PATHS.python,
            main_py=PROJECT_PATHS.main_py,
            pddchat_root=PROJECT_PATHS.root,
            shops=shops,
            query_interval_seconds=query_interval,
            submitted_since=self._result_query_since(),
        )
        worker = self._check_aftersale_results_worker
        thread = self._check_aftersale_results_thread
        worker.moveToThread(thread)
        thread.started.connect(worker.start)
        worker.shop_started.connect(self._on_check_aftersale_shop_started)
        worker.shop_progress.connect(self._on_check_aftersale_shop_progress)
        worker.shop_finished.connect(self._on_check_aftersale_shop_finished)
        worker.shop_waiting.connect(self._on_check_aftersale_shop_waiting)
        worker.all_finished.connect(self._on_check_aftersale_all_finished)
        worker.all_finished.connect(thread.quit)
        thread.finished.connect(self._on_check_aftersale_thread_finished)
        thread.finished.connect(thread.deleteLater)
        try:
            thread.start()
        except Exception:
            scheduler_core.release_operation_lock(lock_id)
            self._check_aftersale_results_lock_id = None
            self._check_aftersale_results_worker = None
            self._check_aftersale_results_thread = None
            self._restore_buttons_after_aftersale_check()
            raise

    def _on_check_aftersale_shop_started(self, shop: str) -> None:
        self._task_shop_started_at[shop] = time.monotonic()
        self._set_reconsider_status(shop, "正在查询售后结果…", "")
        self._append_log(f"[查询售后结果][{shop}] 开始……")

    def _on_check_aftersale_shop_waiting(self, shop: str, seconds: int) -> None:
        self._set_reconsider_status(shop, f"间隔等待 {seconds} 秒…", "")
        self._append_log(f"[查询售后结果] 等待 {seconds} 秒后开始店铺：{shop}")

    def _on_check_aftersale_shop_progress(
        self, shop: str, page: int, checked: int, matched: int, updated: int
    ) -> None:
        self._run_checked_by_shop[shop] = max(0, checked)
        self._run_processed_orders = sum(self._run_checked_by_shop.values())
        self._set_reconsider_status(shop, f"查询第 {page} 页", f"已查 {checked}")
        self._refresh_run_progress()
        self._append_log(
            f"[查询售后结果][{shop}] 第 {page} 页，检查 {checked} 条，"
            f"匹配 {matched} 条，更新 {updated} 条"
        )

    def _on_check_aftersale_shop_finished(
        self, shop: str, success: bool, result: dict, message: str
    ) -> None:
        duration = time.monotonic() - self._task_shop_started_at.pop(
            shop, self._run_started_monotonic or time.monotonic()
        )
        self._run_terminal_shops.add(shop)
        incomplete = success and (result.get("complete") is False or bool(result.get("unmatched_local_count")) or bool(result.get("truncated")))
        skipped = success and bool(result.get("skipped"))
        if skipped:
            self._result_check_skipped[shop] = message or result.get("message") or "没有符合条件的待查售后申诉记录。"
        if not success or incomplete:
            issue = message or result.get("message") or ("售后申诉记录未查齐，请核对平台筛选范围及翻页上限。" if incomplete else "未收到具体错误原因。")
            self._result_check_issues[shop] = ("未查齐：" if incomplete else "查询失败：") + issue
            self._run_failed_shops.add(shop)
        self._set_reconsider_status(shop, "查询失败" if not success else "无需查询" if skipped else "未查齐" if incomplete else "已完成", "")
        self._refresh_run_progress()
        if skipped:
            self._append_log(f"[查询售后结果][{shop}] 无需查询：{self._result_check_skipped[shop]}")
            return
        if not success:
            self._append_log(
                f"[查询售后结果][{shop}] 失败：{message}"
                f"（耗时 {scheduler_core.format_duration(duration)}）"
            )
            return
        updates = list(result.get("updates") or [])
        self._check_aftersale_results_updated += len(updates)
        for item in updates:
            if item.get("detail"):
                self._append_log(
                    f"[查询售后结果][{shop}][{item.get('order_no', '')}] {item['detail']}"
                )
            status = str(item.get("status") or "结果待确认")
            self._check_aftersale_result_counts[status] = (
                self._check_aftersale_result_counts.get(status, 0) + 1
            )
        self._append_log(
            f"[查询售后结果][{shop}] 完成：平台检查 {int(result.get('checked_count', 0))} 条，"
            f"更新 {len(updates)} 条，"
            f"忽略非本地申诉 {int(result.get('ignored_platform_count', 0))} 条，"
            f"本地未匹配 {int(result.get('unmatched_local_count', 0))} 条，"
            f"歧义 {int(result.get('ambiguous_count', 0))} 条，"
            f"耗时 {scheduler_core.format_duration(duration)}。"
        )
        if message:
            self._append_log(f"[查询售后结果][{shop}] {message}")

    def _restore_buttons_after_aftersale_check(self) -> None:
        self._check_aftersale_results_button.setEnabled(True)
        self._aftersale_query_interval_spin.setEnabled(True)
        self._check_results_button.setEnabled(True)
        self._check_reconsider_results_button.setEnabled(True)
        if self._worker is None:
            self._start_button.setEnabled(True)

    def _on_check_aftersale_all_finished(self) -> None:
        self._complete_progress_tracking()
        elapsed = scheduler_core.format_duration(self._run_finished_elapsed or 0.0)
        scheduler_core.release_operation_lock(self._check_aftersale_results_lock_id)
        self._check_aftersale_results_lock_id = None
        self._check_aftersale_results_worker = None
        self._check_aftersale_results_thread = None
        self._restore_buttons_after_aftersale_check()
        self._show_result_check_summary(
            "售后", self._check_aftersale_results_updated,
            self._check_aftersale_result_counts, elapsed,
        )

    def _show_result_check_summary(self, kind: str, updated: int, counts: dict, elapsed: str) -> None:
        summary = f"共更新 {updated} 条。"
        count_text = "，".join(f"{status} {count} 条" for status, count in sorted(counts.items()))
        if count_text:
            summary += "\n" + count_text
        issues = self._result_check_issues
        skipped = self._result_check_skipped
        self._append_log(
            f"[查询{kind}结果] 本批次结束，共更新 {updated} 条，"
            f"无需查询 {len(skipped)} 家，失败或未查齐 {len(issues)} 家，总耗时 {elapsed}。"
        )
        details = "\n\n".join(
            [f"{shop}：{reason}" for shop, reason in issues.items()]
            + [f"{shop}：无需查询，{reason}" for shop, reason in skipped.items()]
        )
        if issues:
            msgbox.warning(self, f"{kind}结果查询失败或未查齐", f"{summary}\n以下店铺未完成查询：\n\n{details}")
        elif skipped and len(skipped) == len(self._run_shops):
            msgbox.information(self, "没有符合条件的待查订单", f"所选店铺无需查询，未打开浏览器：\n\n{details}")
        else:
            msgbox.information(self, f"{kind}结果查询完成", summary + (f"\n\n{details}" if details else ""))

    def _on_check_aftersale_thread_finished(self) -> None:
        if self._check_aftersale_results_worker is None:
            return
        self._append_log("[查询售后结果] 工作线程异常结束，已释放运行锁。")
        self._complete_progress_tracking(normal=False)
        scheduler_core.release_operation_lock(self._check_aftersale_results_lock_id)
        self._check_aftersale_results_lock_id = None
        self._check_aftersale_results_worker = None
        self._check_aftersale_results_thread = None
        self._restore_buttons_after_aftersale_check()

    def _on_stop_clicked(self) -> None:
        self._request_stop()

    def _request_stop(self, reason: str = "") -> None:
        """人工点"优雅停止"按钮、或者定时任务到点了都调这个——不区分
        当前这一轮是怎么启动的，只要没在跑就不用管，在跑就一律走同一条
        "请求优雅停止"的路径。reason 非空时会带进日志文案，方便事后
        从日志里区分是人工点的还是定时任务自动触发的。"""
        if self._worker is None or self._stopping:
            return
        self._stopping = True
        self._stop_button.setEnabled(False)
        self._stop_button.setText("停止中…")
        suffix = f"（{reason}）" if reason else ""
        self._append_log(f"[GUI] 已请求停止{suffix}，等待当前操作告一段落……")
        self._worker.request_stop()

    def _on_stopped(self) -> None:
        if self._pipeline_active:
            self._pipeline_phase_complete = True
        self._release_topmost_browser()
        if self._run_started_monotonic is not None:
            self._run_finished_elapsed = time.monotonic() - self._run_started_monotonic
        # 非人工停止时，调度器正常返回就代表整轮已经收尾。
        # 其中“今天已完成、本轮直接跳过”的店铺不会逐家发
        # worker_done，因此需要在这里统一记为完成，否则界面会卡在
        # 100% 以下。人工优雅停止则保留当前真实进度。
        if not self._stopping:
            self._run_terminal_shops.update(self._run_shops)
        elapsed = scheduler_core.format_duration(self._run_finished_elapsed or 0.0)
        completed = len(self._run_terminal_shops)
        total = len(self._run_shops)
        finish_word = "已停止" if self._stopping and completed < total else "已结束"
        self._append_log(
            f"[GUI] 本次运行{finish_word}：完成 {completed}/{total} 家，"
            f"处理 {self._run_processed_orders} 单，总耗时 {elapsed}，"
            f"失败 {len(self._run_failed_shops)} 家。"
        )
        self._refresh_run_progress()
        self._run_started_monotonic = None
        self._set_form_enabled(True)
        self._stop_button.setEnabled(False)
        self._stop_button.setText("优雅停止")
        self._worker = None
        self._stopping = False
        self._update_auto_status_label()
        # QThread 对象留给 finished -> deleteLater 自然回收，这里不用手动清。

    # ---- 定时自动运行 ----

    def _on_auto_enable_toggled(self, _state: int) -> None:
        checked = self._auto_enable_check.isChecked()

        if checked and not self._auto_settings.enabled:
            # 关 -> 开：这是一次新的"打开开关"动作，弹一次醒目确认，
            # 确认之后往后每天的自动触发才不再逐次弹窗（用户已经知悉后果）。
            start = self._auto_start_edit.time().toString("HH:mm")
            end = self._auto_end_edit.time().toString("HH:mm")
            confirmed = msgbox.confirm_yes_no(
                self,
                "启用每日自动运行",
                f"开启后，每天 {start}–{end} 会自动开始真实提交申诉，不会再逐次弹窗确认；\n"
                f"{end} 后会自动优雅停止（不会打断正在处理中的订单，只是不再开始新的），"
                "当天没跑完的订单会顺延到第二天继续处理。\n\n确定要开启吗？",
            )
            if not confirmed:
                self._auto_enable_check.blockSignals(True)
                self._auto_enable_check.setChecked(False)
                self._auto_enable_check.blockSignals(False)
                return

        self._auto_settings.enabled = checked
        auto_schedule.save_settings(self._auto_settings)
        self._append_log(f"[GUI] 定时自动运行已{'启用' if checked else '关闭'}。")
        self._update_auto_status_label()
        self._check_auto_schedule()

    def _on_auto_time_changed(self, _time) -> None:
        self._auto_settings.start_time = self._auto_start_edit.time().toString("HH:mm")
        self._auto_settings.end_time = self._auto_end_edit.time().toString("HH:mm")
        auto_schedule.save_settings(self._auto_settings)
        self._update_auto_status_label()

    def _currently_in_auto_window(self) -> bool:
        """当前时间是不是落在"启用每日自动运行"设置的时间段内。解析失败
        （理论上不会发生，QTimeEdit 保证格式）时保守返回 False——不管是
        判断要不要自动启动/停止，还是判断能不能手动启动，"解析不出时间"
        都应该等价于"不在允许的时间段内"，不应该放行。
        """
        try:
            start = auto_schedule.parse_hhmm(self._auto_settings.start_time)
            end = auto_schedule.parse_hhmm(self._auto_settings.end_time)
        except ValueError:
            return False
        return auto_schedule.is_within_window(datetime.now().time(), start, end)

    def _update_auto_status_label(self) -> None:
        if not self._auto_settings.enabled:
            self._auto_status_label.setText("定时运行：未启用")
            return

        start, end = self._auto_settings.start_time, self._auto_settings.end_time
        in_window = self._currently_in_auto_window()

        if self._worker is not None:
            state = "运行中"
        elif in_window:
            state = "在窗口内（即将或已经自动启动）"
        else:
            state = "窗口外，等待下次自动启动"
        self._auto_status_label.setText(f"定时运行：已启用（{start}–{end}），当前{state}")

    def _check_auto_schedule(self) -> None:
        if self._pipeline_active:
            return
        self._update_auto_status_label()

        if not self._auto_settings.enabled:
            return

        now_dt = datetime.now()
        in_window = self._currently_in_auto_window()

        if in_window and self._worker is None and self._auto_started_date != now_dt.date():
            if (
                self._check_results_worker is not None
                or self._check_reconsider_results_worker is not None
                or self._reconsider_worker is not None
            ):
                # 不消费 _auto_started_date——留到下一个 30 秒 tick 再判断一次，
                # 等查询申诉结果跑完之后自然就能正常自动启动，不会因为撞上
                # 这一个 tick 就错过今天整个窗口。
                return
            # GUI 之外启动的批量任务或结果查询也使用同一把运行锁。这里不
            # 消费今天的自动运行名额，等对方结束后的下一个 tick 再启动。
            if scheduler_core.active_operation_lock() is not None:
                return
            shops = self._checked_shops()
            if not shops:
                self._append_log("[GUI] 定时任务到点了，但当前没有勾选任何店铺，跳过本次自动启动。")
                return
            problems = PROJECT_PATHS.validate()
            if problems:
                self._append_log(f"[GUI] 定时任务到点了，但环境配置有误，跳过：{'；'.join(problems)}")
                return
            pool_size = self._pool_size_spin.value()
            self._auto_started_date = now_dt.date()
            self._append_log(f"[GUI] 定时任务自动启动：{len(shops)} 家店铺，并发上限 {pool_size}，真实提交，只跑一轮")
            self._start_run(shops, pool_size)
        elif not in_window and self._worker is not None and not self._stopping:
            self._request_stop(reason="定时任务到点")

    # ---- 查询申诉结果：每日定时执行 ----

    def _on_check_result_schedule_changed(self, *_args) -> None:
        self._check_result_schedule_settings.enabled = (
            self._check_result_schedule_enable.isChecked()
        )
        self._check_result_schedule_settings.run_time = (
            self._check_result_schedule_time.time().toString("HH:mm")
        )
        appeal_result_schedule.save_settings(
            self._check_result_schedule_settings
        )
        self._update_check_result_schedule_status()
        self._check_result_schedule()

    def _update_check_result_schedule_status(
        self,
        override: str | None = None,
    ) -> None:
        settings = self._check_result_schedule_settings
        if not settings.enabled:
            self._check_result_schedule_status.setText("申诉结果定时查询：未启用")
            return
        if override:
            state = override
        elif self._check_results_started_date == date.today():
            state = "今天已执行"
        elif self._check_results_worker is not None:
            state = "查询进行中"
        else:
            try:
                run_time = auto_schedule.parse_hhmm(settings.run_time)
                due = datetime.now().time() >= run_time
            except ValueError:
                state = "执行时间无效"
            else:
                state = "已到执行时间，等待空闲" if due else "等待执行时间"
        self._check_result_schedule_status.setText(
            f"申诉结果定时查询：每天 {settings.run_time}，当前{state}"
        )

    def _check_result_schedule(self) -> None:
        if self._pipeline_active:
            return
        """到点后自动执行一次；遇到批量任务或其它进程时不抢占、稍后重试。"""
        self._update_check_result_schedule_status()
        settings = self._check_result_schedule_settings
        if not settings.enabled:
            return

        now_dt = datetime.now()
        if self._check_results_started_date == now_dt.date():
            return
        try:
            run_time = auto_schedule.parse_hhmm(settings.run_time)
        except ValueError:
            return
        if now_dt.time() < run_time:
            return

        # 本窗口内的批量任务、正在进行的单次查询，以及其它 GUI/CLI 进程
        # 都优先；不标记“今天已执行”，下一个 30 秒 tick 会继续尝试。
        if (
            self._worker is not None
            or self._check_results_worker is not None
            or self._check_reconsider_results_worker is not None
            or self._reconsider_worker is not None
        ):
            self._update_check_result_schedule_status("等待当前任务结束")
            return
        if scheduler_core.active_operation_lock() is not None:
            self._update_check_result_schedule_status("等待其它任务结束")
            return

        shops = self._checked_shops()
        if not shops:
            self._update_check_result_schedule_status("等待选择可用店铺")
            return

        if self._start_check_results(shops, source="定时"):
            self._check_results_started_date = now_dt.date()
            self._append_log(
                f"[定时查询申诉结果] 今日任务已启动，店铺数：{len(shops)}"
            )
            self._update_check_result_schedule_status()

    # ---- 拟人休息 ----

    def _save_native_properties(self, enabled: bool) -> None:
        try:
            browser_settings.save_native_properties(enabled)
        except OSError as exc:
            self._native_properties_check.blockSignals(True)
            self._native_properties_check.setChecked(not enabled)
            self._native_properties_check.blockSignals(False)
            msgbox.warning(self, "保存失败", f"浏览器模式保存失败：{exc}")

    def _update_browser_controls(self) -> None:
        firefox = self._browser_engine_combo.currentData() == "firefox"
        for widget in self._chrome_controls:
            widget.setEnabled(not firefox)
        self._browser_engine_hint.setText(
            "Firefox 使用配套浏览器；首次切换请重新登录店铺。"
            if firefox else "Chrome 使用原有登录目录；可在下方选择本地 chrome.exe。"
        )

    def _save_browser_engine(self, _index: int) -> None:
        previous = browser_settings.browser_engine()
        engine = self._browser_engine_combo.currentData()
        shops_page = getattr(self.window(), "_shops_page", None)
        login_busy = shops_page is not None and (
            shops_page._login_check is not None or shops_page._batch_is_running()
        )
        if self.is_running() or login_busy:
            self._browser_engine_combo.blockSignals(True)
            self._browser_engine_combo.setCurrentIndex(self._browser_engine_combo.findData(previous))
            self._browser_engine_combo.blockSignals(False)
            msgbox.warning(self, "请先停止任务", "请先停止正在运行的任务，再切换浏览器。")
            return
        try:
            browser_settings.save_browser_engine(engine)
        except (OSError, ValueError) as exc:
            self._browser_engine_combo.blockSignals(True)
            self._browser_engine_combo.setCurrentIndex(self._browser_engine_combo.findData(previous))
            self._browser_engine_combo.blockSignals(False)
            msgbox.warning(self, "保存失败", str(exc))
            return
        self._update_browser_controls()
        if shops_page is not None:
            shops_page._model.login_checks.clear()
            shops_page._poller.start()
        self._shop_poller.start()

    def _choose_chrome_executable(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Google Chrome 的 chrome.exe",
            self._chrome_path_edit.text() or "C:/Program Files/Google/Chrome/Application",
            "Google Chrome (chrome.exe)"
        )
        if path:
            self._set_chrome_executable(path)

    def _set_chrome_executable(self, path: str) -> None:
        try:
            browser_settings.save_chrome_executable(path)
        except (OSError, ValueError) as exc:
            msgbox.warning(self, "保存失败", str(exc))
            return
        self._chrome_path_edit.setText(path)

    def _on_rest_settings_changed(self, *_args) -> None:
        """五个控件（启用开关 + 四个数字框）共用这一个槽——每次改动都
        原样重读全部五个控件当前状态存盘，不区分是哪一个触发的，跟
        _on_auto_time_changed 是同一种"改了就立刻存盘"模式。不需要像
        _on_auto_enable_toggled 那样在打开开关时弹确认框——这个开关不会
        让软件开始做一件它本来不会做的新事情（不像"启用每日自动运行"
        第一次打开会让软件在没人盯着的时候开始真实提交），只是调整一次
        已经由用户自己（手动或定时）启动的运行本身的节奏快慢，不需要
        同等级别的郑重确认。
        """
        self._rest_settings.enabled = self._rest_enable_check.isChecked()
        self._rest_settings.min_hours = self._rest_min_hours_spin.value()
        self._rest_settings.max_hours = self._rest_max_hours_spin.value()
        self._rest_settings.min_minutes = self._rest_min_minutes_spin.value()
        self._rest_settings.max_minutes = self._rest_max_minutes_spin.value()
        rest_settings.save_settings(self._rest_settings)

    def _set_form_enabled(self, enabled: bool) -> None:
        self._shop_list.setEnabled(enabled)
        self._pool_size_spin.setEnabled(enabled)
        self._rest_enable_check.setEnabled(enabled)
        self._rest_min_hours_spin.setEnabled(enabled)
        self._rest_max_hours_spin.setEnabled(enabled)
        self._rest_min_minutes_spin.setEnabled(enabled)
        self._rest_max_minutes_spin.setEnabled(enabled)
        self._start_button.setEnabled(enabled)
        # 查询申诉结果跟批量任务共用同一批店铺的 Chrome 用户目录，批量任务
        # 跑着的时候不能顺手点查询——这里只负责"批量任务开始/结束"这个
        # 方向；反过来"查询申诉结果进行中要不要禁用这个按钮"由
        # _on_check_results_clicked/_on_check_results_all_finished 自己管理
        # （见那两处，不在这个通用方法里处理，避免跟这里的 enabled 语义
        # 互相覆盖）。
        self._check_results_button.setEnabled(enabled)
        self._check_reconsider_results_button.setEnabled(enabled)
        self._reconsider_button.setEnabled(enabled)
        self._reconsider_pool_size_spin.setEnabled(enabled)
        self._reconsider_limit_spin.setEnabled(enabled)
        if self._reconsider_worker is None:
            self._stop_reconsider_button.setEnabled(False)
        self._aftersale_submit_button.setEnabled(enabled)
        self._aftersale_pool_size_spin.setEnabled(enabled)
        self._aftersale_limit_spin.setEnabled(enabled)
        if self._aftersale_worker is None:
            self._stop_aftersale_button.setEnabled(False)

    # ---- 事件展示 ----

    def _on_event(self, event: scheduler_core.SchedulerEvent) -> None:
        self._pipeline_event(event)
        memory_monitor.increment_runtime_metric('scheduler_events_processed')
        self._track_run_progress(event)
        if event.line:
            self._append_log(event.line)

        if event.shop is not None:
            self._update_status_row(event)
            if event.type == "shop_activity":
                self._maybe_follow_browser(event.shop)
                self._activate_topmost_browser(event.shop)
            elif event.type in {
                "order_result", "order_error", "worker_done", "worker_empty_queue",
                "worker_init_failed", "worker_comm_lost", "worker_too_many_failures",
            }:
                self._release_topmost_browser(event.shop)

    def _append_log(self, text: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        raw_text = str(text)
        self._max_log_event_chars_seen = max(
            self._max_log_event_chars_seen, len(raw_text)
        )
        view_text = _limit_log_text(raw_text, _LOG_VIEW_MAX_CHARS_PER_EVENT)
        display_text = "\n".join(
            f"[{stamp}] {line}" if line else ""
            for line in view_text.splitlines()
        )
        self._log_view.appendPlainText(display_text)
        scrollbar = self._log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())
        memory_monitor.update_runtime_metrics(
            log_event_last_chars=len(raw_text),
            log_event_max_chars=self._max_log_event_chars_seen,
            log_view_blocks=self._log_view.document().blockCount(),
        )
        # 界面上的 QPlainTextEdit 只是内存态展示（还限了最多
        # _LOG_MAX_BLOCKS 行），同步写一份到落盘日志——运营出问题时
        # 才有东西能发给开发排查，不依赖软件还开着、还没滚动超限。
        logger.info(_limit_log_text(raw_text, _LOG_FILE_MAX_CHARS_PER_EVENT))

    def _begin_progress_tracking(self, shops: list[str], initial_status: str) -> None:
        """为任意一种店铺批量任务重置同一套进度界面。"""
        self._result_check_skipped.clear()
        self._result_check_issues.clear()
        self._status_table.setRowCount(0)
        self._row_index.clear()
        self._browser_buttons.clear()
        self._run_shops = list(shops)
        self._run_terminal_shops.clear()
        self._run_failed_shops.clear()
        self._run_initial_totals.clear()
        self._run_remaining.clear()
        self._run_checked_by_shop.clear()
        self._task_shop_started_at.clear()
        self._task_order_started_at.clear()
        self._run_processed_orders = 0
        self._run_started_monotonic = time.monotonic()
        self._run_finished_elapsed = None
        self._run_eta_seconds = None
        self._run_progress_max_seen = 0.0
        self._run_progress_bar.setRange(0, 1000)
        self._run_progress_bar.setValue(0)
        self._run_progress_bar.setFormat("总体进度 %p%")
        for shop in shops:
            self._set_reconsider_status(shop, initial_status, "")
        self._refresh_run_progress()

    def _complete_progress_tracking(self, *, normal: bool = True) -> None:
        if self._run_started_monotonic is not None:
            self._run_finished_elapsed = time.monotonic() - self._run_started_monotonic
        if normal:
            self._run_terminal_shops.update(self._run_shops)
        self._refresh_run_progress()
        self._run_started_monotonic = None

    def _track_run_progress(self, event: scheduler_core.SchedulerEvent) -> None:
        """把调度器事件折算成界面进度。只处理批量申诉调度事件，
        复议/结果回查各自的 worker 信号不会进入这里。
        """
        shop = event.shop
        if shop is None or shop not in self._run_shops:
            return

        if event.type == "worker_ready":
            total = max(0, int(event.data.get("queue_size") or 0))
            self._run_initial_totals[shop] = total
            self._run_remaining[shop] = total

        remaining = event.data.get("remaining")
        if remaining is not None:
            try:
                self._run_remaining[shop] = max(0, int(remaining))
            except (TypeError, ValueError):
                pass

        if event.type in {"order_result", "order_error"}:
            self._run_processed_orders += 1

        terminal_types = {
            "worker_done",
            "worker_empty_queue",
            "worker_init_failed",
            "worker_comm_lost",
            "worker_too_many_failures",
        }
        failure_types = {
            "worker_init_failed",
            "worker_comm_lost",
            "worker_too_many_failures",
        }
        if event.type in terminal_types:
            self._run_terminal_shops.add(shop)
        if event.type in failure_types:
            self._run_failed_shops.add(shop)
        self._refresh_run_progress()

    def _refresh_run_progress(self) -> None:
        if not hasattr(self, "_run_progress_bar"):
            return
        total = len(self._run_shops)
        if not total:
            self._run_progress_bar.setValue(0)
            self._run_progress_bar.setFormat("尚未开始")
            self._run_progress_summary.setText("尚未开始")
            return

        progress = scheduler_core.overall_shop_progress(
            self._run_shops,
            self._run_terminal_shops,
            self._run_initial_totals,
            self._run_remaining,
        )
        self._run_progress_max_seen = max(self._run_progress_max_seen, progress)
        progress = self._run_progress_max_seen
        elapsed = (
            time.monotonic() - self._run_started_monotonic
            if self._run_started_monotonic is not None
            else (self._run_finished_elapsed or 0.0)
        )

        all_finished = len(self._run_terminal_shops) >= total
        if all_finished:
            progress = 1.0
            self._run_progress_max_seen = 1.0
            self._run_eta_seconds = 0.0
        elif elapsed >= 10.0 and progress >= 0.02:
            raw_eta = elapsed * (1.0 - progress) / progress
            self._run_eta_seconds = (
                raw_eta
                if self._run_eta_seconds is None
                else self._run_eta_seconds * 0.75 + raw_eta * 0.25
            )

        if self._run_started_monotonic is not None and progress <= 0 and not all_finished:
            # 初始化/首家店尚未完成时还没有足够样本计算百分比，
            # 用 Qt 的不确定进度动画表明程序仍在工作，避免像卡死。
            self._run_progress_bar.setRange(0, 0)
            self._run_progress_bar.setFormat("")
        else:
            self._run_progress_bar.setRange(0, 1000)
            self._run_progress_bar.setValue(round(progress * 1000))
            self._run_progress_bar.setFormat("总体进度 %p%")
        eta_text = (
            "0秒"
            if all_finished
            else (
                f"约 {scheduler_core.format_duration(self._run_eta_seconds)}"
                if self._run_eta_seconds is not None
                else "计算中"
            )
        )
        self._run_progress_summary.setText(
            f"已完成 {len(self._run_terminal_shops)}/{total} 家  ｜  "
            f"已处理 {self._run_processed_orders} 单  ｜  "
            f"已运行 {scheduler_core.format_duration(elapsed)}  ｜  "
            f"预计剩余 {eta_text}"
        )

    def _on_open_log_folder_clicked(self) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(LOG_DIR))  # noqa: S606  (Windows 专用，跟本项目其余部分一致)

    def _on_export_memory_diagnostics(self) -> None:
        import memory_monitor
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存内存诊断包", f"memory-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip", "ZIP (*.zip)")
        if not destination:
            return
        try:
            memory_monitor.export_bundle(destination)
        except Exception as error:
            QMessageBox.warning(self, "导出失败", f"无法保存诊断包：{type(error).__name__}，请检查目录权限和磁盘空间。")
        else:
            QMessageBox.information(self, "导出完成", "内存诊断包已保存。请同时提供异常时间、任务类型、并发店铺数和任务管理器截图。")

    def _on_export_captcha_diagnostics(self) -> None:
        destination, _ = QFileDialog.getSaveFileName(
            self, "保存验证码诊断包", f"captcha-diagnostics-{datetime.now():%Y%m%d-%H%M%S}.zip", "ZIP (*.zip)")
        if not destination:
            return
        self._captcha_export_button.setEnabled(False)
        worker = CaptchaExportWorker(PROJECT_PATHS.root / "debug" / "captcha_attempts", destination, self)
        self._captcha_export_worker = worker
        worker.result_ready.connect(self._on_captcha_export_result)
        worker.finished.connect(lambda: self._captcha_export_button.setEnabled(True))
        worker.finished.connect(worker.deleteLater)
        worker.finished.connect(lambda: setattr(self, "_captcha_export_worker", None))
        worker.start()

    def is_exporting_captcha_diagnostics(self) -> bool:
        return getattr(self, "_captcha_export_worker", None) is not None

    def _on_captcha_export_result(self, result) -> None:
        if isinstance(result, str):
            QMessageBox.warning(self, "导出失败", result)
            return
        rate = f"{result['pass_rate']:.1%}" if result['attempts'] else "暂无记录"
        QMessageBox.information(self, "验证码诊断导出完成",
            f"自动尝试 {result['attempts']} 次，通过 {result['passed']} 次，未通过 {result['failed']} 次。\n"
            f"自动尝试通过率：{rate}\n包内包含分店铺/题型统计及失败样本。")

    def _update_status_row(self, event: scheduler_core.SchedulerEvent) -> None:
        shop = event.shop
        assert shop is not None
        row = self._ensure_status_row(shop)

        status_text = _ROW_STATUS_TEXT.get(event.type)
        if event.type == "worker_too_many_failures":
            count = int(event.data.get("consecutive_failures") or 3)
            status_text = f"连续{count}单失败，已跳过店铺"
        if event.type == "shop_activity":
            stage = str(event.data.get("stage") or "正在处理")
            order_no = str(event.data.get("order_no") or "")
            status_text = f"{stage}｜{order_no}" if order_no else stage
        if status_text is not None:
            self._status_table.item(row, 1).setText(status_text)
            self._status_table.item(row, 1).setToolTip(status_text)

        remaining = event.data.get("remaining")
        if remaining is None and event.type == "worker_ready":
            remaining = event.data.get("queue_size")
        if remaining is not None:
            self._status_table.item(row, 2).setText(str(remaining))
