"""B. 店铺管理：店铺列表 + 新增店铺/重新登录（弹浏览器引导登录）+
停用/启用/删除。

用 QTableView + 自定义 QAbstractTableModel（而不是 QTableWidget）——
表格数据每 4 秒被 DbPoller 整体刷新一次，用 Model/View 架构统一走
beginResetModel/endResetModel，比手动维护 QTableWidgetItem 更省心。
"""

from __future__ import annotations

from PyQt5.QtCore import QAbstractTableModel, QModelIndex, Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from ..dialogs.account_management_dialog import AccountManagementDialog
from ..dialogs.add_shop_dialog import AddShopDialog
from ..dialogs.batch_account_login_dialog import BatchAccountLoginDialog
from ..dialogs.shop_group_dialog import BatchShopGroupDialog, ShopGroupDialog
from ..dialogs.shop_browser_dialog import ShopBrowserDialog
from ..dialogs.shop_max_orders_dialog import (
    BatchShopMaxOrdersDialog,
    ShopMaxOrdersDialog,
)
from ..services import account_store, msgbox, shop_repo, time_format
from ..services.app_logging import logger
from ..services.project_paths import PROJECT_PATHS
from ..workers.db_poll_worker import DbPoller
from ..workers.batch_login_check import BatchLoginCheck

_COLUMNS = [
    "选择",
    "店铺名",
    "所属组",
    "登录状态",
    "今日调度状态",
    "待处理",
    "最近活动",
    "启用状态",
    "处理上限",
]


class ShopTableModel(QAbstractTableModel):
    checked_changed = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._rows: list[shop_repo.ShopSummary] = []
        self._checked_shops: set[str] = set()
        self.login_checks = {}
        self._sort_column = None
        self._sort_order = Qt.AscendingOrder

    def set_rows(self, rows: list[shop_repo.ShopSummary]) -> None:
        self.beginResetModel()
        self._rows = list(rows)
        if self._sort_column is not None:
            self._rows.sort(
                key=lambda row: (
                    (row.group_name or "未分组").casefold() if self._sort_column == 2
                    else {"x": 0, "√": 1, "检测异常": 2}[self._login_label(row)],
                    row.shop.casefold(),
                ),
                reverse=self._sort_order == Qt.DescendingOrder,
            )
        self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        if column in (2, 3):
            self._sort_column = column
            self._sort_order = order
            self.set_rows(self._rows)
            self.headerDataChanged.emit(Qt.Horizontal, 2, 3)

    def _login_label(self, row):
        if row.shop in self.login_checks:
            value = self.login_checks[row.shop][0]
            return "√" if value is True else "x" if value is False else "检测异常"
        return "√" if (row.has_profile and not row.login_expired) else "x"

    def row_at(self, index: QModelIndex) -> shop_repo.ShopSummary | None:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        return self._rows[index.row()]

    def checked_shops(self) -> list[str]:
        """按当前完整店铺列表顺序返回勾选项，而不是按点击顺序。"""
        return [row.shop for row in self._rows if row.shop in self._checked_shops]

    def all_checked_shops(self) -> set[str]:
        return set(self._checked_shops)

    def check_visible(self) -> None:
        changed_rows = [
            index for index, row in enumerate(self._rows)
            if row.shop not in self._checked_shops
        ]
        if not changed_rows:
            return
        self._checked_shops.update(self._rows[index].shop for index in changed_rows)
        self.dataChanged.emit(
            self.index(changed_rows[0], 0),
            self.index(changed_rows[-1], 0),
            [Qt.CheckStateRole],
        )
        self.checked_changed.emit()

    def clear_checked(self) -> None:
        if not self._checked_shops:
            return
        self._checked_shops.clear()
        if self._rows:
            self.dataChanged.emit(
                self.index(0, 0),
                self.index(len(self._rows) - 1, 0),
                [Qt.CheckStateRole],
            )
        self.checked_changed.emit()

    def retain_shops(self, shops: set[str]) -> None:
        retained = self._checked_shops & shops
        if retained == self._checked_shops:
            return
        self._checked_shops = retained
        self.checked_changed.emit()

    # ---- QAbstractTableModel 接口 ----

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        label = _COLUMNS[section]
        if section == self._sort_column:
            label += "  ▲" if self._sort_order == Qt.AscendingOrder else "  ▼"
        return label

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if col == 3 and role == Qt.ToolTipRole:
            return self.login_checks.get(row.shop, (None, "尚未检测"))[1]
        if col == 0 and role == Qt.CheckStateRole:
            return Qt.Checked if row.shop in self._checked_shops else Qt.Unchecked
        if role != Qt.DisplayRole:
            return None
        if col == 0:
            return ""
        if col == 1:
            return row.shop
        if col == 2:
            return row.group_name or "未分组"
        if col == 3:
            # has_profile=False（从没登录过/已删登录态目录）和
            # login_expired=True（今天跑过、确认登录已失效）统一显示
            # "未登录"，处理方式都一样：去重新登录一次。
            return self._login_label(row)
        if col == 4:
            if row.schedule_status == "done":
                return "今日已完成"
            if row.schedule_status == "failed":
                return f"失败：{row.schedule_detail or '原因未知'}"
            if row.schedule_status == "skipped":
                return "排队中，等待优先补位"
            return "尚未开始"
        if col == 5:
            return str(row.pending_remaining)
        if col == 6:
            return time_format.format_local(row.last_activity) or "—"
        if col == 7:
            return "已停用" if row.disabled else "启用中"
        if col == 8:
            return str(row.max_orders) if row.max_orders is not None else "默认"
        return None

    def flags(self, index: QModelIndex):
        flags = super().flags(index)
        if index.isValid() and index.column() == 0:
            flags |= Qt.ItemIsUserCheckable
        return flags

    def setData(self, index: QModelIndex, value, role: int = Qt.EditRole):
        if (
            not index.isValid()
            or index.column() != 0
            or role != Qt.CheckStateRole
        ):
            return False
        shop = self._rows[index.row()].shop
        checked = value == Qt.Checked
        if checked:
            self._checked_shops.add(shop)
        else:
            self._checked_shops.discard(shop)
        self.dataChanged.emit(index, index, [Qt.CheckStateRole])
        self.checked_changed.emit()
        return True


class ShopsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)

        header = QLabel("店铺管理")
        header_font = header.font()
        header_font.setPointSize(14)
        header_font.setBold(True)
        header.setFont(header_font)
        root.addWidget(header)

        toolbar = QHBoxLayout()
        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("搜索店铺名或所属组……")
        self._add_shop_button = QPushButton("新增店铺")
        self._add_shop_button.clicked.connect(self._on_add_shop_clicked)
        self._open_shop_button = QPushButton("打开店铺")
        self._open_shop_button.setToolTip("选择一家店铺，复用登录状态打开浏览器，可按 F12 检查页面元素")
        self._open_shop_button.clicked.connect(self._on_open_shop_clicked)
        self._excel_import_button = QPushButton("账号列表录入 / 批量登录")
        self._excel_import_button.setMinimumHeight(32)
        self._excel_import_button.setStyleSheet("padding: 4px 14px;")
        self._excel_import_button.clicked.connect(self._on_excel_import_clicked)
        self._batch_group_button = QPushButton("批量设置店铺分组")
        self._batch_group_button.setMinimumHeight(32)
        self._batch_group_button.setStyleSheet("padding: 4px 14px;")
        self._batch_group_button.setEnabled(False)
        self._batch_group_button.setToolTip(
            "选择多家店铺，统一设置所属组或批量设为未分组"
        )
        self._batch_group_button.clicked.connect(self._open_batch_group_dialog)
        self._batch_max_orders_button = QPushButton("批量设置处理上限")
        self._batch_max_orders_button.setMinimumHeight(32)
        self._batch_max_orders_button.setStyleSheet("padding: 4px 14px;")
        self._batch_max_orders_button.setEnabled(False)
        self._batch_max_orders_button.setToolTip(
            "选择多家店铺，一次设置相同的申诉处理上限或批量恢复默认"
        )
        self._batch_max_orders_button.clicked.connect(
            self._open_batch_max_orders_dialog
        )
        toolbar.addWidget(self._search_box, stretch=1)
        toolbar.addWidget(self._batch_group_button)
        toolbar.addWidget(self._batch_max_orders_button)
        toolbar.addWidget(self._excel_import_button)
        toolbar.addWidget(self._open_shop_button)
        toolbar.addWidget(self._add_shop_button)
        root.addLayout(toolbar)

        batch_toolbar = QHBoxLayout()
        batch_toolbar.setContentsMargins(0, 2, 0, 2)
        batch_toolbar.setSpacing(8)
        self._checked_label = QLabel("已选择 0 家")
        self._select_all_button = QPushButton("全选")
        self._select_all_button.setMinimumHeight(32)
        self._select_all_button.setMinimumWidth(72)
        self._select_all_button.clicked.connect(self._select_all_visible)
        self._clear_selection_button = QPushButton("全不选")
        self._clear_selection_button.setMinimumHeight(32)
        self._clear_selection_button.setMinimumWidth(72)
        self._clear_selection_button.clicked.connect(self._clear_checked)
        self._batch_login_button = QPushButton("批量登录选中店铺")
        self._batch_login_button.setMinimumHeight(32)
        self._batch_login_button.setMinimumWidth(150)
        self._batch_login_button.setStyleSheet("padding: 4px 14px;")
        self._batch_login_button.setEnabled(False)
        self._batch_login_button.clicked.connect(self._on_batch_login_selected)
        self._stop_batch_login_button = QPushButton("停止批量登录")
        self._stop_batch_login_button.setMinimumHeight(32)
        self._stop_batch_login_button.setMinimumWidth(112)
        self._stop_batch_login_button.setStyleSheet("padding: 4px 14px;")
        self._stop_batch_login_button.setEnabled(False)
        self._stop_batch_login_button.clicked.connect(self._stop_batch_login)
        batch_toolbar.addWidget(self._checked_label)
        batch_toolbar.addWidget(self._select_all_button)
        batch_toolbar.addWidget(self._clear_selection_button)
        batch_toolbar.addStretch()
        self._check_login_button = QPushButton("批量检测选中店铺登录状态")
        self._check_login_button.setEnabled(False)
        self._check_login_button.setMinimumHeight(32)
        self._check_login_button.clicked.connect(self._start_login_check)
        batch_toolbar.addWidget(self._check_login_button)
        self._stop_check_button = QPushButton("停止检测")
        self._stop_check_button.clicked.connect(self.stop_login_check)
        self._stop_check_button.setEnabled(False)
        batch_toolbar.addWidget(self._stop_check_button)
        batch_toolbar.addWidget(self._batch_login_button)
        batch_toolbar.addWidget(self._stop_batch_login_button)
        root.addLayout(batch_toolbar)
        self._check_progress = QLabel("")
        root.addWidget(self._check_progress)
        self._batch_progress_layout = QVBoxLayout()
        root.addLayout(self._batch_progress_layout)

        self._model = ShopTableModel()
        self._model.checked_changed.connect(self._update_bulk_controls)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.Fixed)
        header_view.resizeSection(0, 52)
        header_view.setSectionResizeMode(1, QHeaderView.Stretch)
        header_view.setSectionsClickable(True)
        header_view.sectionClicked.connect(self._sort_shops)
        self._table.verticalHeader().setVisible(False)
        self._table.setContextMenuPolicy(Qt.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)
        root.addWidget(self._table, stretch=1)

        hint = QLabel("提示：选中一家店铺后点“打开店铺”可查看订单或按 F12 检查元素；右键也可打开店铺及单独设置。")
        hint.setStyleSheet("color: #9e9e9e;")
        root.addWidget(hint)

        self._search_box.textChanged.connect(self._apply_filter)

        self._all_rows: list[shop_repo.ShopSummary] = []
        # 对话框引用必须在轮询线程启动前就存在；极快的首次查询也可能立即
        # 触发 _on_data -> _update_bulk_controls。
        self._active_dialog: AddShopDialog | None = None
        self._active_batch_dialog: BatchAccountLoginDialog | None = None
        self._login_check = None
        self._poller = DbPoller(fetch=shop_repo.list_shop_summaries, interval_ms=4000)
        self._poller.data_ready.connect(self._on_data)
        self._poller.fetch_error.connect(self._on_fetch_error)
        self._poller.start()

    # ---- 数据刷新 ----

    def _sort_shops(self, column):
        if column not in (2, 3):
            return
        order = (Qt.DescendingOrder if self._model._sort_column == column
                 and self._model._sort_order == Qt.AscendingOrder
                 else Qt.AscendingOrder)
        self._model.sort(column, order)

    def _on_data(self, rows: list[shop_repo.ShopSummary]) -> None:
        self._all_rows = rows
        self._model.retain_shops({row.shop for row in rows})
        self._apply_filter(self._search_box.text())

    def _on_fetch_error(self, message: str) -> None:
        logger.warning(f"[ShopsPage] 刷新店铺列表失败：{message}")

    def _apply_filter(self, keyword: str) -> None:
        keyword = keyword.strip()
        if not keyword:
            self._model.set_rows(self._all_rows)
            self._update_bulk_controls()
            return
        self._model.set_rows(
            [
                row
                for row in self._all_rows
                if keyword in row.shop or keyword in (row.group_name or "")
            ]
        )
        self._update_bulk_controls()

    def _select_all_visible(self) -> None:
        self._model.check_visible()

    def _clear_checked(self) -> None:
        self._model.clear_checked()

    def _selected_shops(self) -> list[str]:
        selected = self._model.all_checked_shops()
        return [row.shop for row in self._all_rows if row.shop in selected]

    def _batch_is_running(self) -> bool:
        return bool(
            self._active_batch_dialog is not None
            and not self._active_batch_dialog.completed
        )

    def _update_bulk_controls(self) -> None:
        count = len(self._selected_shops())
        checking = self._login_check is not None
        running = self._batch_is_running() or checking
        self._check_login_button.setEnabled(count > 0 and not running)
        self._stop_check_button.setEnabled(checking)
        self._checked_label.setText(f"已选择 {count} 家")
        self._batch_group_button.setEnabled(count > 0 and not running)
        self._batch_max_orders_button.setEnabled(count > 0 and not running)
        self._batch_login_button.setEnabled(count > 0 and not running)
        self._stop_batch_login_button.setEnabled(self._batch_is_running())
        self._excel_import_button.setEnabled(not running)
        self._add_shop_button.setEnabled(not running)

    def _start_login_check(self):
        if self._login_check is not None or self._batch_is_running():
            return
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        shops = self._selected_shops()
        if not shops:
            return
        self._login_check = BatchLoginCheck(shops, self)
        self._login_check.result.connect(self._login_check_result)
        self._login_check.progress.connect(lambda done, total: self._check_progress.setText(f"已检测 {done}/{total} 家（最多 3 家并发）"))
        self._login_check.finished.connect(self._login_check_finished)
        self._check_progress.setText(f"开始检测 {len(shops)} 家店铺（无头浏览器，3 家并发）")
        self._update_bulk_controls()
        self._login_check.start()

    def _login_check_result(self, shop, success, message):
        self._model.login_checks[shop] = (success, message)
        logger.info("[登录检测][%s] %s", shop, message)
        if success is not None:
            shop_repo.mark_imported_shop_login(shop, success, message)
            if success:
                shop_repo.mark_shop_login_succeeded(shop)
        self._refresh_now()

    def _login_check_finished(self):
        self._login_check.deleteLater()
        self._login_check = None
        self._check_progress.setText(self._check_progress.text() + "；检测结束")
        self._update_bulk_controls()

    def stop_login_check(self):
        if self._login_check is not None:
            self._login_check.stop()

    def _refresh_now(self) -> None:
        """操作完成后（新增/删除/停用）不想等最多 4 秒的下一次轮询，
        主动查一次立刻刷新界面。"""
        self._on_data(shop_repo.list_shop_summaries())

    # ---- 新增店铺 / 重新登录 ----

    def _on_add_shop_clicked(self) -> None:
        self._open_login_dialog(shop=None)

    def _on_open_shop_clicked(self) -> None:
        row = self._model.row_at(self._table.currentIndex())
        shops = self._selected_shops()
        # 勾选恰好一家时优先采用勾选项，避免打开上次高亮的其它店铺。
        shop = shops[0] if len(shops) == 1 else row.shop if row else None
        if shop is None:
            msgbox.information(self, "请选择店铺", "请点击或勾选一家店铺，再点击“打开店铺”。")
            return
        self._open_shop_browser(shop)

    def _open_shop_browser(self, shop: str) -> None:
        import scheduler_core

        if self._login_check is not None or self._batch_is_running():
            msgbox.information(self, "店铺浏览器正在使用", "请先结束登录检测或批量登录，再打开店铺。")
            return
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        if scheduler_core.active_operation_lock() is not None:
            msgbox.information(self, "店铺浏览器正在使用", "请先结束正在运行的任务，再打开店铺查看。")
            return
        dialog = ShopBrowserDialog(shop=shop, parent=self)
        dialog.exec_()
        dialog.deleteLater()

    def _on_excel_import_clicked(self) -> None:
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        account_dialog = AccountManagementDialog(self)
        account_dialog.exec_()
        if not account_dialog.saved:
            return
        self._refresh_now()
        if not account_dialog.login_records:
            return
        self._start_batch_login(account_dialog.login_records)

    def _on_batch_login_selected(self) -> None:
        problems = PROJECT_PATHS.validate()
        if problems:
            msgbox.critical(self, "环境配置有误", "\n".join(problems))
            return
        shops = self._selected_shops()
        if not shops:
            msgbox.information(self, "未选择店铺", "请先勾选需要登录的店铺。")
            return
        try:
            saved_records = account_store.list_accounts()
        except Exception as exc:  # noqa: BLE001 - UI boundary must report store/DPAPI errors
            logger.exception("[ShopsPage] 读取店铺账号列表失败")
            msgbox.critical(self, "读取账号失败", f"无法读取已保存的账号密码：{exc}")
            return

        by_shop = {record.shop: record for record in saved_records}
        missing = [shop for shop in shops if shop not in by_shop]
        if missing:
            for record in saved_records:
                record.password = ""
            preview = "\n".join(f"• {shop}" for shop in missing[:10])
            suffix = f"\n等 {len(missing)} 家店铺" if len(missing) > 10 else ""
            msgbox.warning(
                self,
                "缺少账号密码",
                "以下店铺没有已录入的账号密码，请先点击“账号列表录入 / 批量登录”补充：\n\n"
                f"{preview}{suffix}",
            )
            return

        selected_records = [by_shop[shop] for shop in shops]
        for record in saved_records:
            if record.shop not in shops:
                record.password = ""
        self._start_batch_login(selected_records)

    def _start_batch_login(self, records) -> None:
        if self._batch_is_running():
            for record in records:
                record.password = ""
            msgbox.information(self, "正在批量登录", "已有一批店铺正在登录，请先等待完成或停止。")
            return
        if self._active_batch_dialog is not None:
            self._batch_progress_layout.removeWidget(self._active_batch_dialog)
            self._active_batch_dialog.close()
            self._active_batch_dialog = None

        dialog = BatchAccountLoginDialog(records, self, embedded=True)
        dialog.setAttribute(Qt.WA_DeleteOnClose, True)
        self._active_batch_dialog = dialog
        dialog.batch_finished.connect(self._on_batch_login_finished)
        dialog.finished.connect(lambda _result, current=dialog: self._on_batch_dialog_closed(current))
        dialog.setModal(False)
        self._batch_progress_layout.addWidget(dialog)
        dialog.show()
        self._update_bulk_controls()

    def _stop_batch_login(self) -> None:
        if not self._batch_is_running():
            self._update_bulk_controls()
            return
        self._stop_batch_login_button.setEnabled(False)
        self._active_batch_dialog.stop_batch_login()

    def _on_batch_login_finished(self, _succeeded: int, _failed: int, _cancelled: int) -> None:
        self._model.login_checks.clear()
        self._refresh_now()
        self._update_bulk_controls()

    def _on_batch_dialog_closed(self, dialog: BatchAccountLoginDialog) -> None:
        if self._active_batch_dialog is dialog:
            self._active_batch_dialog = None
        self._refresh_now()
        self._update_bulk_controls()

    def _open_login_dialog(self, shop: str | None) -> None:
        if self._login_check is not None:
            msgbox.information(self, "正在检测", "请先停止登录检测再进行登录。")
            return
        dialog = AddShopDialog(shop=shop, parent=self)
        self._active_dialog = dialog
        dialog.exec_()
        self._active_dialog = None
        if dialog.result_shop:
            self._model.login_checks.pop(dialog.result_shop, None)
            self._refresh_now()

    # ---- 右键菜单：重新登录 / 停用 / 启用 / 删除 ----

    def _on_context_menu(self, pos) -> None:
        if self._login_check is not None:
            return
        index = self._table.indexAt(pos)
        row = self._model.row_at(index)
        if row is None:
            return

        menu = QMenu(self)
        open_action = menu.addAction("打开店铺（查看 / 调试）")
        relogin_action = menu.addAction("重新登录")
        toggle_action = menu.addAction("启用" if row.disabled else "停用")
        group_action = menu.addAction("设置所属组")
        max_orders_action = menu.addAction("设置处理上限")
        reset_schedule_action = menu.addAction("重置今日状态（允许重新处理）")
        delete_action = menu.addAction("删除")

        chosen = menu.exec_(self._table.viewport().mapToGlobal(pos))
        if chosen is None:
            return

        if chosen is open_action:
            self._open_shop_browser(row.shop)
        elif chosen is relogin_action:
            self._open_login_dialog(shop=row.shop)
        elif chosen is toggle_action:
            shop_repo.set_shop_disabled(row.shop, not row.disabled)
            self._refresh_now()
        elif chosen is group_action:
            self._open_group_dialog(row.shop)
        elif chosen is max_orders_action:
            self._open_max_orders_dialog(row.shop)
        elif chosen is reset_schedule_action:
            self._reset_schedule(row.shop)
        elif chosen is delete_action:
            self._confirm_and_delete(row.shop)

    def _open_max_orders_dialog(self, shop: str) -> None:
        dialog = ShopMaxOrdersDialog(shop=shop, parent=self)
        dialog.exec_()
        if dialog.saved:
            self._refresh_now()

    def _open_group_dialog(self, shop: str) -> None:
        dialog = ShopGroupDialog(shop=shop, parent=self)
        dialog.exec_()
        if dialog.saved:
            self._refresh_now()

    def _open_batch_group_dialog(self) -> None:
        shops = self._selected_shops()
        if not shops:
            msgbox.information(self, "暂无店铺", "当前没有可设置的店铺。")
            return
        dialog = BatchShopGroupDialog(
            shops=shops,
            group_by_shop=shop_repo.list_shop_groups(),
            parent=self,
        )
        dialog.exec_()
        if dialog.saved:
            self._refresh_now()

    def _open_batch_max_orders_dialog(self) -> None:
        shops = self._selected_shops()
        if not shops:
            msgbox.information(self, "暂无店铺", "当前没有可设置的店铺。")
            return
        dialog = BatchShopMaxOrdersDialog(shops=shops, parent=self)
        dialog.exec_()
        if dialog.saved:
            self._refresh_now()

    def _reset_schedule(self, shop: str) -> None:
        # 非破坏性操作（只清一个调度标记位，不碰订单历史数据），不需要
        # 二次确认——跟"停用/启用"这类幂等切换是同一档风险等级。
        shop_repo.reset_today_schedule(shop)
        self._refresh_now()
        msgbox.information(
            self,
            "已重置",
            f"「{shop}」今日调度状态已重置，下一轮会重新处理这家店铺。",
        )

    def _confirm_and_delete(self, shop: str) -> None:
        confirmed = msgbox.confirm_yes_no(
            self,
            "确认删除店铺",
            f"确定要删除「{shop}」的登录态吗？\n\n"
            "只会删除本地保存的登录信息，之后需要重新登录才能恢复；"
            "这家店铺在数据库里的历史订单记录不受影响、仍会保留。",
        )
        if not confirmed:
            return
        shop_repo.delete_shop_profile(shop)
        self._refresh_now()
