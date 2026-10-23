"""B 页"新增店铺"/"重新登录"共用的引导对话框。

流程：（新增店铺时）填店铺名 → 点"开始登录" → 起 --login-only 子进程 →
真实浏览器窗口弹出，人工完成登录 → 子进程轮询检测到登录完成 → 对话框
显示成功、可以关闭。重新登录时不需要填名字，直接跳到"开始登录"这一步。

注意：本对话框目前没有"我确认已登录，强制继续"这个兜底按钮——
--login-only 子进程侧（见 login.py 的 wait_for_login_polling）目前只支持
被动轮询检测，没有做"收到指令立刻插队检测一次"这个交互，加一个假的
"强制继续"按钮只会误导用户（点了也不能真的验证登录状态），所以先不做。
如果实测发现轮询检测经常误判，需要回头给 --login-only 补一个 check_now
指令，再在这里加对应按钮。
"""

from __future__ import annotations

from PyQt5.QtCore import QThread
from PyQt5.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
)

from ..services import msgbox, shop_repo
from ..services.project_paths import PROJECT_PATHS  # noqa: F401 (import 时机很关键，见下)
from ..workers.login_worker import LoginWorker

# project_paths 模块在 import 时会把主项目根目录塞进 sys.path（见其模块
# 文档），必须放在这行 import scheduler_core 之前，才能保证不管这个对话框
# 模块是不是全应用里第一个被 import 的地方，都能正确找到 scheduler_core.py。
import scheduler_core


class AddShopDialog(QDialog):
    def __init__(self, shop: str | None = None, parent=None) -> None:
        """shop=None：新增店铺（需要填店铺名）；shop="已有店铺名"：重新
        登录该店铺（名字固定，直接可以点开始登录）。"""
        super().__init__(parent)
        self._is_relogin = shop is not None
        self._thread: QThread | None = None
        self._worker: LoginWorker | None = None
        self.result_shop: str | None = None  # 成功后调用方读这个拿到店铺名

        self.setWindowTitle("重新登录" if self._is_relogin else "新增店铺")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        layout.addWidget(QLabel("店铺名："))
        self._name_edit = QLineEdit(shop or "")
        self._name_edit.setPlaceholderText("给这家店铺起个便于识别的名字")
        self._name_edit.setEnabled(not self._is_relogin)
        layout.addWidget(self._name_edit)

        self._status_label = QLabel("填好店铺名后点「开始登录」。")
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._progress = QProgressBar()
        self._progress.setRange(0, 0)  # 不确定态（忙碌动画），不是"能算出百分比"的进度
        self._progress.setVisible(False)
        layout.addWidget(self._progress)

        self._start_button = QPushButton("开始登录")
        self._start_button.clicked.connect(self._on_start_clicked)
        layout.addWidget(self._start_button)

        # QDialogButtonBox 的标准按钮文案默认是英文（"Cancel"）——Qt 自带的
        # 中文翻译要单独加载 .qm 翻译文件才会生效，这里没有搭那套机制，
        # 直接手动把文案改成中文更省事可靠。
        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self._buttons.rejected.connect(self._on_cancel)
        layout.addWidget(self._buttons)

    # ---- 交互 ----

    def _on_start_clicked(self) -> None:
        shop = self._name_edit.text().strip()
        if not shop:
            msgbox.warning(self, "店铺名不能为空", "请先填写店铺名。")
            return

        if not self._is_relogin:
            existing = {s.shop for s in shop_repo.list_shop_summaries()}
            if shop in existing:
                msgbox.warning(
                    self, "店铺已存在",
                    f"「{shop}」已经在店铺列表里了，如果是想刷新登录态，"
                    "请在列表里对这家店铺用「重新登录」。",
                )
                return

        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return

        lock = scheduler_core.read_lock_status()
        if lock is not None and scheduler_core.is_pid_running(lock.get("pid", -1)):
            # 只读检查，不是完整加锁——登录流程本身不写这把锁，只是提醒一下
            # 用户"批量任务可能还在跑"，不做精细的按店铺区分。
            proceed = msgbox.confirm_yes_no(
                self,
                "批量任务正在运行",
                "当前有批量任务正在运行，同时登录可能造成冲突，"
                "建议先在「运行控制」页停止批量任务再登录。是否仍要继续？",
                yes_text="仍要继续",
                no_text="取消",
            )
            if not proceed:
                return

        self._name_edit.setEnabled(False)
        self._start_button.setEnabled(False)
        self._progress.setVisible(True)
        self._status_label.setText("正在启动浏览器……")

        self._thread = QThread(self)
        self._worker = LoginWorker(
            pddchat_python=PROJECT_PATHS.python,
            main_py=PROJECT_PATHS.main_py,
            pddchat_root=PROJECT_PATHS.root,
            shop=shop,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.start)
        self._worker.waiting.connect(self._on_waiting)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._thread.quit)
        self._thread.finished.connect(self._thread.deleteLater)
        self._thread.start()

        self._pending_shop_name = shop

    def _on_waiting(self) -> None:
        self._status_label.setText(
            "浏览器窗口已打开，请在里面完成拼多多商家后台登录。\n"
            "登录成功后本对话框会自动感知，不需要手动确认（最长等待 15 分钟）。"
        )

    def _on_succeeded(self) -> None:
        self.result_shop = self._pending_shop_name
        # 登录态已经重新验证成功：如果今天曾因登录失效被标成 failed，必须
        # 同步清掉该标记。否则店铺管理仍显示“登录失败”，运行控制也会因
        # login_expired=True 把这家店过滤掉，造成明明已登录却无法勾选。
        shop_repo.mark_shop_login_succeeded(self.result_shop)
        self._status_label.setText("登录成功 ✓")
        self._progress.setVisible(False)
        msgbox.information(self, "登录成功", f"「{self.result_shop}」登录成功。")
        self.accept()

    def _on_failed(self, message: str) -> None:
        self._progress.setVisible(False)
        self._status_label.setText(f"登录未成功：{message}")
        self._name_edit.setEnabled(not self._is_relogin)
        self._start_button.setEnabled(True)
        self._start_button.setText("重试")

    def _on_cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
        self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt 事件处理器命名约定)
        if self._worker is not None:
            self._worker.cancel()
        super().closeEvent(event)
