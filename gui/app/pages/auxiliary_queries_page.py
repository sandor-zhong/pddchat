"""辅助查询功能：按提交日期统计数量、通过率并导出同范围订单结果。"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from PyQt5.QtCore import (
    QAbstractTableModel,
    QObject,
    QRunnable,
    Qt,
    QThreadPool,
    pyqtSignal,
)
from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..services import auxiliary_export as export
from ..services import auxiliary_queries as queries
from ..services import msgbox


class _Signals(QObject):
    completed = pyqtSignal(object)
    failed = pyqtSignal(str)


class _Work(QRunnable):
    def __init__(self, function):
        super().__init__()
        self.function = function
        self.signals = _Signals()

    def run(self):
        try:
            result = self.function()
        except Exception as exc:  # noqa: BLE001 - 工作线程统一把查询/导出异常返回界面
            self.signals.failed.emit(str(exc))
        else:
            self.signals.completed.emit(result)


class QueryTableModel(QAbstractTableModel):
    def __init__(self, columns, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.rows = []

    def rowCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.rows)

    def columnCount(self, parent=None):
        return 0 if parent is not None and parent.isValid() else len(self.columns)

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid():
            return None
        row, key = self.rows[index.row()], self.columns[index.column()][0]
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            return str(export.display_value(row, key))
        if role == Qt.TextAlignmentRole and isinstance(row.get(key), (int, float)):
            return int(Qt.AlignRight | Qt.AlignVCenter)
        return None

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role not in (Qt.DisplayRole, Qt.ToolTipRole):
            return None
        return (
            self.columns[section][1]
            if orientation == Qt.Horizontal
            else str(section + 1)
        )

    def set_rows(self, rows):
        self.beginResetModel()
        self.rows = list(rows)
        self.endResetModel()

    def sort(self, column, order=Qt.AscendingOrder):
        if not 0 <= column < len(self.columns):
            return
        key = self.columns[column][0]
        present = [row for row in self.rows if row.get(key) is not None]
        missing = [row for row in self.rows if row.get(key) is None]
        present.sort(
            key=lambda row: (
                row[key]
                if isinstance(row[key], (int, float))
                else str(row[key]).casefold()
            ),
            reverse=order == Qt.DescendingOrder,
        )
        self.set_rows(present + missing)


class _Table(QTableView):
    def keyPressEvent(self, event):
        if event.matches(QKeySequence.Copy):
            indices = sorted(
                self.selectedIndexes(), key=lambda index: (index.row(), index.column())
            )
            rows = {}
            for index in indices:
                rows.setdefault(index.row(), []).append(str(index.data() or ""))
            QApplication.clipboard().setText(
                "\n".join("\t".join(values) for values in rows.values())
            )
            event.accept()
        else:
            super().keyPressEvent(event)


class AuxiliaryQueriesPage(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._report = None
        self._last_criteria = None
        self._query_busy = False
        self._export_busy = False
        self._tasks = {}
        self._next_job = 0
        self._active = False
        layout = QVBoxLayout(self)
        title = QLabel("辅助查询功能")
        title.setStyleSheet("font-size: 20px; font-weight: 600; color: #182230;")
        layout.addWidget(title)
        hint = QLabel(
            "按订单已有的提交时间查询各店铺负向申诉、复议和售后申诉数量、通过率及明细。支持今天、昨天或任意历史日期；查询某一天时，将起止日期设为同一天。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("店铺"))
        self._shop_combo = QComboBox()
        self._shop_combo.setEditable(True)
        self._shop_combo.setInsertPolicy(QComboBox.NoInsert)
        self._shop_combo.addItem("全部店铺", None)
        self._shop_combo.setMinimumWidth(190)
        self._shop_combo.completer().setCaseSensitivity(Qt.CaseInsensitive)
        self._shop_combo.completer().setFilterMode(Qt.MatchContains)
        filters.addWidget(self._shop_combo, 1)
        filters.addWidget(QLabel("申诉类型"))
        self._type_combo = QComboBox()
        self._type_combo.addItem("全部类型", None)
        for kind in queries.BUSINESS_TYPES:
            self._type_combo.addItem(kind, kind)
        filters.addWidget(self._type_combo)
        self._query_button = QPushButton("查询 / 刷新")
        self._query_button.setMinimumHeight(32)
        self._query_button.clicked.connect(self.query)
        filters.addWidget(self._query_button)
        layout.addLayout(filters)
        dates = QHBoxLayout()
        dates.addWidget(QLabel("提交日期范围（数量 / 通过率 / 明细）"))
        today = queries.beijing_now().date()
        self._start_date = QDateEdit(today)
        self._end_date = QDateEdit(today)
        for widget in (self._start_date, self._end_date):
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
        dates.addWidget(self._start_date)
        dates.addWidget(QLabel("至"))
        dates.addWidget(self._end_date)
        for label, mode in (("今天", "today"), ("昨天", "yesterday"), ("近7天", "week"), ("本月", "month")):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, choice=mode: self._quick_range(choice)
            )
            dates.addWidget(button)
        dates.addStretch(1)
        layout.addLayout(dates)
        self._partial_check = QCheckBox("售后“部分通过”计入通过数（与原报表一致）")
        self._partial_check.setChecked(True)
        layout.addWidget(self._partial_check)
        policy = QLabel(
            queries.POLICY
            + " 区间日期包含结束当天；提交数量不受当前审核结果是否通过影响。"
        )
        policy.setWordWrap(True)
        policy.setStyleSheet(
            "color: #475467; background: #f2f4f7; padding: 8px; border-radius: 5px;"
        )
        layout.addWidget(policy)
        self._snapshot_label = QLabel("尚未查询。选择条件后点击“查询 / 刷新”。")
        self._snapshot_label.setWordWrap(True)
        layout.addWidget(self._snapshot_label)
        self._tabs = QTabWidget()
        self._daily_model, self._daily_table = self._add_table_tab(
            "提交数量", export.DAILY_COLUMNS
        )
        self._summary_model, self._summary_table = self._add_table_tab(
            "区间通过率", export.SUMMARY_COLUMNS
        )
        self._detail_model, self._detail_table = self._add_table_tab(
            "订单结果明细", export.DETAIL_COLUMNS
        )
        layout.addWidget(self._tabs, 1)
        self._count_label = QLabel("暂无查询结果")
        self._count_label.setWordWrap(True)
        layout.addWidget(self._count_label)
        actions = QHBoxLayout()
        self._excel_button = QPushButton("导出汇总及全部订单明细（Excel）")
        self._csv_button = QPushButton("导出全部订单明细（CSV）")
        self._excel_button.clicked.connect(lambda: self._export("xlsx"))
        self._csv_button.clicked.connect(lambda: self._export("csv"))
        actions.addWidget(self._excel_button)
        actions.addWidget(self._csv_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        for signal in (
            self._shop_combo.currentTextChanged,
            self._type_combo.currentIndexChanged,
            self._start_date.dateChanged,
            self._end_date.dateChanged,
            self._partial_check.toggled,
        ):
            signal.connect(self._criteria_changed)
        self._update_actions()

    def _add_table_tab(self, name, columns):
        table = _Table()
        model = QueryTableModel(columns, table)
        table.setModel(model)
        table.setSelectionBehavior(QTableView.SelectRows)
        table.setAlternatingRowColors(True)
        table.setSortingEnabled(True)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        table.horizontalHeader().setDefaultSectionSize(110)
        widths = {
            "shop": 150,
            "business_type": 100,
            "order_no": 190,
            "outcome": 105,
            "pass_rate": 110,
            "submission_pass_rate": 135,
            "submitted": 100,
            "goods_title": 220,
            "record_key": 190,
            "submission_time": 205,
        }
        for index, (key, _label) in enumerate(columns):
            table.setColumnWidth(index, widths.get(key, 110))
        table.verticalHeader().setDefaultSectionSize(27)
        table.setWordWrap(False)
        self._tabs.addTab(table, name)
        return model, table

    def _criteria(self):
        shop = self._shop_combo.currentText().strip()
        if not shop or (self._shop_combo.currentIndex() == 0 and shop == "全部店铺"):
            shop = None
        return (
            self._start_date.date().toPyDate(),
            self._end_date.date().toPyDate(),
            shop,
            self._type_combo.currentData(),
            self._partial_check.isChecked(),
        )

    def _criteria_changed(self, *_args):
        if self._report is not None and self._criteria() != self._last_criteria:
            self._snapshot_label.setText(
                "筛选条件已修改。下方保留上次结果，请点击“查询 / 刷新”后再导出。"
            )
        self._update_actions()

    def _update_actions(self):
        busy = self._query_busy or self._export_busy
        self._query_button.setEnabled(not busy)
        valid = (
            self._report is not None
            and self._criteria() == self._last_criteria
            and not busy
        )
        self._excel_button.setEnabled(valid)
        self._csv_button.setEnabled(valid)

    def _quick_range(self, mode):
        today = queries.beijing_now().date()
        end = today - timedelta(days=1) if mode == "yesterday" else today
        start = (
            end
            if mode in {"today", "yesterday"}
            else today - timedelta(days=6)
            if mode == "week"
            else today.replace(day=1)
        )
        self._start_date.setDate(start)
        self._end_date.setDate(end)
        self._tabs.setCurrentIndex(0 if mode in {"today", "yesterday"} else 1)
        if not self._query_busy and not self._export_busy:
            self.query()

    def set_active(self, active):
        self._active = bool(active)
        if (
            active
            and not self._query_busy
            and not self._export_busy
            and (
                self._report is None
                or self._report["today"] != queries.beijing_now().date().isoformat()
            )
        ):
            self.query()

    def _start_job(self, function, completed, failed):
        self._next_job += 1
        job_id = self._next_job
        work = _Work(function)
        self._tasks[job_id] = work

        def success(result):
            self._tasks.pop(job_id, None)
            completed(result)

        def error(message):
            self._tasks.pop(job_id, None)
            failed(message)

        work.signals.completed.connect(success)
        work.signals.failed.connect(error)
        QThreadPool.globalInstance().start(work)

    def query(self):
        if self._query_busy or self._export_busy:
            return
        criteria = self._criteria()
        start, end, shop, kind, partial = criteria
        if start > end:
            msgbox.warning(self, "日期范围有误", "开始日期不能晚于结束日期。")
            return
        self._query_busy = True
        self._snapshot_label.setText("正在按所选提交日期查询数量和审核结果……")
        self._update_actions()
        self._start_job(
            lambda: queries.query_report(
                start, end, shop=shop, business_type=kind, partial_pass=partial
            ),
            lambda report: self._on_result(criteria, report),
            self._on_query_error,
        )

    def _on_result(self, criteria, report):
        self._query_busy = False
        if criteria != self._criteria():
            self._snapshot_label.setText(
                "查询期间筛选条件已修改，本次结果未显示。请重新点击“查询 / 刷新”。"
            )
            self._update_actions()
            return
        current_text = self._shop_combo.currentText()
        self._shop_combo.blockSignals(True)
        self._shop_combo.clear()
        self._shop_combo.addItem("全部店铺", None)
        for shop in report["shops"]:
            self._shop_combo.addItem(shop, shop)
        index = self._shop_combo.findText(current_text)
        if index >= 0:
            self._shop_combo.setCurrentIndex(index)
        else:
            self._shop_combo.setEditText(current_text)
        self._shop_combo.blockSignals(False)
        self._last_criteria = criteria
        self._report = report
        self._daily_model.set_rows(report["daily"])
        self._summary_model.set_rows(report["summary"])
        self._detail_model.set_rows(report["details"])
        period = criteria[0].isoformat() if criteria[0] == criteria[1] else f"{criteria[0]} 至 {criteria[1]}"
        self._tabs.setTabText(0, f"提交数量（{period}）")
        self._tabs.setTabText(2, f"订单结果明细（{len(report['details'])} 条）")
        self._snapshot_label.setText(
            f"当前快照：{criteria[0]} 至 {criteria[1]}（含结束日）｜{criteria[2] or '全部店铺'}｜"
            f"{criteria[3] or '全部类型'}｜更新于 {report['generated_at']}（北京时间）"
        )
        submitted = sum(row["total"] for row in report["daily"])
        passed = sum(row["passed"] for row in report["summary"])
        reviewed = sum(row["reviewed"] for row in report["summary"])
        rate = f"{passed / reviewed:.1%}" if reviewed else "—"
        message = f"所选提交日期 {period}：提交合计 {submitted} 条；订单明细 {len(report['details'])} 条，已出审核结果 {reviewed} 条，计入通过 {passed} 条，汇总审核通过率 {rate}。"
        if report["missing_time"]:
            message += f"另有 {report['missing_time']} 条所选店铺/业务记录缺少可用提交时间，未参与区间统计。"
        self._count_label.setText(message)
        self._update_actions()

    def _on_query_error(self, message):
        self._query_busy = False
        # Old data cannot be exported as if the failed refresh had succeeded.
        self._last_criteria = None
        self._snapshot_label.setText(f"查询失败：{message}")
        self._update_actions()

    def _export(self, kind):
        if (
            self._report is None
            or self._criteria() != self._last_criteria
            or self._query_busy
            or self._export_busy
        ):
            return
        report = self._report
        criteria = report["criteria"]
        name = f"申诉辅助查询_{criteria['start']}_{criteria['end']}.{kind}"
        path, _selected = QFileDialog.getSaveFileName(
            self,
            "导出当前查询结果",
            name,
            "Excel 工作簿 (*.xlsx)" if kind == "xlsx" else "CSV 明细 (*.csv)",
        )
        if not path:
            return
        if not path.lower().endswith("." + kind):
            path += "." + kind
        self._export_busy = True
        self._snapshot_label.setText(f"正在导出 {len(report['details'])} 条订单明细……")
        self._update_actions()

        def write():
            export.export_report(report, path, kind)
            return path

        self._start_job(write, self._on_exported, self._on_export_error)

    def _on_exported(self, path):
        self._export_busy = False
        self._snapshot_label.setText(
            f"导出完成：{Path(path).name}（{len(self._report['details'])} 条订单明细）"
        )
        self._update_actions()
        msgbox.information(
            self,
            "导出完成",
            f"查询汇总和订单明细已保存：\n{path}"
            if str(path).lower().endswith(".xlsx")
            else f"订单明细已保存：\n{path}",
        )

    def _on_export_error(self, message):
        self._export_busy = False
        self._snapshot_label.setText(
            f"导出失败：{message}。查询结果仍保留，可重试导出。"
        )
        self._update_actions()
        msgbox.warning(self, "导出失败", message)

    def is_exporting(self):
        return self._export_busy
