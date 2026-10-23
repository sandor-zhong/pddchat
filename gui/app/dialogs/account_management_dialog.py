"""Editable account list with multi-row paste and explicit Excel column mapping."""

from __future__ import annotations

from openpyxl.utils import get_column_letter
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QShortcut,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..services import account_store, msgbox, shop_repo
from ..services.account_import import (
    AccountRecord,
    assign_account_shop_names,
    inspect_excel_layout,
    parse_pasted_accounts,
    read_excel_rows,
)
from .password_widgets import PasswordDelegate, PasswordItem, eye_icon


class ExcelColumnsDialog(QDialog):
    def __init__(self, path: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择 Excel 列")
        self._path = path
        self._layouts = inspect_excel_layout(path)
        self.records: list[AccountRecord] = []
        layout = QFormLayout(self)
        self._sheet = QComboBox()
        self._sheet.addItems(self._layouts)
        layout.addRow("工作表", self._sheet)
        self._start_row = QSpinBox()
        self._start_row.setRange(1, 1_048_576)
        layout.addRow("首行数据行号", self._start_row)
        self._columns = {}
        for field, label in (
            ("username", "账号列"),
            ("password", "密码列"),
            ("shop", "店铺名列"),
            ("group", "分组列"),
        ):
            combo = QComboBox()
            self._columns[field] = combo
            layout.addRow(label, combo)
        hint = QLabel(
            "店铺名可不选择：程序会从“店铺名:子账号”中提取店铺名；分组也可不设置。\n"
            "账号和密码均为空的行自动跳过，原 Excel 文件不会被修改。"
        )
        hint.setWordWrap(True)
        layout.addRow(hint)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("读取到列表")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addRow(buttons)
        self._sheet.currentTextChanged.connect(self._update_sheet)
        self._update_sheet()

    def _update_sheet(self) -> None:
        info = self._layouts[self._sheet.currentText()]
        self._start_row.setValue(info["header_row"] + 1)
        for field, combo in self._columns.items():
            combo.clear()
            if field in {"shop", "group"}:
                combo.addItem("不填写（自动生成店铺名）" if field == "shop" else "不设置", None)
            for index in range(info["max_column"]):
                combo.addItem(f"{get_column_letter(index + 1)} 列", index)
            default = info["mapping"].get(field)
            if default is None and field in {"username", "password"}:
                default = 0 if field == "username" else min(1, info["max_column"] - 1)
            found = combo.findData(default)
            combo.setCurrentIndex(max(0, found))

    def _accept(self) -> None:
        columns = {field: combo.currentData() for field, combo in self._columns.items()}
        chosen = [index for index in columns.values() if index is not None]
        if len(chosen) != len(set(chosen)):
            msgbox.warning(self, "列选择重复", "账号、密码、店铺名和分组不能选择同一列。")
            return
        try:
            self.records = read_excel_rows(
                self._path, self._sheet.currentText(), self._start_row.value(), columns
            )
        except Exception as exc:  # noqa: BLE001 - normalize workbook errors for operators
            msgbox.warning(self, "读取失败", str(exc) if isinstance(exc, ValueError) else "无法读取 Excel。")
            return
        self.accept()


class AccountManagementDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("账号列表录入 / 批量粘贴")
        self.resize(920, 620)
        self.login_records: list[AccountRecord] = []
        self.saved = False
        self._load_error = False
        layout = QVBoxLayout(self)
        hint = QLabel(
            "表格按分组、店铺名、账号、密码显示；账号和密码必填，店铺名和分组可选。\n"
            "粘贴仍使用账号、密码、店铺名、分组的顺序；支持 Excel 两至四列（Ctrl+V）。"
            "按登录账号去重，同批重复账号以最后一行为准。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        toolbar = QHBoxLayout()
        for label, callback in (
            ("新增一行", self._add_row),
            ("粘贴多行（追加）", self._paste),
            ("读取 Excel…", self._read_excel),
            ("移除选中行", self._remove_selected),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            toolbar.addWidget(button)
        toolbar.addStretch()
        self._password_toggle = QPushButton()
        self._password_toggle.clicked.connect(self._toggle_passwords)
        toolbar.addWidget(self._password_toggle)
        layout.addLayout(toolbar)

        self._table = QTableWidget(0, 4)
        self._table.passwords_visible = False
        self._table.setHorizontalHeaderLabels(["分组（可选）", "店铺名（可选）", "账号", "密码"])
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self._table.setAlternatingRowColors(True)
        self._table.setItemDelegateForColumn(3, PasswordDelegate(self._table))
        layout.addWidget(self._table, 1)
        self._update_password_button()
        self._paste_shortcut = QShortcut(QKeySequence.Paste, self._table)
        self._paste_shortcut.setContext(Qt.WidgetWithChildrenShortcut)
        self._paste_shortcut.activated.connect(self._paste)
        self._count = QLabel()
        self._table.itemChanged.connect(self._update_count)
        layout.addWidget(self._count)

        buttons = QHBoxLayout()
        buttons.addStretch()
        for label, callback in (
            ("保存列表", lambda: self._save(False)),
            ("保存并批量登录", lambda: self._save(True)),
            ("取消", self.reject),
        ):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)

        try:
            existing = account_store.list_accounts()
        except Exception as exc:  # noqa: BLE001 - show a usable DPAPI/database error
            existing = []
            self._load_error = True
            msgbox.warning(self, "账号列表读取失败", str(exc))
        for record in existing:
            self._add_row(record)
        if not existing:
            self._add_row()

    def _add_row(self, record=None) -> None:
        if not isinstance(record, AccountRecord):
            record = AccountRecord("", "")
        row = self._table.rowCount()
        self._table.insertRow(row)
        values = [record.group_name, record.shop, record.username, record.password]
        for column, value in enumerate(values):
            item = PasswordItem() if column == 3 else QTableWidgetItem()
            item.setData(Qt.EditRole, value)
            self._table.setItem(row, column, item)
        self._update_count()

    def _records(self) -> list[AccountRecord]:
        records = []
        for row in range(self._table.rowCount()):
            values = [self._table.item(row, column).data(Qt.EditRole) if self._table.item(row, column) else ""
                      for column in range(4)]
            if any(str(value or "") for value in values):
                records.append(AccountRecord(str(values[2]), str(values[3]), str(values[1]), str(values[0])))
        return records

    def _append_records(self, records: list[AccountRecord]) -> None:
        for row in reversed(range(self._table.rowCount())):
            if not any(self._table.item(row, column).data(Qt.EditRole) for column in range(4)):
                self._table.removeRow(row)
        for record in records:
            self._add_row(record)
        self._update_count()

    def _paste(self) -> None:
        text = QApplication.clipboard().text()
        editor = QApplication.focusWidget()
        if isinstance(editor, QLineEdit) and "\t" not in text and "\n" not in text:
            editor.paste()
            return
        if "\t" not in text and "\n" not in text and "," not in text and self._table.currentRow() >= 0:
            item = self._table.item(self._table.currentRow(), self._table.currentColumn())
            if item is not None:
                item.setData(Qt.EditRole, text)
            return
        try:
            self._append_records(parse_pasted_accounts(text))
        except ValueError as exc:
            msgbox.warning(self, "粘贴失败", str(exc))

    def _read_excel(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "读取账号列表", "", "Excel 工作簿 (*.xlsx)")
        if not path:
            return
        try:
            dialog = ExcelColumnsDialog(path, self)
        except Exception:  # noqa: BLE001 - normalize workbook errors for operators
            msgbox.warning(self, "读取失败", "无法打开 Excel，请选择未加密的 .xlsx 文件。")
            return
        if dialog.exec_() == QDialog.Accepted:
            self._append_records(dialog.records)

    def _remove_selected(self) -> None:
        for row in sorted({index.row() for index in self._table.selectedIndexes()}, reverse=True):
            self._table.removeRow(row)
        if not self._table.rowCount():
            self._add_row()
        self._update_count()

    def _toggle_passwords(self) -> None:
        self._table.setFocus()
        self._table.passwords_visible = not self._table.passwords_visible
        self._update_password_button()
        self._table.viewport().update()

    def _update_password_button(self) -> None:
        visible = self._table.passwords_visible
        self._password_toggle.setIcon(eye_icon(visible))
        self._password_toggle.setText("隐藏密码" if visible else "显示密码")

    def _update_count(self) -> None:
        self._count.setText(f"待保存 {len(self._records())} 行 · 双击可修改 · 店铺名和分组可留空")

    def _save(self, login: bool) -> None:
        self._table.setFocus()
        if self._load_error:
            msgbox.warning(self, "无法保存", "原账号列表读取失败。请关闭窗口并修复问题后再保存，避免覆盖原数据。")
            return
        try:
            records = assign_account_shop_names(self._records())
            saved_records = account_store.replace_accounts(records)
            shop_repo.sync_account_shops([record.shop for record in saved_records])
            for record in saved_records:
                shop_repo.set_shop_group(record.shop, record.group_name or None)
        except Exception as exc:  # noqa: BLE001 - show validation, DPAPI or SQLite failures
            msgbox.warning(self, "保存未完成", str(exc))
            return
        self.saved = True
        if login:
            self.login_records = saved_records
        else:
            for record in saved_records:
                record.clear_password()
        self._clear_table_passwords()
        self.accept()

    def _clear_table_passwords(self) -> None:
        self._table.blockSignals(True)
        try:
            for row in range(self._table.rowCount()):
                item = self._table.item(row, 3)
                if item is not None:
                    item.setData(Qt.EditRole, "")
        finally:
            self._table.blockSignals(False)

    def reject(self) -> None:
        self._clear_table_passwords()
        super().reject()
