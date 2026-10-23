"""申诉数据报表：运营指标、状态/趋势图、明细筛选与导出。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

from PyQt5.QtChart import (
    QBarCategoryAxis,
    QBarSet,
    QChart,
    QChartView,
    QDateTimeAxis,
    QHorizontalBarSeries,
    QLineSeries,
    QPieSeries,
    QValueAxis,
)
from PyQt5.QtCore import QAbstractTableModel, QDate, QDateTime, QModelIndex, Qt, QTime
from PyQt5.QtGui import QColor, QPainter
from PyQt5.QtWidgets import (
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsSimpleTextItem,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSizePolicy,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

import db
import memory_monitor

from ..dialogs.screenshot_preview_dialog import ScreenshotPreviewDialog
from ..services import export_service, msgbox, time_format
from ..workers.db_poll_worker import DbPoller

_DATE_RANGE_OPTIONS = ["全部", "今天", "近 7 天", "近 30 天", "指定日期"]
_ALL_STATUSES = "全部状态"
_TRUNCATE_LEN = 60


def _replace_chart(view: QChartView, chart: QChart) -> None:
    """替换图表并显式释放 QChartView 不再持有的原生 Qt 图表。"""
    previous = view.chart()
    view.setChart(chart)
    memory_monitor.increment_runtime_metric("report_charts_created")
    if previous is not None and previous is not chart:
        memory_monitor.increment_runtime_metric("report_charts_dispose_requested")
        previous.deleteLater()

# ticketSn 后移；前面优先放运营识别订单所需的字段。
_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("shop", "店铺"),
    ("order_no", "订单编号"),
    ("goods_title", "商品信息"),
    ("issue_type", "问题类型"),
    ("status", "申诉结果"),
    ("compensation_amount", "涉及金额（元）"),
    ("submitted_at", "提交时间"),
    ("result_checked_at", "最后查询时间"),
    ("ticket_sn", "ticketSn"),
    ("record_key", "申诉记录标识"),
]

_APPROVED_STATUSES = {"approved", "申诉通过"}
_REJECTED_STATUSES = {
    "rejected",
    "expired",
    "申诉未通过已处罚",
    "超时未申诉已处罚",
}
_APPEAL_WAITING_STATUSES = {"submitted", "already_appealed", "已提交", "已申诉"}
_WAIT_CONFIRM_STATUSES = {"结果待确认", "review_unknown"}
_STATUS_DISPLAY_TEXT = {
    "pending": "待处理",
    "analyzed": "处理中",
    "submitted": "已提交",
    "simulated": "模拟成功",
    "submit_failed": "提交失败",
    "analysis_failed": "分析失败",
    "not_found": "未找到申诉记录",
    "already_appealed": "已申诉",
    "review_unknown": "结果待确认",
    "approved": "申诉通过",
    "rejected": "申诉未通过已处罚",
    "expired": "超时未申诉已处罚",
}
_PIE_COLORS = [
    "#2563EB",  # 蓝
    "#F59E0B",  # 琥珀
    "#10B981",  # 绿
    "#EF4444",  # 红
    "#8B5CF6",  # 紫
    "#06B6D4",  # 青
    "#F97316",  # 橙
    "#84CC16",  # 青柠
    "#EC4899",  # 粉
    "#64748B",  # 灰蓝
]

_RECON_ALL_STATUSES = "全部复议状态"
_RECON_SUCCESS = "申诉成功"
_RECON_FAILED = "申诉失败"
_RECON_STATUS_DISPLAY_TEXT = {
    "待准备材料": "待准备材料",
    "正在准备材料": "正在准备材料",
    "已提交复议": "待处理",
    "复议提交确认中": "待处理",
    "待处理": "待处理",
    "审核中": "审核中",
    "复议结果待确认": "结果待确认",
    _RECON_SUCCESS: "复议成功",
    _RECON_FAILED: "复议失败",
}
_RECON_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("shop", "店铺"),
    ("order_no", "订单编号"),
    ("goods_title", "商品信息"),
    ("issue_type", "问题类型"),
    ("original_appeal_status", "原申诉结果"),
    ("penalty_amount", "复议金额（元）"),
    ("status", "复议状态"),
    ("refund_amount", "返还金额（元）"),
    ("reconsider_reason", "复议原因"),
    ("platform_reconsidered_at", "复议时间"),
    ("result_checked_at", "最后查询时间"),
    ("record_key", "复议记录标识"),
]

_AFTERSALE_ALL_STATUSES = "全部审核状态"
_AFTERSALE_STATUS_ORDER = (
    "待处理", "审核中", "申诉成功", "部分通过", "申诉失败", "已撤回",
    "结果待确认", "已提交", "材料已准备", "失败",
)
_AFTERSALE_HIDDEN_REPORT_STATUSES = {"材料已准备"}
_AFTERSALE_TABLE_COLUMNS: list[tuple[str, str]] = [
    ("shop", "店铺"),
    ("order_no", "订单编号"),
    ("aftersale_id", "售后编号"),
    ("goods_title", "商品信息"),
    ("appeal_item", "申诉项"),
    ("refund_amount", "退款金额（元）"),
    ("appeal_amount", "申诉金额（元）"),
    ("status", "审核状态"),
    ("appeal_reason", "申诉原因"),
    ("platform_appealed_at", "平台申诉时间"),
    ("submitted_at", "提交时间"),
    ("result_checked_at", "最后查询时间"),
    ("record_key", "申诉记录标识"),
]


def _status_color(status: str, fallback_index: int) -> QColor:
    """常见业务状态使用固定语义色，其它状态使用高对比备用色。"""
    bucket = _status_bucket(status)
    semantic_colors = {
        "待出结果": "#2563EB",
        "结果待确认": "#F59E0B",
        "申诉通过": "#16A34A",
        "申诉失败": "#DC2626",
    }
    return QColor(semantic_colors.get(bucket, _PIE_COLORS[fallback_index % len(_PIE_COLORS)]))


def _truncate(value, length: int = _TRUNCATE_LEN) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= length else text[: length - 1] + "…"


def _display_status(status: object) -> str:
    text = str(status or "未知")
    return _STATUS_DISPLAY_TEXT.get(text, text)


def _parse_timestamp(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value


def _status_bucket(status: object) -> str:
    text = str(status or "未知")
    if text in _APPROVED_STATUSES:
        return "申诉通过"
    if text in _REJECTED_STATUSES:
        return "申诉失败"
    if text in _APPEAL_WAITING_STATUSES:
        return "待出结果"
    if text in _WAIT_CONFIRM_STATUSES:
        return "结果待确认"
    return "非结果记录"


def _status_metrics(rows: list[dict]) -> dict[str, object]:
    metrics: dict[str, object] = {
        "已提交": len(rows),
        "待出结果": 0,
        "等待平台结果": 0,
        "结果待确认": 0,
        "申诉通过": 0,
        "申诉失败": 0,
        "已出结果": 0,
        "申诉通过率": "—",
        "申诉失败率": "—",
        "结果完成率": "—",
        "通过涉及金额": "¥0.00",
        "待出结果金额": "¥0.00",
        "已提交涉及总金额": "¥0.00",
    }
    approved_amount = Decimal("0")
    waiting_amount = Decimal("0")
    total_amount = Decimal("0")
    for row in rows:
        bucket = _status_bucket(row.get("status"))
        if bucket not in ("非结果记录",):
            metrics[bucket] = int(metrics[bucket]) + 1
        amount = _amount(row.get("compensation_amount"))
        total_amount += amount
        if bucket in ("待出结果", "结果待确认"):
            waiting_amount += amount
        elif bucket == "申诉通过":
            approved_amount += amount
    result_total = int(metrics["申诉通过"]) + int(metrics["申诉失败"])
    metrics["等待平台结果"] = int(metrics["待出结果"])
    metrics["待出结果"] = int(metrics["待出结果"]) + int(
        metrics["结果待确认"]
    )
    metrics["已出结果"] = result_total
    if result_total:
        metrics["申诉通过率"] = f"{int(metrics['申诉通过']) / result_total:.1%}"
        metrics["申诉失败率"] = f"{int(metrics['申诉失败']) / result_total:.1%}"
    if rows:
        metrics["结果完成率"] = f"{result_total / len(rows):.1%}"
    metrics["通过涉及金额"] = f"¥{approved_amount:.2f}"
    metrics["待出结果金额"] = f"¥{waiting_amount:.2f}"
    metrics["已提交涉及总金额"] = f"¥{total_amount:.2f}"
    return metrics


def _nice_axis(maximum: int) -> tuple[int, int]:
    """返回自然的纵轴最大值和刻度数，例如 74 -> 80 / 5 个刻度。"""
    if maximum <= 0:
        return 1, 2
    raw_step = maximum * 1.08 / 4
    magnitude = 10 ** math.floor(math.log10(raw_step))
    normalized = raw_step / magnitude
    if normalized <= 1:
        nice = 1
    elif normalized <= 2:
        nice = 2
    elif normalized <= 5:
        nice = 5
    else:
        nice = 10
    step = max(1, int(nice * magnitude))
    axis_maximum = max(step, math.ceil(maximum / step) * step)
    return axis_maximum, axis_maximum // step + 1


def _add_horizontal_bar_value_labels(
    chart: QChart, values: list[int], axis_maximum: int
) -> None:
    """为每根横向柱子绘制不会被 Qt 自动省略的“X 条”标签。"""
    labels = []
    for value in values:
        label = QGraphicsSimpleTextItem(f"{value} 条", chart)
        font = label.font()
        font.setPointSize(9)
        font.setBold(True)
        label.setFont(font)
        label.setZValue(10)
        labels.append(label)

    def position_labels(plot_area) -> None:
        if not labels or plot_area.width() <= 0 or plot_area.height() <= 0:
            return
        row_height = plot_area.height() / len(labels)
        for index, (label, value) in enumerate(zip(labels, values, strict=True)):
            bounds = label.boundingRect()
            bar_end = plot_area.left() + plot_area.width() * value / axis_maximum
            outside_x = bar_end + 5
            # QBarCategoryAxis 的第一项绘制在最下面，标签位置也要按同样顺序反转。
            label_y = (
                plot_area.top()
                + (len(labels) - index - 0.5) * row_height
                - bounds.height() / 2
            )
            if outside_x + bounds.width() <= plot_area.right() - 2:
                label.setPos(outside_x, label_y)
                label.setBrush(QColor("#263238"))
            else:
                label.setPos(
                    max(plot_area.left() + 3, bar_end - bounds.width() - 5),
                    label_y,
                )
                label.setBrush(QColor("#FFFFFF"))

    chart.plotAreaChanged.connect(position_labels)
    position_labels(chart.plotArea())
    chart.value_labels = labels
    chart.value_label_positioner = position_labels


def _build_status_chart(rows: list[dict], clicked=None, hovered=None) -> QChart:
    counts: dict[str, int] = {}
    for row in rows:
        status = _status_bucket(row.get("status"))
        if status == "非结果记录":
            continue
        counts[status] = counts.get(status, 0) + 1

    if not counts:
        chart = QChart()
        chart.setTitle("申诉状态分布（暂无数据）")
        chart.legend().setVisible(False)
        return chart

    total = len(rows)
    statuses = [
        status
        for status in ("待出结果", "结果待确认", "申诉通过", "申诉失败")
        if status in counts
    ]
    values = [counts[status] for status in statuses]
    axis_maximum, tick_count = _nice_axis(max(values))

    chart = QChart()
    chart.statuses = statuses
    chart.status_counts = counts
    chart.total_orders = total
    chart.setTitle(f"提交后申诉结果分布（共 {total} 条；点击柱子筛选）")
    chart.legend().setVisible(False)

    bar_set = QBarSet("申诉记录")
    bar_set.append(values)
    if clicked is not None:
        bar_set.clicked.connect(clicked)
    if hovered is not None:
        bar_set.hovered.connect(hovered)
    series = QHorizontalBarSeries()
    series.append(bar_set)
    series.setLabelsVisible(False)
    chart.addSeries(series)

    category_axis = QBarCategoryAxis()
    category_axis.append(statuses)
    chart.addAxis(category_axis, Qt.AlignLeft)
    series.attachAxis(category_axis)

    value_axis = QValueAxis()
    value_axis.setLabelFormat("%d")
    value_axis.setRange(0, axis_maximum)
    value_axis.setTickCount(tick_count)
    chart.addAxis(value_axis, Qt.AlignBottom)
    series.attachAxis(value_axis)
    _add_horizontal_bar_value_labels(chart, values, axis_maximum)
    return chart


def _build_share_chart(rows: list[dict], clicked=None, hovered=None) -> QChart:
    metrics = _status_metrics(rows)
    success = int(metrics["申诉通过"])
    failed = int(metrics["申诉失败"])
    result_total = success + failed
    chart = QChart()
    chart.legend().setVisible(True)
    chart.legend().setAlignment(Qt.AlignRight)
    if not result_total:
        chart.setTitle("申诉通过率（暂无已出结果记录）")
        return chart
    chart.setTitle(
        f"申诉通过率 {success / result_total:.1%}\n"
        f"已出结果 {result_total} 条（待出结果不计入）"
    )

    series = QPieSeries()
    series.setHoleSize(0.52)
    series.setPieSize(0.76)
    for status, count, color in (
        ("申诉通过", success, "#16A34A"),
        ("申诉失败", failed, "#DC2626"),
    ):
        if count <= 0:
            continue
        pie_slice = series.append(status, count)
        pie_slice.setBrush(QColor(color))
        pie_slice.setBorderColor(QColor("#FFFFFF"))
        pie_slice.setBorderWidth(2)
        pie_slice.setLabel(f"{status}　{count} 条　{count / result_total:.1%}")
        pie_slice.setLabelVisible(False)
        pie_slice.setExplodeDistanceFactor(0.06)
        pie_slice.hovered.connect(
            lambda state, current=pie_slice, current_status=status: _handle_pie_hover(
                current, current_status, state, hovered
            )
        )
        if clicked is not None:
            pie_slice.clicked.connect(lambda status=status: clicked(status))
    chart.addSeries(series)
    center_label = QGraphicsSimpleTextItem(
        f"{success / result_total:.1%}", chart
    )
    center_font = center_label.font()
    center_font.setPointSize(15)
    center_font.setBold(True)
    center_label.setFont(center_font)
    center_label.setBrush(QColor("#1F2937"))

    def position_center_label(plot_area) -> None:
        bounds = center_label.boundingRect()
        center_label.setPos(
            plot_area.center().x() - bounds.width() / 2,
            plot_area.center().y() - bounds.height() / 2,
        )

    chart.plotAreaChanged.connect(position_center_label)
    position_center_label(chart.plotArea())
    chart.rate_center_label = center_label
    return chart


def _handle_pie_hover(pie_slice, status: str, state: bool, callback) -> None:
    pie_slice.setExploded(state)
    if callback is not None:
        callback(state, status)


def _build_trend_chart(rows: list[dict], days: int) -> QChart:
    today = date.today()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    success_counts = {day: 0 for day in dates}
    failed_counts = {day: 0 for day in dates}
    for row in rows:
        timestamp = _parse_timestamp(row.get("submitted_at"))
        if timestamp is None:
            continue
        local_day = timestamp.astimezone().date()
        if local_day not in success_counts:
            continue
        bucket = _status_bucket(row.get("status"))
        if bucket == "申诉通过":
            success_counts[local_day] += 1
        elif bucket == "申诉失败":
            failed_counts[local_day] += 1

    success_total = sum(success_counts.values())
    failed_total = sum(failed_counts.values())
    result_total = success_total + failed_total
    rate_text = (
        f"；阶段通过率 {success_total / result_total:.1%}"
        if result_total
        else ""
    )
    chart = QChart()
    chart.setTitle(f"最近 {days} 天申诉结果趋势（按提交时间{rate_text}）")
    chart.legend().setVisible(True)
    chart.legend().setAlignment(Qt.AlignBottom)
    success_series = QLineSeries()
    success_series.setName("申诉通过")
    success_series.setColor(QColor("#16A34A"))
    failed_series = QLineSeries()
    failed_series.setName("申诉失败")
    failed_series.setColor(QColor("#DC2626"))
    for day in dates:
        point_time = QDateTime(QDate(day.year, day.month, day.day), QTime(12, 0))
        timestamp = point_time.toMSecsSinceEpoch()
        success_series.append(timestamp, success_counts[day])
        failed_series.append(timestamp, failed_counts[day])
    for series in (success_series, failed_series):
        series.setPointsVisible(True)
        chart.addSeries(series)

    axis_x = QDateTimeAxis()
    axis_x.setFormat("MM-dd")
    axis_x.setTickCount(7 if days > 7 else days)
    first = dates[0]
    last = dates[-1]
    axis_x.setRange(
        QDateTime(QDate(first.year, first.month, first.day), QTime(0, 0)),
        QDateTime(QDate(last.year, last.month, last.day), QTime(23, 59)),
    )
    chart.addAxis(axis_x, Qt.AlignBottom)

    maximum = max([*success_counts.values(), *failed_counts.values()], default=0)
    axis_maximum, tick_count = _nice_axis(maximum)
    axis_y = QValueAxis()
    axis_y.setLabelFormat("%d")
    axis_y.setRange(0, axis_maximum)
    axis_y.setTickCount(tick_count)
    chart.addAxis(axis_y, Qt.AlignLeft)
    for series in (success_series, failed_series):
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
    return chart


def _display_reconsider_status(status: object) -> str:
    text = str(status or "未知")
    return _RECON_STATUS_DISPLAY_TEXT.get(text, text)


def _reconsider_status_bucket(status: object) -> str:
    text = str(status or "未知")
    if text == _RECON_SUCCESS:
        return "复议成功"
    if text == _RECON_FAILED:
        return "复议失败"
    if text == "审核中":
        return "审核中"
    if text == "复议结果待确认":
        return "结果待确认"
    return "待处理"


def _amount(value: object) -> Decimal:
    try:
        return Decimal(str(value or "0").strip())
    except InvalidOperation:
        return Decimal("0")


def _visible_aftersale_rows(rows: list[dict]) -> list[dict]:
    return [
        row
        for row in rows
        if str(row.get("status") or "") not in _AFTERSALE_HIDDEN_REPORT_STATUSES
    ]


def _aftersale_metrics(rows: list[dict]) -> dict[str, object]:
    rows = _visible_aftersale_rows(rows)
    counts = {
        status: sum(1 for row in rows if row.get("status") == status)
        for status in _AFTERSALE_STATUS_ORDER
    }
    waiting = sum(
        counts.get(status, 0) for status in ("待处理", "审核中", "结果待确认", "已提交")
    )
    success = counts.get("申诉成功", 0)
    partial = counts.get("部分通过", 0)
    failed = counts.get("申诉失败", 0)
    result_total = success + partial + failed
    pass_rate = f"{(success + partial) / result_total:.1%}" if result_total else "—"
    total_amount = sum((_amount(row.get("appeal_amount")) for row in rows), Decimal(0))
    success_amount = sum(
        (
            _amount(row.get("appeal_amount"))
            for row in rows
            if row.get("status") == "申诉成功"
        ),
        Decimal(0),
    )
    partial_amount = sum(
        (
            _amount(row.get("appeal_amount"))
            for row in rows
            if row.get("status") == "部分通过"
        ),
        Decimal(0),
    )
    return {
        "售后通过率": pass_rate,
        "申诉总数": len(rows),
        "待出结果": waiting,
        "申诉成功": success,
        "部分通过": partial,
        "申诉失败": failed,
        "申诉总金额": f"¥{total_amount:.2f}",
        "成功申诉金额": f"¥{success_amount:.2f}",
        "部分通过返还金额": f"¥{partial_amount:.2f}",
        "已撤回": counts.get("已撤回", 0),
        "已出结果": result_total,
    }


def _aftersale_timestamp(row: dict) -> datetime | None:
    for key in ("platform_appealed_at", "submitted_at", "updated_at", "discovered_at"):
        value = _parse_timestamp(row.get(key))
        if value is not None:
            return value
    return None


def _build_aftersale_status_chart(
    rows: list[dict], clicked=None, hovered=None
) -> QChart:
    rows = _visible_aftersale_rows(rows)
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "未知")
        counts[status] = counts.get(status, 0) + 1
    if not counts:
        chart = QChart()
        chart.setTitle("售后申诉状态分布（暂无数据）")
        chart.legend().setVisible(False)
        return chart

    statuses = [status for status in _AFTERSALE_STATUS_ORDER if status in counts]
    statuses.extend(sorted(status for status in counts if status not in statuses))
    values = [counts[status] for status in statuses]
    axis_maximum, tick_count = _nice_axis(max(values))
    chart = QChart()
    chart.statuses = statuses
    chart.status_counts = counts
    chart.total_orders = len(rows)
    chart.setTitle(f"各状态售后申诉数量（共 {len(rows)} 条；点击柱子筛选）")
    chart.legend().setVisible(False)

    bar_set = QBarSet("售后申诉数")
    bar_set.append(values)
    if clicked is not None:
        bar_set.clicked.connect(clicked)
    if hovered is not None:
        bar_set.hovered.connect(hovered)
    series = QHorizontalBarSeries()
    series.append(bar_set)
    series.setLabelsVisible(False)
    chart.addSeries(series)

    category_axis = QBarCategoryAxis()
    category_axis.append(statuses)
    chart.addAxis(category_axis, Qt.AlignLeft)
    series.attachAxis(category_axis)
    value_axis = QValueAxis()
    value_axis.setLabelFormat("%d")
    value_axis.setRange(0, axis_maximum)
    value_axis.setTickCount(tick_count)
    chart.addAxis(value_axis, Qt.AlignBottom)
    series.attachAxis(value_axis)
    _add_horizontal_bar_value_labels(chart, values, axis_maximum)
    return chart


def _build_aftersale_rate_chart(
    rows: list[dict], clicked=None, hovered=None
) -> QChart:
    metrics = _aftersale_metrics(rows)
    success = int(metrics["申诉成功"])
    partial = int(metrics["部分通过"])
    failed = int(metrics["申诉失败"])
    result_total = success + partial + failed
    chart = QChart()
    chart.legend().setVisible(True)
    chart.legend().setAlignment(Qt.AlignRight)
    if not result_total:
        chart.setTitle("售后通过率（暂无已出结果记录）")
        return chart

    passed = success + partial
    chart.setTitle(
        f"售后通过率 {passed / result_total:.1%}\n"
        f"已出结果 {result_total} 条（待出结果不计入）"
    )
    series = QPieSeries()
    series.setHoleSize(0.52)
    series.setPieSize(0.76)
    for status, count, color in (
        ("申诉成功", success, "#16A34A"),
        ("部分通过", partial, "#D97706"),
        ("申诉失败", failed, "#DC2626"),
    ):
        if count <= 0:
            continue
        pie_slice = series.append(status, count)
        pie_slice.setBrush(QColor(color))
        pie_slice.setBorderColor(QColor("#FFFFFF"))
        pie_slice.setBorderWidth(2)
        pie_slice.setLabel(f"{status}　{count} 条　{count / result_total:.1%}")
        pie_slice.setLabelVisible(False)
        pie_slice.setExplodeDistanceFactor(0.06)
        pie_slice.hovered.connect(
            lambda state, current=pie_slice, current_status=status: _handle_pie_hover(
                current, current_status, state, hovered
            )
        )
        if clicked is not None:
            pie_slice.clicked.connect(lambda status=status: clicked(status))
    chart.addSeries(series)
    center_label = QGraphicsSimpleTextItem(f"{passed / result_total:.1%}", chart)
    center_font = center_label.font()
    center_font.setPointSize(15)
    center_font.setBold(True)
    center_label.setFont(center_font)
    center_label.setBrush(QColor("#1F2937"))

    def position_center_label(plot_area) -> None:
        bounds = center_label.boundingRect()
        center_label.setPos(
            plot_area.center().x() - bounds.width() / 2,
            plot_area.center().y() - bounds.height() / 2,
        )

    chart.plotAreaChanged.connect(position_center_label)
    position_center_label(chart.plotArea())
    chart.rate_center_label = center_label
    return chart


def _build_aftersale_trend_chart(rows: list[dict], days: int) -> QChart:
    today = date.today()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    counts = {
        status: {day: 0 for day in dates}
        for status in ("申诉成功", "部分通过", "申诉失败")
    }
    for row in rows:
        status = str(row.get("status") or "")
        if status not in counts:
            continue
        timestamp = _aftersale_timestamp(row)
        if timestamp is None:
            continue
        local_day = timestamp.astimezone().date()
        if local_day in counts[status]:
            counts[status][local_day] += 1

    success_total = sum(counts["申诉成功"].values())
    partial_total = sum(counts["部分通过"].values())
    failed_total = sum(counts["申诉失败"].values())
    result_total = success_total + partial_total + failed_total
    rate_text = (
        f"；阶段通过率 {(success_total + partial_total) / result_total:.1%}"
        if result_total
        else ""
    )
    chart = QChart()
    chart.setTitle(f"最近 {days} 天售后申诉结果趋势（按申诉时间{rate_text}）")
    chart.legend().setVisible(True)
    chart.legend().setAlignment(Qt.AlignBottom)
    series_list = []
    for status, color in (
        ("申诉成功", "#16A34A"),
        ("部分通过", "#D97706"),
        ("申诉失败", "#DC2626"),
    ):
        series = QLineSeries()
        series.setName(status)
        series.setColor(QColor(color))
        for day in dates:
            point_time = QDateTime(QDate(day.year, day.month, day.day), QTime(12, 0))
            series.append(point_time.toMSecsSinceEpoch(), counts[status][day])
        series.setPointsVisible(True)
        chart.addSeries(series)
        series_list.append(series)

    axis_x = QDateTimeAxis()
    axis_x.setFormat("MM-dd")
    axis_x.setTickCount(7 if days > 7 else days)
    first, last = dates[0], dates[-1]
    axis_x.setRange(
        QDateTime(QDate(first.year, first.month, first.day), QTime(0, 0)),
        QDateTime(QDate(last.year, last.month, last.day), QTime(23, 59)),
    )
    chart.addAxis(axis_x, Qt.AlignBottom)
    maximum = max(
        (value for status_counts in counts.values() for value in status_counts.values()),
        default=0,
    )
    axis_maximum, tick_count = _nice_axis(maximum)
    axis_y = QValueAxis()
    axis_y.setLabelFormat("%d")
    axis_y.setRange(0, axis_maximum)
    axis_y.setTickCount(tick_count)
    chart.addAxis(axis_y, Qt.AlignLeft)
    for series in series_list:
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
    return chart


def _reconsider_metrics(rows: list[dict]) -> dict[str, object]:
    metrics: dict[str, object] = {
        "已提交": len(rows),
        "待处理": 0,
        "审核中": 0,
        "结果待确认": 0,
        "待出结果": 0,
        "复议成功": 0,
        "复议失败": 0,
        "已出结果": 0,
        "复议通过率": "—",
        "复议失败率": "—",
        "结果完成率": "—",
        "成功返还金额": "¥0.00",
        "待出结果金额": "¥0.00",
        "总复议金额": "¥0.00",
    }
    refund_total = Decimal("0")
    waiting_amount = Decimal("0")
    total_amount = Decimal("0")
    for row in rows:
        bucket = _reconsider_status_bucket(row.get("status"))
        metrics[bucket] = int(metrics[bucket]) + 1
        amount = _amount(row.get("penalty_amount"))
        total_amount += amount
        if bucket == "复议成功":
            refund_total += _amount(row.get("refund_amount"))
        elif bucket in ("待处理", "审核中", "结果待确认"):
            waiting_amount += amount
    metrics["待出结果"] = (
        int(metrics["待处理"])
        + int(metrics["审核中"])
        + int(metrics["结果待确认"])
    )
    result_total = int(metrics["复议成功"]) + int(metrics["复议失败"])
    metrics["已出结果"] = result_total
    if result_total:
        metrics["复议通过率"] = (
            f"{int(metrics['复议成功']) / result_total:.1%}"
        )
        metrics["复议失败率"] = (
            f"{int(metrics['复议失败']) / result_total:.1%}"
        )
    if rows:
        metrics["结果完成率"] = f"{result_total / len(rows):.1%}"
    metrics["成功返还金额"] = f"¥{refund_total:.2f}"
    metrics["待出结果金额"] = f"¥{waiting_amount:.2f}"
    metrics["总复议金额"] = f"¥{total_amount:.2f}"
    return metrics


def _reconsider_timestamp(row: dict) -> datetime | None:
    platform_value = row.get("platform_reconsidered_at")
    if platform_value:
        try:
            value = datetime.fromisoformat(str(platform_value))
        except ValueError:
            value = None
        if value is not None:
            if value.tzinfo is None:
                return value.astimezone()
            return value
    return _parse_timestamp(row.get("submitted_at"))


def _reconsider_status_color(status: str, fallback_index: int) -> QColor:
    colors = {
        "待处理": "#64748B",
        "审核中": "#2563EB",
        "结果待确认": "#F59E0B",
        "复议成功": "#16A34A",
        "复议失败": "#DC2626",
    }
    bucket = _reconsider_status_bucket(status)
    return QColor(colors.get(bucket, _PIE_COLORS[fallback_index % len(_PIE_COLORS)]))


def _build_reconsider_status_chart(
    rows: list[dict], clicked=None, hovered=None
) -> QChart:
    counts: dict[str, int] = {}
    for row in rows:
        status = _reconsider_status_bucket(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    if not counts:
        chart = QChart()
        chart.setTitle("复议状态分布（暂无数据）")
        chart.legend().setVisible(False)
        return chart

    statuses = [
        status
        for status in ("待处理", "审核中", "结果待确认", "复议成功", "复议失败")
        if status in counts
    ]
    values = [counts[status] for status in statuses]
    axis_maximum, tick_count = _nice_axis(max(values))
    chart = QChart()
    chart.statuses = statuses
    chart.status_counts = counts
    chart.total_orders = len(rows)
    chart.setTitle(f"各状态复议数量（共 {len(rows)} 条；点击柱子筛选）")
    chart.legend().setVisible(False)

    bar_set = QBarSet("复议数")
    bar_set.append(values)
    if clicked is not None:
        bar_set.clicked.connect(clicked)
    if hovered is not None:
        bar_set.hovered.connect(hovered)
    series = QHorizontalBarSeries()
    series.append(bar_set)
    series.setLabelsVisible(False)
    chart.addSeries(series)

    category_axis = QBarCategoryAxis()
    category_axis.append(statuses)
    chart.addAxis(category_axis, Qt.AlignLeft)
    series.attachAxis(category_axis)
    value_axis = QValueAxis()
    value_axis.setLabelFormat("%d")
    value_axis.setRange(0, axis_maximum)
    value_axis.setTickCount(tick_count)
    chart.addAxis(value_axis, Qt.AlignBottom)
    series.attachAxis(value_axis)
    _add_horizontal_bar_value_labels(chart, values, axis_maximum)
    return chart


def _build_reconsider_rate_chart(
    rows: list[dict], clicked=None, hovered=None
) -> QChart:
    success = sum(1 for row in rows if row.get("status") == _RECON_SUCCESS)
    failed = sum(1 for row in rows if row.get("status") == _RECON_FAILED)
    result_total = success + failed
    chart = QChart()
    chart.legend().setVisible(True)
    chart.legend().setAlignment(Qt.AlignRight)
    if not result_total:
        chart.setTitle("复议通过率（暂无已出结果记录）")
        return chart
    chart.setTitle(
        f"复议通过率 {success / result_total:.1%}\n"
        f"已出结果 {result_total} 条（待处理状态不计入）"
    )
    series = QPieSeries()
    series.setHoleSize(0.52)
    series.setPieSize(0.76)
    for status, count, color in (
        ("复议成功", success, "#16A34A"),
        ("复议失败", failed, "#DC2626"),
    ):
        if count <= 0:
            continue
        pie_slice = series.append(status, count)
        pie_slice.setBrush(QColor(color))
        pie_slice.setBorderColor(QColor("#FFFFFF"))
        pie_slice.setBorderWidth(2)
        pie_slice.setLabel(
            f"{status}　{count} 条　{count / result_total:.1%}"
        )
        pie_slice.setLabelVisible(False)
        pie_slice.setExplodeDistanceFactor(0.06)
        pie_slice.hovered.connect(
            lambda state, current=pie_slice, current_status=status: _handle_pie_hover(
                current, current_status, state, hovered
            )
        )
        if clicked is not None:
            pie_slice.clicked.connect(lambda status=status: clicked(status))
    chart.addSeries(series)
    center_label = QGraphicsSimpleTextItem(
        f"{success / result_total:.1%}", chart
    )
    center_font = center_label.font()
    center_font.setPointSize(15)
    center_font.setBold(True)
    center_label.setFont(center_font)
    center_label.setBrush(QColor("#1F2937"))

    def position_center_label(plot_area) -> None:
        bounds = center_label.boundingRect()
        center_label.setPos(
            plot_area.center().x() - bounds.width() / 2,
            plot_area.center().y() - bounds.height() / 2,
        )

    chart.plotAreaChanged.connect(position_center_label)
    position_center_label(chart.plotArea())
    chart.rate_center_label = center_label
    return chart


def _build_reconsider_trend_chart(rows: list[dict], days: int) -> QChart:
    today = date.today()
    dates = [today - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    success_counts = {day: 0 for day in dates}
    failed_counts = {day: 0 for day in dates}
    for row in rows:
        timestamp = _reconsider_timestamp(row)
        if timestamp is None:
            continue
        local_day = timestamp.astimezone().date()
        if local_day not in success_counts:
            continue
        if row.get("status") == _RECON_SUCCESS:
            success_counts[local_day] += 1
        elif row.get("status") == _RECON_FAILED:
            failed_counts[local_day] += 1

    success_total = sum(success_counts.values())
    failed_total = sum(failed_counts.values())
    result_total = success_total + failed_total
    rate_text = (
        f"；阶段通过率 {success_total / result_total:.1%}"
        if result_total
        else ""
    )
    chart = QChart()
    chart.setTitle(f"最近 {days} 天复议结果趋势（按复议时间{rate_text}）")
    chart.legend().setVisible(True)
    chart.legend().setAlignment(Qt.AlignBottom)
    success_series = QLineSeries()
    success_series.setName("复议成功")
    success_series.setColor(QColor("#16A34A"))
    failed_series = QLineSeries()
    failed_series.setName("复议失败")
    failed_series.setColor(QColor("#DC2626"))
    for day in dates:
        point_time = QDateTime(QDate(day.year, day.month, day.day), QTime(12, 0))
        timestamp = point_time.toMSecsSinceEpoch()
        success_series.append(timestamp, success_counts[day])
        failed_series.append(timestamp, failed_counts[day])
    for series in (success_series, failed_series):
        series.setPointsVisible(True)
        chart.addSeries(series)

    axis_x = QDateTimeAxis()
    axis_x.setFormat("MM-dd")
    axis_x.setTickCount(7 if days > 7 else days)
    first = dates[0]
    last = dates[-1]
    axis_x.setRange(
        QDateTime(QDate(first.year, first.month, first.day), QTime(0, 0)),
        QDateTime(QDate(last.year, last.month, last.day), QTime(23, 59)),
    )
    chart.addAxis(axis_x, Qt.AlignBottom)
    maximum = max([*success_counts.values(), *failed_counts.values()], default=0)
    axis_maximum, tick_count = _nice_axis(maximum)
    axis_y = QValueAxis()
    axis_y.setLabelFormat("%d")
    axis_y.setRange(0, axis_maximum)
    axis_y.setTickCount(tick_count)
    chart.addAxis(axis_y, Qt.AlignLeft)
    for series in (success_series, failed_series):
        series.attachAxis(axis_x)
        series.attachAxis(axis_y)
    return chart


class _MetricCard(QFrame):
    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("reportMetricCard")
        self.setStyleSheet(
            "#reportMetricCard { background: #f6f8fa; border: 1px solid #e2e5e9; "
            "border-radius: 7px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        self._value = QLabel("0")
        font = self._value.font()
        font.setPointSize(18)
        font.setBold(True)
        self._value.setFont(font)
        caption = QLabel(title)
        caption.setStyleSheet("color: #616161;")
        layout.addWidget(self._value)
        layout.addWidget(caption)

    def set_value(self, value: object) -> None:
        self._value.setText(str(value))


class OrderTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, index: QModelIndex) -> dict | None:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        return self._rows[index.row()]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_TABLE_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return _TABLE_COLUMNS[section][1]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        key, _ = _TABLE_COLUMNS[index.column()]
        row = self._rows[index.row()]
        raw = row.get(key)
        if key in ("submitted_at", "updated_at", "result_checked_at"):
            text = time_format.format_local(raw)
        elif key == "status":
            text = _display_status(raw)
        elif role == Qt.DisplayRole and key in (
            "issue_type",
            "goods_title",
            "appeal_text",
            "error_message",
        ):
            text = _truncate(raw)
        else:
            text = "" if raw is None else str(raw)
        if role in (Qt.DisplayRole, Qt.ToolTipRole):
            if role == Qt.DisplayRole or key == "status":
                return text
            return "" if raw is None else str(raw)
        return None


class _OrderDetailDialog(QDialog):
    def __init__(self, row: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self.setWindowTitle(f"订单 {row.get('order_no') or ''} — 申诉详情")
        self.resize(960, 720)
        layout = QVBoxLayout(self)

        form = QFormLayout()
        fields = (
            ("店铺", "shop"),
            ("订单编号", "order_no"),
            ("申诉状态", "status"),
            ("ticketSn", "ticket_sn"),
            ("申诉原因", "issue_type"),
            ("商品信息", "goods_title"),
            ("补偿金额", "compensation_amount"),
            ("申诉说明", "appeal_text"),
            ("结果/错误说明", "error_message"),
            ("提交时间", "submitted_at"),
            ("最后查询时间", "result_checked_at"),
        )
        for title, key in fields:
            value = row.get(key)
            if key == "status":
                value = _display_status(value)
            elif key in ("submitted_at", "result_checked_at"):
                value = time_format.format_local(value)
            label = QLabel("" if value is None else str(value))
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(f"{title}：", label)
        layout.addLayout(form)

        records = db.list_appeal_records(str(row.get("shop") or ""), str(row.get("order_no") or ""))
        layout.addWidget(QLabel(f"申诉记录（{len(records)} 条）"))
        record_table = QTableWidget(len(records), 5)
        record_table.setHorizontalHeaderLabels(["记录标识", "问题类型", "状态", "说明", "更新时间"])
        for row_index, record in enumerate(records):
            values = (
                record.get("record_key"),
                record.get("issue_type"),
                _display_status(record.get("status")),
                record.get("error_message"),
                time_format.format_local(record.get("updated_at")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setToolTip(item.text())
                record_table.setItem(row_index, column, item)
        record_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        record_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(record_table, stretch=1)

        history = db.list_appeal_review_history(
            str(row.get("shop") or ""), str(row.get("order_no") or "")
        )
        layout.addWidget(QLabel(f"回查历史（{len(history)} 条；从本版本起记录）"))
        history_table = QTableWidget(len(history), 3)
        history_table.setHorizontalHeaderLabels(["回查状态", "回查说明", "回查时间"])
        for row_index, entry in enumerate(history):
            values = (
                entry.get("status"),
                entry.get("detail"),
                time_format.format_local(entry.get("checked_at")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setToolTip(item.text())
                history_table.setItem(row_index, column, item)
        history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(history_table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        screenshot_button = buttons.addButton("查看截图", QDialogButtonBox.ActionRole)
        screenshot_button.clicked.connect(self._show_screenshots)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_screenshots(self) -> None:
        labeled_paths = _screenshot_paths(self._row)
        dialog = ScreenshotPreviewDialog(
            f"订单 {self._row.get('order_no')} 的凭证截图", labeled_paths, self
        )
        dialog.exec_()


class ReconsiderationTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, index: QModelIndex) -> dict | None:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        return self._rows[index.row()]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_RECON_TABLE_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return _RECON_TABLE_COLUMNS[section][1]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        key, _ = _RECON_TABLE_COLUMNS[index.column()]
        row = self._rows[index.row()]
        raw = row.get(key)
        if key in ("submitted_at", "result_checked_at"):
            text = time_format.format_local(raw)
        elif key == "platform_reconsidered_at":
            text = "" if raw is None else str(raw)
        elif key == "status":
            text = _display_reconsider_status(raw)
        elif key == "original_appeal_status":
            text = _display_status(raw)
        elif key in ("goods_title", "reconsider_reason", "record_key"):
            text = _truncate(raw)
        else:
            text = "" if raw is None else str(raw)
        if role == Qt.DisplayRole:
            return text
        if role == Qt.ToolTipRole:
            return "" if raw is None else str(raw)
        return None


class _ReconsiderationDetailDialog(QDialog):
    def __init__(self, row: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self.setWindowTitle(f"订单 {row.get('order_no') or ''} — 复议详情")
        self.resize(960, 720)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        fields = (
            ("店铺", "shop"),
            ("订单编号", "order_no"),
            ("原申诉结果", "original_appeal_status"),
            ("复议状态", "status"),
            ("商品信息", "goods_title"),
            ("问题类型", "issue_type"),
            ("复议金额", "penalty_amount"),
            ("返还金额", "refund_amount"),
            ("复议原因", "reconsider_reason"),
            ("复议说明", "reconsider_text"),
            ("平台复议时间", "platform_reconsidered_at"),
            ("最后查询时间", "result_checked_at"),
            ("平台结果原文", "result_text"),
            ("结果/错误说明", "error_message"),
        )
        for title, key in fields:
            value = row.get(key)
            if key == "status":
                value = _display_reconsider_status(value)
            elif key == "original_appeal_status":
                value = _display_status(value)
            elif key == "result_checked_at":
                value = time_format.format_local(value)
            label = QLabel("" if value is None else str(value))
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(f"{title}：", label)
        layout.addLayout(form)

        history = db.list_reconsideration_review_history(
            str(row.get("shop") or ""),
            str(row.get("order_no") or ""),
            str(row.get("record_key") or ""),
        )
        layout.addWidget(QLabel(f"复议结果回查历史（{len(history)} 条）"))
        history_table = QTableWidget(len(history), 5)
        history_table.setHorizontalHeaderLabels(
            ["复议状态", "返还金额", "平台复议时间", "回查说明", "回查时间"]
        )
        for row_index, entry in enumerate(history):
            values = (
                _display_reconsider_status(entry.get("status")),
                entry.get("refund_amount"),
                entry.get("platform_reconsidered_at"),
                entry.get("detail"),
                time_format.format_local(entry.get("checked_at")),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setToolTip(item.text())
                history_table.setItem(row_index, column, item)
        history_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        history_table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(history_table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        screenshot_button = buttons.addButton(
            "查看复议凭证", QDialogButtonBox.ActionRole
        )
        screenshot_button.clicked.connect(self._show_screenshots)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_screenshots(self) -> None:
        dialog = ScreenshotPreviewDialog(
            f"订单 {self._row.get('order_no')} 的复议凭证",
            _screenshot_paths(self._row),
            self,
        )
        dialog.exec_()


class AftersaleTableModel(QAbstractTableModel):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._rows: list[dict] = []

    def set_rows(self, rows: list[dict]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def row_at(self, index: QModelIndex) -> dict | None:
        if not index.isValid() or not (0 <= index.row() < len(self._rows)):
            return None
        return self._rows[index.row()]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: N802
        return 0 if parent.isValid() else len(_AFTERSALE_TABLE_COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):  # noqa: N802
        if role != Qt.DisplayRole or orientation != Qt.Horizontal:
            return None
        return _AFTERSALE_TABLE_COLUMNS[section][1]

    def data(self, index: QModelIndex, role: int = Qt.DisplayRole):
        if not index.isValid():
            return None
        key, _ = _AFTERSALE_TABLE_COLUMNS[index.column()]
        raw = self._rows[index.row()].get(key)
        if key in ("submitted_at", "result_checked_at"):
            text = time_format.format_local(raw)
        elif key in ("goods_title", "appeal_reason", "record_key"):
            text = _truncate(raw)
        else:
            text = "" if raw is None else str(raw)
        if role == Qt.DisplayRole:
            return text
        if role == Qt.ToolTipRole:
            return "" if raw is None else str(raw)
        if role == Qt.ForegroundRole and key == "status":
            colors = {
                "申诉成功": "#16A34A", "部分通过": "#D97706",
                "申诉失败": "#DC2626", "已撤回": "#64748B",
                "审核中": "#2563EB", "结果待确认": "#F59E0B",
            }
            return QColor(colors.get(text, "#334155"))
        return None


class _AftersaleDetailDialog(QDialog):
    def __init__(self, row: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._row = row
        self.setWindowTitle(f"订单 {row.get('order_no') or ''} — 售后申诉详情")
        self.resize(960, 760)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        fields = (
            ("店铺", "shop"), ("订单编号", "order_no"),
            ("售后编号", "aftersale_id"), ("商品信息", "goods_title"),
            ("审核状态", "status"), ("退款金额", "refund_amount"),
            ("申诉金额", "appeal_amount"), ("申诉项", "appeal_item"),
            ("申诉原因", "appeal_reason"), ("申诉文案", "appeal_text"),
            ("平台申诉时间", "platform_appealed_at"),
            ("提交时间", "submitted_at"), ("最后查询时间", "result_checked_at"),
            ("平台结果原文", "result_text"), ("结果/错误说明", "error_message"),
        )
        for title, key in fields:
            value = row.get(key)
            if key in ("submitted_at", "result_checked_at"):
                value = time_format.format_local(value)
            label = QLabel("" if value is None else str(value))
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(f"{title}：", label)
        layout.addLayout(form)

        history = db.list_aftersale_appeal_review_history(
            str(row.get("shop") or ""), str(row.get("order_no") or ""),
            str(row.get("record_key") or ""),
        )
        layout.addWidget(QLabel(f"售后结果回查历史（{len(history)} 条）"))
        table = QTableWidget(len(history), 6)
        table.setHorizontalHeaderLabels(
            ["审核状态", "申诉项", "申诉金额", "平台时间", "回查说明", "回查时间"]
        )
        for row_index, entry in enumerate(history):
            values = (entry.get("status"), entry.get("appeal_item"),
                      entry.get("appeal_amount"), entry.get("platform_appealed_at"),
                      entry.get("detail"), time_format.format_local(entry.get("checked_at")))
            for column, value in enumerate(values):
                item = QTableWidgetItem("" if value is None else str(value))
                item.setToolTip(item.text())
                table.setItem(row_index, column, item)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        layout.addWidget(table, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        screenshot_button = buttons.addButton("查看售后凭证", QDialogButtonBox.ActionRole)
        screenshot_button.clicked.connect(self._show_screenshots)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_screenshots(self) -> None:
        paths: list[tuple[str, str]] = []
        if self._row.get("refund_application_path"):
            paths.append(("退款申请截图", self._row["refund_application_path"]))
        evidence = self._row.get("chat_evidence_paths") or []
        paths.extend((f"聊天记录凭证 {i}/{len(evidence)}", path)
                     for i, path in enumerate(evidence, 1))
        if self._row.get("form_screenshot_path"):
            paths.append(("填表后截图", self._row["form_screenshot_path"]))
        if self._row.get("error_screenshot_path"):
            paths.append(("错误现场截图", self._row["error_screenshot_path"]))
        ScreenshotPreviewDialog(
            f"订单 {self._row.get('order_no')} 的售后申诉凭证", paths, self
        ).exec_()


def _screenshot_paths(row: dict) -> list[tuple[str, str]]:
    paths: list[tuple[str, str]] = []
    evidence = row.get("evidence_paths") or []
    for index, rel_path in enumerate(evidence, start=1):
        paths.append((f"聊天记录凭证 {index}/{len(evidence)}", rel_path))
    if row.get("form_screenshot_path"):
        paths.append(("填表后截图", row["form_screenshot_path"]))
    if row.get("error_screenshot_path"):
        paths.append(("错误现场截图", row["error_screenshot_path"]))
    return paths


class ReportsPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)

        header = QLabel("申诉数据报表")
        header_font = header.font()
        header_font.setPointSize(14)
        header_font.setBold(True)
        header.setFont(header_font)
        root.addWidget(header)

        self._report_tabs = QTabWidget()
        appeal_tab = QWidget()
        appeal_root = QVBoxLayout(appeal_tab)
        appeal_root.setContentsMargins(0, 4, 0, 0)
        self._report_tabs.addTab(appeal_tab, "首次申诉")
        root.addWidget(self._report_tabs, stretch=1)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("店铺："))
        self._shop_combo = QComboBox()
        filter_row.addWidget(self._shop_combo)
        filter_row.addWidget(QLabel("提交时间："))
        self._range_combo = QComboBox()
        self._range_combo.addItems(_DATE_RANGE_OPTIONS)
        filter_row.addWidget(self._range_combo)

        self._start_date = QDateEdit(QDate.currentDate().addDays(-30))
        self._end_date = QDateEdit(QDate.currentDate())
        for editor in (self._start_date, self._end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setEnabled(False)
        self._date_separator = QLabel("至")
        self._date_separator.setEnabled(False)
        filter_row.addWidget(self._start_date)
        filter_row.addWidget(self._date_separator)
        filter_row.addWidget(self._end_date)

        filter_row.addWidget(QLabel("结果状态："))
        self._status_combo = QComboBox()
        self._status_combo.addItem(_ALL_STATUSES, None)
        filter_row.addWidget(self._status_combo)

        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._reload)
        filter_row.addWidget(refresh_button)
        self._last_updated_label = QLabel("最后更新时间：--")
        self._last_updated_label.setStyleSheet("color: #757575;")
        filter_row.addWidget(self._last_updated_label)
        filter_row.addStretch(1)
        appeal_root.addLayout(filter_row)

        metrics_row = QHBoxLayout()
        self._metric_cards = {
            name: _MetricCard(name)
            for name in (
                "申诉通过率",
                "申诉失败率",
                "申诉通过",
                "申诉失败",
                "待出结果",
                "通过涉及金额",
            )
        }
        for card in self._metric_cards.values():
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            metrics_row.addWidget(card)
        appeal_root.addLayout(metrics_row)
        self._appeal_secondary_metrics_label = QLabel()
        self._appeal_secondary_metrics_label.setObjectName("statusChip")
        self._appeal_secondary_metrics_label.setWordWrap(True)
        appeal_root.addWidget(self._appeal_secondary_metrics_label)

        chart_row = QHBoxLayout()
        bar_box = QVBoxLayout()
        self._chart_view = QChartView()
        self._chart_view.setRenderHint(QPainter.Antialiasing)
        self._chart_view.setMinimumHeight(210)
        self._chart_view.setMaximumHeight(275)
        bar_box.addWidget(self._chart_view)
        self._bar_detail_label = self._make_chart_detail_label(
            "将鼠标移到柱子上，可在这里查看并保留具体数值。"
        )
        bar_box.addWidget(self._bar_detail_label)
        chart_row.addLayout(bar_box, stretch=3)

        self._chart_tabs = QTabWidget()
        self._chart_tabs.setMaximumHeight(320)
        share_tab = QWidget()
        share_box = QVBoxLayout(share_tab)
        share_box.setContentsMargins(4, 4, 4, 4)
        self._share_view = QChartView()
        self._share_view.setRenderHint(QPainter.Antialiasing)
        self._share_view.setMinimumHeight(185)
        share_box.addWidget(self._share_view)
        self._share_detail_label = self._make_chart_detail_label(
            "将鼠标移到扇区上，可在这里查看并保留具体占比。"
        )
        share_box.addWidget(self._share_detail_label)
        self._chart_tabs.addTab(share_tab, "申诉通过率")

        trend_tab = QWidget()
        trend_box = QVBoxLayout(trend_tab)
        trend_box.setContentsMargins(4, 4, 4, 4)
        trend_controls = QHBoxLayout()
        trend_controls.addWidget(QLabel("趋势范围："))
        self._trend_range_combo = QComboBox()
        self._trend_range_combo.addItems(["近 7 天", "近 30 天"])
        trend_controls.addWidget(self._trend_range_combo)
        trend_controls.addStretch(1)
        trend_box.addLayout(trend_controls)
        self._trend_view = QChartView()
        self._trend_view.setRenderHint(QPainter.Antialiasing)
        self._trend_view.setMinimumHeight(190)
        self._trend_view.setMaximumHeight(290)
        trend_box.addWidget(self._trend_view)
        self._chart_tabs.addTab(trend_tab, "申诉趋势")
        chart_row.addWidget(self._chart_tabs, stretch=2)
        appeal_root.addLayout(chart_row)

        table_toolbar = QHBoxLayout()
        self._result_count_label = QLabel("当前筛选结果：0 条")
        count_font = self._result_count_label.font()
        count_font.setBold(True)
        self._result_count_label.setFont(count_font)
        table_toolbar.addWidget(self._result_count_label)
        detail_button = QPushButton("查看所选订单详情")
        detail_button.clicked.connect(self._show_selected_detail)
        table_toolbar.addWidget(detail_button)
        table_toolbar.addStretch(1)
        export_excel_button = QPushButton("导出当前筛选结果（Excel）")
        export_excel_button.clicked.connect(self._export_excel)
        export_csv_button = QPushButton("导出当前筛选结果（CSV）")
        export_csv_button.clicked.connect(self._export_csv)
        table_toolbar.addWidget(export_excel_button)
        table_toolbar.addWidget(export_csv_button)
        appeal_root.addLayout(table_toolbar)

        self._model = OrderTableModel()
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QTableView.SelectRows)
        self._table.setSelectionMode(QTableView.SingleSelection)
        self._table.setEditTriggers(QTableView.NoEditTriggers)
        self._table.setMouseTracking(True)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self._table.verticalHeader().setVisible(False)
        self._table.doubleClicked.connect(self._on_row_double_clicked)
        appeal_root.addWidget(self._table, stretch=1)

        self._report_tabs.addTab(
            self._build_reconsideration_report_tab(), "申诉复议"
        )
        self._report_tabs.addTab(self._build_aftersale_report_tab(), "售后申诉")

        self._all_rows: list[dict] = []
        self._base_rows: list[dict] = []
        self._current_rows: list[dict] = []
        self._appeal_rows_loaded = False
        self._filter_shop: str | None = None
        self._bar_detail_status: str | None = None
        self._share_detail_status: str | None = None

        self._reload_shop_choices()
        self._shop_combo.currentIndexChanged.connect(self._reload)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        self._start_date.dateChanged.connect(self._apply_filters)
        self._end_date.dateChanged.connect(self._apply_filters)
        self._status_combo.currentIndexChanged.connect(self._apply_table_filter)
        self._trend_range_combo.currentIndexChanged.connect(self._update_trend)
        self._reload()

        self._refresh_reconsideration_report()
        self._refresh_aftersale_report()

        self._poller = DbPoller(fetch=self._fetch_rows, interval_ms=4000)
        self._poller.data_ready.connect(self._on_poll_data)

        self._reconsider_poller = DbPoller(
            fetch=self._fetch_reconsideration_rows, interval_ms=4000
        )
        self._reconsider_poller.data_ready.connect(self._on_reconsider_poll_data)

        self._aftersale_poller = DbPoller(
            fetch=self._fetch_aftersale_rows, interval_ms=4000
        )
        self._aftersale_poller.data_ready.connect(self._on_aftersale_poll_data)
        self._polling_active = False

    def set_polling_active(self, active: bool) -> None:
        """只在报表页面实际显示时轮询数据库。"""
        active = bool(active)
        if active == self._polling_active:
            return
        self._polling_active = active
        pollers = (
            self._poller,
            self._reconsider_poller,
            self._aftersale_poller,
        )
        if active:
            for poller in pollers:
                poller.start(immediate=True)
        else:
            for poller in pollers:
                poller.stop()
        memory_monitor.update_runtime_metrics(report_polling_active=int(active))

    def _build_reconsideration_report_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(0, 4, 0, 0)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("店铺："))
        self._reconsider_shop_combo = QComboBox()
        filter_row.addWidget(self._reconsider_shop_combo)
        filter_row.addWidget(QLabel("复议时间："))
        self._reconsider_range_combo = QComboBox()
        self._reconsider_range_combo.addItems(_DATE_RANGE_OPTIONS)
        filter_row.addWidget(self._reconsider_range_combo)
        self._reconsider_start_date = QDateEdit(QDate.currentDate().addDays(-30))
        self._reconsider_end_date = QDateEdit(QDate.currentDate())
        for editor in (self._reconsider_start_date, self._reconsider_end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setEnabled(False)
        self._reconsider_date_separator = QLabel("至")
        self._reconsider_date_separator.setEnabled(False)
        filter_row.addWidget(self._reconsider_start_date)
        filter_row.addWidget(self._reconsider_date_separator)
        filter_row.addWidget(self._reconsider_end_date)
        filter_row.addWidget(QLabel("复议状态："))
        self._reconsider_status_combo = QComboBox()
        self._reconsider_status_combo.addItem(_RECON_ALL_STATUSES, None)
        filter_row.addWidget(self._reconsider_status_combo)
        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self._refresh_reconsideration_report)
        filter_row.addWidget(refresh_button)
        self._reconsider_last_updated_label = QLabel("最后更新时间：--")
        self._reconsider_last_updated_label.setStyleSheet("color: #757575;")
        filter_row.addWidget(self._reconsider_last_updated_label)
        filter_row.addStretch(1)
        root.addLayout(filter_row)

        metrics_grid = QGridLayout()
        metric_names = (
            "复议通过率",
            "复议失败率",
            "复议成功",
            "复议失败",
            "待出结果",
            "成功返还金额",
        )
        self._reconsider_metric_cards = {
            name: _MetricCard(name) for name in metric_names
        }
        for index, card in enumerate(self._reconsider_metric_cards.values()):
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            metrics_grid.addWidget(card, 0, index)
        root.addLayout(metrics_grid)
        self._reconsider_secondary_metrics_label = QLabel()
        self._reconsider_secondary_metrics_label.setObjectName("statusChip")
        self._reconsider_secondary_metrics_label.setWordWrap(True)
        root.addWidget(self._reconsider_secondary_metrics_label)

        chart_row = QHBoxLayout()
        bar_box = QVBoxLayout()
        self._reconsider_chart_view = QChartView()
        self._reconsider_chart_view.setRenderHint(QPainter.Antialiasing)
        self._reconsider_chart_view.setMinimumHeight(210)
        self._reconsider_chart_view.setMaximumHeight(275)
        bar_box.addWidget(self._reconsider_chart_view)
        self._reconsider_bar_detail_label = self._make_chart_detail_label(
            "将鼠标移到柱子上，可在这里查看并保留具体数值。"
        )
        bar_box.addWidget(self._reconsider_bar_detail_label)
        chart_row.addLayout(bar_box, stretch=3)

        self._reconsider_chart_tabs = QTabWidget()
        self._reconsider_chart_tabs.setMaximumHeight(320)
        rate_tab = QWidget()
        rate_box = QVBoxLayout(rate_tab)
        rate_box.setContentsMargins(4, 4, 4, 4)
        self._reconsider_rate_view = QChartView()
        self._reconsider_rate_view.setRenderHint(QPainter.Antialiasing)
        self._reconsider_rate_view.setMinimumHeight(185)
        rate_box.addWidget(self._reconsider_rate_view)
        self._reconsider_rate_detail_label = self._make_chart_detail_label(
            "通过率只按已出结果记录计算：成功 ÷（成功＋失败）。"
        )
        rate_box.addWidget(self._reconsider_rate_detail_label)
        self._reconsider_chart_tabs.addTab(rate_tab, "复议通过率")

        trend_tab = QWidget()
        trend_box = QVBoxLayout(trend_tab)
        trend_box.setContentsMargins(4, 4, 4, 4)
        trend_controls = QHBoxLayout()
        trend_controls.addWidget(QLabel("趋势范围："))
        self._reconsider_trend_range_combo = QComboBox()
        self._reconsider_trend_range_combo.addItems(["近 7 天", "近 30 天"])
        trend_controls.addWidget(self._reconsider_trend_range_combo)
        trend_controls.addStretch(1)
        trend_box.addLayout(trend_controls)
        self._reconsider_trend_view = QChartView()
        self._reconsider_trend_view.setRenderHint(QPainter.Antialiasing)
        self._reconsider_trend_view.setMinimumHeight(190)
        self._reconsider_trend_view.setMaximumHeight(290)
        trend_box.addWidget(self._reconsider_trend_view)
        self._reconsider_chart_tabs.addTab(trend_tab, "复议趋势")
        chart_row.addWidget(self._reconsider_chart_tabs, stretch=2)
        root.addLayout(chart_row)

        table_toolbar = QHBoxLayout()
        self._reconsider_result_count_label = QLabel("当前筛选结果：0 条")
        count_font = self._reconsider_result_count_label.font()
        count_font.setBold(True)
        self._reconsider_result_count_label.setFont(count_font)
        table_toolbar.addWidget(self._reconsider_result_count_label)
        detail_button = QPushButton("查看所选复议详情")
        detail_button.clicked.connect(self._show_selected_reconsideration_detail)
        table_toolbar.addWidget(detail_button)
        table_toolbar.addStretch(1)
        export_excel_button = QPushButton("导出当前筛选结果（Excel）")
        export_excel_button.clicked.connect(self._export_reconsideration_excel)
        table_toolbar.addWidget(export_excel_button)
        export_csv_button = QPushButton("导出当前筛选结果（CSV）")
        export_csv_button.clicked.connect(self._export_reconsideration_csv)
        table_toolbar.addWidget(export_csv_button)
        root.addLayout(table_toolbar)

        self._reconsider_model = ReconsiderationTableModel()
        self._reconsider_table = QTableView()
        self._reconsider_table.setModel(self._reconsider_model)
        self._reconsider_table.setSelectionBehavior(QTableView.SelectRows)
        self._reconsider_table.setSelectionMode(QTableView.SingleSelection)
        self._reconsider_table.setEditTriggers(QTableView.NoEditTriggers)
        self._reconsider_table.setMouseTracking(True)
        self._reconsider_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeToContents
        )
        self._reconsider_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.Stretch
        )
        self._reconsider_table.verticalHeader().setVisible(False)
        self._reconsider_table.doubleClicked.connect(
            self._on_reconsideration_row_double_clicked
        )
        root.addWidget(self._reconsider_table, stretch=1)

        self._all_reconsider_rows: list[dict] = []
        self._base_reconsider_rows: list[dict] = []
        self._current_reconsider_rows: list[dict] = []
        self._reconsider_rows_loaded = False
        self._reconsider_filter_shop: str | None = None
        self._reconsider_bar_detail_status: str | None = None
        self._reconsider_rate_detail_status: str | None = None

        self._reconsider_shop_combo.currentIndexChanged.connect(
            self._reload_reconsideration
        )
        self._reconsider_range_combo.currentIndexChanged.connect(
            self._on_reconsider_range_changed
        )
        self._reconsider_start_date.dateChanged.connect(
            self._apply_reconsider_filters
        )
        self._reconsider_end_date.dateChanged.connect(
            self._apply_reconsider_filters
        )
        self._reconsider_status_combo.currentIndexChanged.connect(
            self._apply_reconsider_table_filter
        )
        self._reconsider_trend_range_combo.currentIndexChanged.connect(
            self._update_reconsider_trend
        )
        return tab

    def _reload_reconsider_shop_choices(self) -> None:
        selected = self._reconsider_shop_combo.currentText()
        shops = sorted(
            {row["shop"] for row in db.list_reconsideration_report_rows()}
        )
        self._reconsider_shop_combo.blockSignals(True)
        self._reconsider_shop_combo.clear()
        self._reconsider_shop_combo.addItem("全部")
        self._reconsider_shop_combo.addItems(shops)
        index = self._reconsider_shop_combo.findText(selected)
        self._reconsider_shop_combo.setCurrentIndex(max(0, index))
        self._reconsider_shop_combo.blockSignals(False)

    def _refresh_reconsideration_report(self) -> None:
        self._reload_reconsider_shop_choices()
        self._reload_reconsideration()

    def _selected_reconsider_shop(self) -> str | None:
        text = self._reconsider_shop_combo.currentText()
        return None if text in ("", "全部") else text

    def _reload_reconsideration(self) -> None:
        self._reconsider_filter_shop = self._selected_reconsider_shop()
        self._on_reconsider_poll_data(self._fetch_reconsideration_rows())

    def _fetch_reconsideration_rows(self) -> list[dict]:
        return db.list_reconsideration_report_rows(self._reconsider_filter_shop)

    def _on_reconsider_range_changed(self) -> None:
        custom = self._reconsider_range_combo.currentText() == "指定日期"
        for widget in (
            self._reconsider_start_date,
            self._reconsider_date_separator,
            self._reconsider_end_date,
        ):
            widget.setEnabled(custom)
        self._apply_reconsider_filters()

    def _reconsider_date_bounds(self) -> tuple[datetime | None, datetime | None]:
        choice = self._reconsider_range_combo.currentText()
        local_now = datetime.now().astimezone()
        if choice == "全部":
            return None, None
        if choice == "今天":
            start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start.astimezone(timezone.utc), None
        if choice == "近 7 天":
            return datetime.now(timezone.utc) - timedelta(days=7), None
        if choice == "近 30 天":
            return datetime.now(timezone.utc) - timedelta(days=30), None

        start_date = self._reconsider_start_date.date()
        end_date = self._reconsider_end_date.date()
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        start = datetime(
            start_date.year(), start_date.month(), start_date.day()
        ).astimezone()
        end = datetime(
            end_date.year(), end_date.month(), end_date.day(), 23, 59, 59, 999999
        ).astimezone()
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _apply_reconsider_filters(self) -> None:
        start, end = self._reconsider_date_bounds()
        rows: list[dict] = []
        for row in self._all_reconsider_rows:
            timestamp = _reconsider_timestamp(row)
            if timestamp is not None:
                timestamp = timestamp.astimezone(timezone.utc)
            if start is not None and (timestamp is None or timestamp < start):
                continue
            if end is not None and (timestamp is None or timestamp > end):
                continue
            rows.append(row)
        self._base_reconsider_rows = rows
        self._refresh_reconsider_status_choices()
        self._update_reconsider_metrics()
        _replace_chart(
            self._reconsider_chart_view,
            _build_reconsider_status_chart(
                rows,
                self._on_reconsider_chart_clicked,
                self._on_reconsider_chart_hovered,
            ),
        )
        _replace_chart(
            self._reconsider_rate_view,
            _build_reconsider_rate_chart(
                rows,
                self._on_reconsider_rate_clicked,
                self._on_reconsider_rate_hovered,
            ),
        )
        self._refresh_reconsider_chart_details()
        self._apply_reconsider_table_filter()

    def _refresh_reconsider_status_choices(self) -> None:
        selected = self._reconsider_status_combo.currentData()
        available = {
            _reconsider_status_bucket(row.get("status"))
            for row in self._base_reconsider_rows
        }
        statuses = [
            status
            for status in (
                "待处理",
                "审核中",
                "结果待确认",
                "复议成功",
                "复议失败",
            )
            if status in available
        ]
        self._reconsider_status_combo.blockSignals(True)
        self._reconsider_status_combo.clear()
        self._reconsider_status_combo.addItem(_RECON_ALL_STATUSES, None)
        for status in statuses:
            self._reconsider_status_combo.addItem(status, status)
        index = self._reconsider_status_combo.findData(selected)
        self._reconsider_status_combo.setCurrentIndex(max(0, index))
        self._reconsider_status_combo.blockSignals(False)

    def _apply_reconsider_table_filter(self) -> None:
        status = self._reconsider_status_combo.currentData()
        if status is None:
            rows = list(self._base_reconsider_rows)
        else:
            rows = [
                row
                for row in self._base_reconsider_rows
                if _reconsider_status_bucket(row.get("status")) == status
            ]
        self._current_reconsider_rows = rows
        self._reconsider_model.set_rows(rows)
        self._reconsider_result_count_label.setText(
            f"当前筛选结果：{len(rows)} 条复议记录"
        )

    def _on_reconsider_chart_clicked(self, index: int) -> None:
        statuses = getattr(self._reconsider_chart_view.chart(), "statuses", [])
        if 0 <= index < len(statuses):
            self._select_reconsider_status(statuses[index])

    def _on_reconsider_chart_hovered(self, state: bool, index: int) -> None:
        if not state:
            return
        statuses = getattr(self._reconsider_chart_view.chart(), "statuses", [])
        if not (0 <= index < len(statuses)):
            return
        self._reconsider_bar_detail_status = statuses[index]
        self._reconsider_bar_detail_label.setText(
            self._reconsider_status_detail_text(statuses[index])
        )

    def _on_reconsider_rate_clicked(self, status: str) -> None:
        self._select_reconsider_status(status)

    def _on_reconsider_rate_hovered(self, state: bool, status: str) -> None:
        if state:
            self._reconsider_rate_detail_status = status
            self._reconsider_rate_detail_label.setText(
                self._reconsider_status_detail_text(status)
            )

    def _reconsider_status_detail_text(self, status: str) -> str:
        total = len(self._base_reconsider_rows)
        count = sum(
            1
            for row in self._base_reconsider_rows
            if _reconsider_status_bucket(row.get("status")) == status
        )
        if total <= 0:
            return f"{status}　暂无复议记录"
        metrics = _reconsider_metrics(self._base_reconsider_rows)
        result_total = int(metrics["复议成功"]) + int(metrics["复议失败"])
        parts = [
            status,
            f"复议记录 {count} 条",
            f"占全部 {count / total:.1%}",
        ]
        if result_total and status == "复议成功":
            parts.append(
                f"复议通过率 {int(metrics['复议成功']) / result_total:.1%}"
            )
        elif result_total and status == "复议失败":
            parts.append(
                f"已出结果失败率 {int(metrics['复议失败']) / result_total:.1%}"
            )
        return "　｜　".join(parts)

    def _refresh_reconsider_chart_details(self) -> None:
        available = {
            _reconsider_status_bucket(row.get("status"))
            for row in self._base_reconsider_rows
        }
        if self._reconsider_bar_detail_status in available:
            self._reconsider_bar_detail_label.setText(
                self._reconsider_status_detail_text(
                    self._reconsider_bar_detail_status
                )
            )
        elif self._reconsider_bar_detail_status is not None:
            self._reconsider_bar_detail_status = None
            self._reconsider_bar_detail_label.setText(
                "当前筛选范围内已没有上次查看的复议状态。"
            )
        if self._reconsider_rate_detail_status in available:
            self._reconsider_rate_detail_label.setText(
                self._reconsider_status_detail_text(
                    self._reconsider_rate_detail_status
                )
            )
        elif self._reconsider_rate_detail_status is not None:
            self._reconsider_rate_detail_status = None
            self._reconsider_rate_detail_label.setText(
                "通过率只按已出结果记录计算：成功 ÷（成功＋失败）。"
            )

    def _select_reconsider_status(self, status: str) -> None:
        index = self._reconsider_status_combo.findData(status)
        if index >= 0:
            self._reconsider_status_combo.setCurrentIndex(index)

    def _update_reconsider_metrics(self) -> None:
        metrics = _reconsider_metrics(self._base_reconsider_rows)
        for name, card in self._reconsider_metric_cards.items():
            card.set_value(metrics[name])
        self._reconsider_secondary_metrics_label.setText(
            f"已提交 {metrics['已提交']} 条　｜　"
            f"已出结果 {metrics['已出结果']} 条　｜　"
            f"结果完成率 {metrics['结果完成率']}　｜　"
            f"平台待处理 {metrics['待处理']} 条　｜　"
            f"审核中 {metrics['审核中']} 条　｜　"
            f"结果待确认 {metrics['结果待确认']} 条　｜　"
            f"待出结果金额 {metrics['待出结果金额']}　｜　"
            f"总复议金额 {metrics['总复议金额']}"
        )

    def _update_reconsider_trend(self) -> None:
        days = 7 if self._reconsider_trend_range_combo.currentIndex() == 0 else 30
        _replace_chart(
            self._reconsider_trend_view,
            _build_reconsider_trend_chart(self._all_reconsider_rows, days),
        )

    def _on_reconsider_poll_data(self, rows: list[dict]) -> None:
        if self._reconsider_rows_loaded and rows == self._all_reconsider_rows:
            memory_monitor.increment_runtime_metric("report_refreshes_skipped")
            self._reconsider_last_updated_label.setText(
                f"最后更新时间：{datetime.now():%H:%M:%S}"
            )
            return
        self._reconsider_rows_loaded = True
        self._all_reconsider_rows = rows
        self._apply_reconsider_filters()
        self._update_reconsider_trend()
        self._reconsider_last_updated_label.setText(
            f"最后更新时间：{datetime.now():%H:%M:%S}"
        )

    def _selected_reconsideration_row(self) -> dict | None:
        selection = self._reconsider_table.selectionModel().selectedRows()
        if not selection:
            msgbox.information(
                self, "请选择复议记录", "请先在明细表中选择一条复议记录。"
            )
            return None
        return self._reconsider_model.row_at(selection[0])

    def _show_selected_reconsideration_detail(self) -> None:
        row = self._selected_reconsideration_row()
        if row is not None:
            _ReconsiderationDetailDialog(row, self).exec_()

    def _on_reconsideration_row_double_clicked(self, index: QModelIndex) -> None:
        row = self._reconsider_model.row_at(index)
        if row is not None:
            _ReconsiderationDetailDialog(row, self).exec_()

    def _export_reconsideration_excel(self) -> None:
        self._export_reconsideration("Excel")

    def _export_reconsideration_csv(self) -> None:
        self._export_reconsideration("CSV")

    def _export_reconsideration(self, kind: str) -> None:
        if not self._current_reconsider_rows:
            msgbox.information(
                self, "没有数据", "当前筛选条件下没有可导出的复议记录。"
            )
            return
        suffix = "xlsx" if kind == "Excel" else "csv"
        path, _ = QFileDialog.getSaveFileName(
            self,
            f"导出当前筛选复议结果（{kind}）",
            f"当前筛选复议明细.{suffix}",
            f"{kind} 文件 (*.{suffix})",
        )
        if not path:
            return
        try:
            if kind == "Excel":
                export_service.export_reconsiderations_to_excel(
                    self._current_reconsider_rows, path
                )
            else:
                export_service.export_reconsiderations_to_csv(
                    self._current_reconsider_rows, path
                )
        except Exception as exc:
            msgbox.critical(self, "导出失败", str(exc))
            return
        msgbox.information(
            self,
            "导出完成",
            f"已导出当前筛选复议记录 {len(self._current_reconsider_rows)} 条：\n{path}",
        )

    def _build_aftersale_report_tab(self) -> QWidget:
        tab = QWidget()
        root = QVBoxLayout(tab)
        root.setContentsMargins(0, 4, 0, 0)
        filters = QHBoxLayout()
        filters.addWidget(QLabel("店铺："))
        self._aftersale_shop_combo = QComboBox()
        filters.addWidget(self._aftersale_shop_combo)
        filters.addWidget(QLabel("申诉时间："))
        self._aftersale_range_combo = QComboBox()
        self._aftersale_range_combo.addItems(_DATE_RANGE_OPTIONS)
        filters.addWidget(self._aftersale_range_combo)
        self._aftersale_start_date = QDateEdit(QDate.currentDate().addDays(-30))
        self._aftersale_end_date = QDateEdit(QDate.currentDate())
        for editor in (self._aftersale_start_date, self._aftersale_end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
            editor.setEnabled(False)
        self._aftersale_date_separator = QLabel("至")
        self._aftersale_date_separator.setEnabled(False)
        filters.addWidget(self._aftersale_start_date)
        filters.addWidget(self._aftersale_date_separator)
        filters.addWidget(self._aftersale_end_date)
        filters.addWidget(QLabel("审核状态："))
        self._aftersale_status_combo = QComboBox()
        self._aftersale_status_combo.addItem(_AFTERSALE_ALL_STATUSES, None)
        filters.addWidget(self._aftersale_status_combo)
        filters.addWidget(QLabel("申诉项："))
        self._aftersale_item_combo = QComboBox()
        self._aftersale_item_combo.addItem("全部申诉项", None)
        filters.addWidget(self._aftersale_item_combo)
        refresh = QPushButton("刷新")
        refresh.clicked.connect(self._refresh_aftersale_report)
        filters.addWidget(refresh)
        self._aftersale_last_updated_label = QLabel("最后更新时间：--")
        self._aftersale_last_updated_label.setStyleSheet("color: #757575;")
        filters.addWidget(self._aftersale_last_updated_label)
        filters.addStretch(1)
        root.addLayout(filters)

        metric_names = (
            "售后通过率",
            "申诉总数",
            "待出结果",
            "申诉成功",
            "部分通过",
            "申诉失败",
            "申诉总金额",
        )
        metrics = QHBoxLayout()
        self._aftersale_metric_cards = {name: _MetricCard(name) for name in metric_names}
        for card in self._aftersale_metric_cards.values():
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            metrics.addWidget(card)
        root.addLayout(metrics)
        self._aftersale_secondary_metrics_label = QLabel()
        self._aftersale_secondary_metrics_label.setObjectName("statusChip")
        self._aftersale_secondary_metrics_label.setWordWrap(True)
        root.addWidget(self._aftersale_secondary_metrics_label)

        chart_row = QHBoxLayout()
        bar_box = QVBoxLayout()
        self._aftersale_chart_view = QChartView()
        self._aftersale_chart_view.setRenderHint(QPainter.Antialiasing)
        self._aftersale_chart_view.setMinimumHeight(210)
        self._aftersale_chart_view.setMaximumHeight(275)
        bar_box.addWidget(self._aftersale_chart_view)
        self._aftersale_bar_detail_label = self._make_chart_detail_label(
            "将鼠标移到柱子上，可在这里查看并保留具体数值。"
        )
        bar_box.addWidget(self._aftersale_bar_detail_label)
        chart_row.addLayout(bar_box, stretch=3)

        self._aftersale_chart_tabs = QTabWidget()
        self._aftersale_chart_tabs.setMaximumHeight(320)
        rate_tab = QWidget()
        rate_box = QVBoxLayout(rate_tab)
        rate_box.setContentsMargins(4, 4, 4, 4)
        self._aftersale_rate_view = QChartView()
        self._aftersale_rate_view.setRenderHint(QPainter.Antialiasing)
        self._aftersale_rate_view.setMinimumHeight(185)
        rate_box.addWidget(self._aftersale_rate_view)
        self._aftersale_rate_detail_label = self._make_chart_detail_label(
            "通过率按已出结果计算：（申诉成功＋部分通过）÷全部已出结果。"
        )
        rate_box.addWidget(self._aftersale_rate_detail_label)
        self._aftersale_chart_tabs.addTab(rate_tab, "售后通过率")

        trend_tab = QWidget()
        trend_box = QVBoxLayout(trend_tab)
        trend_box.setContentsMargins(4, 4, 4, 4)
        trend_controls = QHBoxLayout()
        trend_controls.addWidget(QLabel("趋势范围："))
        self._aftersale_trend_range_combo = QComboBox()
        self._aftersale_trend_range_combo.addItems(["近 7 天", "近 30 天"])
        trend_controls.addWidget(self._aftersale_trend_range_combo)
        trend_controls.addStretch(1)
        trend_box.addLayout(trend_controls)
        self._aftersale_trend_view = QChartView()
        self._aftersale_trend_view.setRenderHint(QPainter.Antialiasing)
        self._aftersale_trend_view.setMinimumHeight(190)
        self._aftersale_trend_view.setMaximumHeight(290)
        trend_box.addWidget(self._aftersale_trend_view)
        self._aftersale_chart_tabs.addTab(trend_tab, "申诉趋势")
        chart_row.addWidget(self._aftersale_chart_tabs, stretch=2)
        root.addLayout(chart_row)

        toolbar = QHBoxLayout()
        self._aftersale_result_count_label = QLabel("当前筛选结果：0 条")
        font = self._aftersale_result_count_label.font()
        font.setBold(True)
        self._aftersale_result_count_label.setFont(font)
        toolbar.addWidget(self._aftersale_result_count_label)
        detail = QPushButton("查看所选售后详情")
        detail.clicked.connect(self._show_selected_aftersale_detail)
        toolbar.addWidget(detail)
        toolbar.addStretch(1)
        excel = QPushButton("导出当前筛选结果（Excel）")
        excel.clicked.connect(lambda: self._export_aftersale("Excel"))
        csv_button = QPushButton("导出当前筛选结果（CSV）")
        csv_button.clicked.connect(lambda: self._export_aftersale("CSV"))
        toolbar.addWidget(excel)
        toolbar.addWidget(csv_button)
        root.addLayout(toolbar)

        self._aftersale_model = AftersaleTableModel()
        self._aftersale_table = QTableView()
        self._aftersale_table.setModel(self._aftersale_model)
        self._aftersale_table.setSelectionBehavior(QTableView.SelectRows)
        self._aftersale_table.setSelectionMode(QTableView.SingleSelection)
        self._aftersale_table.setEditTriggers(QTableView.NoEditTriggers)
        self._aftersale_table.setMouseTracking(True)
        self._aftersale_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self._aftersale_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self._aftersale_table.verticalHeader().setVisible(False)
        self._aftersale_table.doubleClicked.connect(self._on_aftersale_row_double_clicked)
        root.addWidget(self._aftersale_table, stretch=1)

        self._all_aftersale_rows: list[dict] = []
        self._base_aftersale_rows: list[dict] = []
        self._current_aftersale_rows: list[dict] = []
        self._aftersale_rows_loaded = False
        self._aftersale_filter_shop: str | None = None
        self._aftersale_bar_detail_status: str | None = None
        self._aftersale_rate_detail_status: str | None = None
        self._aftersale_shop_combo.currentIndexChanged.connect(self._reload_aftersale)
        self._aftersale_range_combo.currentIndexChanged.connect(self._on_aftersale_range_changed)
        self._aftersale_start_date.dateChanged.connect(self._apply_aftersale_filters)
        self._aftersale_end_date.dateChanged.connect(self._apply_aftersale_filters)
        self._aftersale_status_combo.currentIndexChanged.connect(self._apply_aftersale_table_filter)
        self._aftersale_item_combo.currentIndexChanged.connect(self._apply_aftersale_table_filter)
        self._aftersale_trend_range_combo.currentIndexChanged.connect(
            self._update_aftersale_trend
        )
        return tab

    def _refresh_aftersale_report(self) -> None:
        selected = self._aftersale_shop_combo.currentText()
        shops = sorted(
            {
                str(row.get("shop") or "")
                for row in _visible_aftersale_rows(db.list_aftersale_appeal_records())
                if row.get("shop")
            }
        )
        self._aftersale_shop_combo.blockSignals(True)
        self._aftersale_shop_combo.clear()
        self._aftersale_shop_combo.addItem("全部")
        self._aftersale_shop_combo.addItems(shops)
        self._aftersale_shop_combo.setCurrentIndex(max(0, self._aftersale_shop_combo.findText(selected)))
        self._aftersale_shop_combo.blockSignals(False)
        self._reload_aftersale()

    def _reload_aftersale(self) -> None:
        text = self._aftersale_shop_combo.currentText()
        self._aftersale_filter_shop = None if text in ("", "全部") else text
        self._on_aftersale_poll_data(self._fetch_aftersale_rows())

    def _fetch_aftersale_rows(self) -> list[dict]:
        return _visible_aftersale_rows(
            db.list_aftersale_appeal_records(self._aftersale_filter_shop)
        )

    def _on_aftersale_range_changed(self) -> None:
        custom = self._aftersale_range_combo.currentText() == "指定日期"
        for widget in (self._aftersale_start_date, self._aftersale_date_separator, self._aftersale_end_date):
            widget.setEnabled(custom)
        self._apply_aftersale_filters()

    def _aftersale_date_bounds(self) -> tuple[datetime | None, datetime | None]:
        choice = self._aftersale_range_combo.currentText()
        now = datetime.now().astimezone()
        if choice == "全部":
            return None, None
        if choice == "今天":
            return now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc), None
        if choice in ("近 7 天", "近 30 天"):
            return datetime.now(timezone.utc) - timedelta(days=7 if choice == "近 7 天" else 30), None
        start_date, end_date = self._aftersale_start_date.date(), self._aftersale_end_date.date()
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        start = datetime(start_date.year(), start_date.month(), start_date.day()).astimezone()
        end = datetime(end_date.year(), end_date.month(), end_date.day(), 23, 59, 59, 999999).astimezone()
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _apply_aftersale_filters(self) -> None:
        start, end = self._aftersale_date_bounds()
        rows = []
        for row in self._all_aftersale_rows:
            timestamp = _aftersale_timestamp(row)
            if start is not None and (timestamp is None or timestamp < start):
                continue
            if end is not None and (timestamp is None or timestamp > end):
                continue
            rows.append(row)
        self._base_aftersale_rows = rows
        selected_status = self._aftersale_status_combo.currentData()
        selected_item = self._aftersale_item_combo.currentData()
        statuses = sorted({str(row.get("status") or "未知") for row in rows}, key=lambda value: (_AFTERSALE_STATUS_ORDER.index(value) if value in _AFTERSALE_STATUS_ORDER else 99, value))
        items = sorted({str(row.get("appeal_item") or row.get("classification") or "") for row in rows if row.get("appeal_item") or row.get("classification")})
        for combo, title, values, selected in ((self._aftersale_status_combo, _AFTERSALE_ALL_STATUSES, statuses, selected_status), (self._aftersale_item_combo, "全部申诉项", items, selected_item)):
            combo.blockSignals(True)
            combo.clear()
            combo.addItem(title, None)
            for value in values:
                combo.addItem(value, value)
            combo.setCurrentIndex(max(0, combo.findData(selected)))
            combo.blockSignals(False)
        self._update_aftersale_metrics()
        _replace_chart(
            self._aftersale_chart_view,
            _build_aftersale_status_chart(
                rows,
                self._on_aftersale_chart_clicked,
                self._on_aftersale_chart_hovered,
            ),
        )
        _replace_chart(
            self._aftersale_rate_view,
            _build_aftersale_rate_chart(
                rows,
                self._on_aftersale_rate_clicked,
                self._on_aftersale_rate_hovered,
            ),
        )
        self._refresh_aftersale_chart_details()
        self._apply_aftersale_table_filter()

    def _apply_aftersale_table_filter(self) -> None:
        status, item = self._aftersale_status_combo.currentData(), self._aftersale_item_combo.currentData()
        rows = [row for row in self._base_aftersale_rows if (status is None or str(row.get("status") or "未知") == status) and (item is None or str(row.get("appeal_item") or row.get("classification") or "") == item)]
        self._current_aftersale_rows = rows
        self._aftersale_model.set_rows(rows)
        self._aftersale_result_count_label.setText(f"当前筛选结果：{len(rows)} 条售后申诉记录")

    def _on_aftersale_chart_clicked(self, index: int) -> None:
        statuses = getattr(self._aftersale_chart_view.chart(), "statuses", [])
        if 0 <= index < len(statuses):
            self._select_aftersale_status(statuses[index])

    def _on_aftersale_chart_hovered(self, state: bool, index: int) -> None:
        if not state:
            return
        statuses = getattr(self._aftersale_chart_view.chart(), "statuses", [])
        if not (0 <= index < len(statuses)):
            return
        self._aftersale_bar_detail_status = statuses[index]
        self._aftersale_bar_detail_label.setText(
            self._aftersale_status_detail_text(statuses[index])
        )

    def _on_aftersale_rate_clicked(self, status: str) -> None:
        self._select_aftersale_status(status)

    def _on_aftersale_rate_hovered(self, state: bool, status: str) -> None:
        if state:
            self._aftersale_rate_detail_status = status
            self._aftersale_rate_detail_label.setText(
                self._aftersale_status_detail_text(status)
            )

    def _aftersale_status_detail_text(self, status: str) -> str:
        total = len(self._base_aftersale_rows)
        count = sum(
            1 for row in self._base_aftersale_rows if row.get("status") == status
        )
        if total <= 0:
            return f"{status}　暂无售后申诉记录"
        metrics = _aftersale_metrics(self._base_aftersale_rows)
        result_total = int(metrics["已出结果"])
        parts = [
            status,
            f"售后申诉记录 {count} 条",
            f"占全部 {count / total:.1%}",
        ]
        if result_total and status in ("申诉成功", "部分通过"):
            parts.append(f"售后通过率 {metrics['售后通过率']}")
        elif result_total and status == "申诉失败":
            parts.append(f"已出结果失败占比 {count / result_total:.1%}")
        return "　｜　".join(parts)

    def _refresh_aftersale_chart_details(self) -> None:
        available = {
            str(row.get("status") or "未知") for row in self._base_aftersale_rows
        }
        if self._aftersale_bar_detail_status in available:
            self._aftersale_bar_detail_label.setText(
                self._aftersale_status_detail_text(self._aftersale_bar_detail_status)
            )
        elif self._aftersale_bar_detail_status is not None:
            self._aftersale_bar_detail_status = None
            self._aftersale_bar_detail_label.setText(
                "当前筛选范围内已没有上次查看的审核状态。"
            )
        if self._aftersale_rate_detail_status in available:
            self._aftersale_rate_detail_label.setText(
                self._aftersale_status_detail_text(self._aftersale_rate_detail_status)
            )
        elif self._aftersale_rate_detail_status is not None:
            self._aftersale_rate_detail_status = None
            self._aftersale_rate_detail_label.setText(
                "通过率按已出结果计算：（申诉成功＋部分通过）÷全部已出结果。"
            )

    def _select_aftersale_status(self, status: str) -> None:
        index = self._aftersale_status_combo.findData(status)
        if index >= 0:
            self._aftersale_status_combo.setCurrentIndex(index)

    def _update_aftersale_metrics(self) -> None:
        values = _aftersale_metrics(self._base_aftersale_rows)
        for name, card in self._aftersale_metric_cards.items():
            card.set_value(values[name])
        self._aftersale_secondary_metrics_label.setText(
            f"通过率按已出结果计算：（申诉成功＋部分通过）÷ "
            f"（申诉成功＋部分通过＋申诉失败），共 {values['已出结果']} 条　｜　"
            f"成功申诉金额 {values['成功申诉金额']}　｜　"
            f"部分通过返还金额 {values['部分通过返还金额']}　｜　"
            f"已撤回 {values['已撤回']} 条　｜　"
            "部分通过取详情页返还金额，其余取申诉列表金额；空白金额待查询"
        )

    def _on_aftersale_poll_data(self, rows: list[dict]) -> None:
        visible_rows = _visible_aftersale_rows(rows)
        if self._aftersale_rows_loaded and visible_rows == self._all_aftersale_rows:
            memory_monitor.increment_runtime_metric("report_refreshes_skipped")
            self._aftersale_last_updated_label.setText(
                f"最后更新时间：{datetime.now():%H:%M:%S}"
            )
            return
        self._aftersale_rows_loaded = True
        self._all_aftersale_rows = visible_rows
        self._apply_aftersale_filters()
        self._update_aftersale_trend()
        self._aftersale_last_updated_label.setText(f"最后更新时间：{datetime.now():%H:%M:%S}")

    def _update_aftersale_trend(self) -> None:
        days = 7 if self._aftersale_trend_range_combo.currentIndex() == 0 else 30
        _replace_chart(
            self._aftersale_trend_view,
            _build_aftersale_trend_chart(self._all_aftersale_rows, days),
        )

    def _selected_aftersale_row(self) -> dict | None:
        selection = self._aftersale_table.selectionModel().selectedRows()
        if not selection:
            msgbox.information(self, "请选择售后记录", "请先在明细表中选择一条售后申诉记录。")
            return None
        return self._aftersale_model.row_at(selection[0])

    def _show_selected_aftersale_detail(self) -> None:
        row = self._selected_aftersale_row()
        if row is not None:
            _AftersaleDetailDialog(row, self).exec_()

    def _on_aftersale_row_double_clicked(self, index: QModelIndex) -> None:
        row = self._aftersale_model.row_at(index)
        if row is not None:
            _AftersaleDetailDialog(row, self).exec_()

    def _export_aftersale(self, kind: str) -> None:
        if not self._current_aftersale_rows:
            msgbox.information(self, "没有数据", "当前筛选条件下没有可导出的售后申诉记录。")
            return
        suffix = "xlsx" if kind == "Excel" else "csv"
        path, _ = QFileDialog.getSaveFileName(self, f"导出售后申诉结果（{kind}）", f"当前筛选售后申诉明细.{suffix}", f"{kind} 文件 (*.{suffix})")
        if not path:
            return
        try:
            if kind == "Excel":
                export_service.export_aftersale_appeals_to_excel(self._current_aftersale_rows, path)
            else:
                export_service.export_aftersale_appeals_to_csv(self._current_aftersale_rows, path)
        except Exception as exc:
            msgbox.critical(self, "导出失败", str(exc))
            return
        msgbox.information(self, "导出完成", f"已导出售后申诉记录 {len(self._current_aftersale_rows)} 条：\n{path}")

    def _reload_shop_choices(self) -> None:
        shops = sorted({row["shop"] for row in db.list_appeal_report_rows()})
        self._shop_combo.blockSignals(True)
        self._shop_combo.clear()
        self._shop_combo.addItem("全部")
        self._shop_combo.addItems(shops)
        self._shop_combo.blockSignals(False)

    @staticmethod
    def _make_chart_detail_label(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setMinimumHeight(38)
        label.setStyleSheet(
            "background: #f6f8fa; border: 1px solid #dfe3e8; border-radius: 5px; "
            "padding: 5px 8px; color: #263238;"
        )
        return label

    def _selected_shop(self) -> str | None:
        text = self._shop_combo.currentText()
        return None if text == "全部" else text

    def _reload(self) -> None:
        self._filter_shop = self._selected_shop()
        self._on_poll_data(self._fetch_rows())

    def _fetch_rows(self) -> list[dict]:
        return db.list_appeal_report_rows(self._filter_shop)

    def _on_range_changed(self) -> None:
        custom = self._range_combo.currentText() == "指定日期"
        for widget in (self._start_date, self._date_separator, self._end_date):
            widget.setEnabled(custom)
        self._apply_filters()

    def _date_bounds(self) -> tuple[datetime | None, datetime | None]:
        choice = self._range_combo.currentText()
        local_now = datetime.now().astimezone()
        if choice == "全部":
            return None, None
        if choice == "今天":
            start = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
            return start.astimezone(timezone.utc), None
        if choice == "近 7 天":
            return datetime.now(timezone.utc) - timedelta(days=7), None
        if choice == "近 30 天":
            return datetime.now(timezone.utc) - timedelta(days=30), None

        start_date = self._start_date.date()
        end_date = self._end_date.date()
        if start_date > end_date:
            start_date, end_date = end_date, start_date
        start = datetime(start_date.year(), start_date.month(), start_date.day()).astimezone()
        end = datetime(
            end_date.year(), end_date.month(), end_date.day(), 23, 59, 59, 999999
        ).astimezone()
        return start.astimezone(timezone.utc), end.astimezone(timezone.utc)

    def _apply_filters(self) -> None:
        start, end = self._date_bounds()
        rows: list[dict] = []
        for row in self._all_rows:
            timestamp = _parse_timestamp(row.get("submitted_at"))
            if start is not None and (timestamp is None or timestamp < start):
                continue
            if end is not None and (timestamp is None or timestamp > end):
                continue
            rows.append(row)
        self._base_rows = rows
        self._refresh_status_choices()
        self._update_metrics()
        _replace_chart(
            self._chart_view,
            _build_status_chart(rows, self._on_chart_clicked, self._on_chart_hovered),
        )
        _replace_chart(
            self._share_view,
            _build_share_chart(rows, self._on_share_clicked, self._on_share_hovered),
        )
        self._refresh_persistent_chart_details()
        self._apply_table_filter()

    def _refresh_status_choices(self) -> None:
        selected = self._status_combo.currentData()
        available = {
            _status_bucket(row.get("status")) for row in self._base_rows
        }
        statuses = [
            status
            for status in ("待出结果", "结果待确认", "申诉通过", "申诉失败")
            if status in available
        ]
        self._status_combo.blockSignals(True)
        self._status_combo.clear()
        self._status_combo.addItem(_ALL_STATUSES, None)
        for status in statuses:
            self._status_combo.addItem(status, status)
        index = self._status_combo.findData(selected)
        self._status_combo.setCurrentIndex(max(0, index))
        self._status_combo.blockSignals(False)

    def _apply_table_filter(self) -> None:
        status = self._status_combo.currentData()
        if status is None:
            rows = list(self._base_rows)
        else:
            rows = [
                row
                for row in self._base_rows
                if _status_bucket(row.get("status")) == status
            ]
        self._current_rows = rows
        self._model.set_rows(rows)
        self._result_count_label.setText(f"当前筛选结果：{len(rows)} 条")

    def _on_chart_clicked(self, index: int) -> None:
        chart = self._chart_view.chart()
        statuses = getattr(chart, "statuses", [])
        if 0 <= index < len(statuses):
            self._select_status(statuses[index])

    def _on_chart_hovered(self, state: bool, index: int) -> None:
        if not state:
            return
        chart = self._chart_view.chart()
        statuses = getattr(chart, "statuses", [])
        if not (0 <= index < len(statuses)):
            return
        self._bar_detail_status = statuses[index]
        self._bar_detail_label.setText(self._status_detail_text(self._bar_detail_status))

    def _status_detail_text(self, status: str) -> str:
        total = len(self._base_rows)
        count = sum(
            1
            for row in self._base_rows
            if _status_bucket(row.get("status")) == status
        )
        if total <= 0:
            return f"{status}　暂无申诉记录"
        parts = [
            status,
            f"申诉记录 {count} 条",
            f"占全部 {count / total:.1%}",
        ]
        metrics = _status_metrics(self._base_rows)
        if status == "申诉通过":
            parts.append(f"申诉通过率 {metrics['申诉通过率']}")
        elif status == "申诉失败":
            parts.append(f"申诉失败率 {metrics['申诉失败率']}")
        return "　｜　".join(parts)

    def _on_share_clicked(self, status: str) -> None:
        self._select_status(status)

    def _on_share_hovered(self, state: bool, status: str) -> None:
        if state:
            self._share_detail_status = status
            self._share_detail_label.setText(self._status_detail_text(status))

    def _refresh_persistent_chart_details(self) -> None:
        available = {
            _status_bucket(row.get("status")) for row in self._base_rows
        }
        if self._bar_detail_status in available:
            self._bar_detail_label.setText(self._status_detail_text(self._bar_detail_status))
        elif self._bar_detail_status is not None:
            self._bar_detail_status = None
            self._bar_detail_label.setText("当前筛选范围内已没有上次查看的状态。")

        if self._share_detail_status in available:
            self._share_detail_label.setText(self._status_detail_text(self._share_detail_status))
        elif self._share_detail_status is not None:
            self._share_detail_status = None
            self._share_detail_label.setText("当前筛选范围内已没有上次查看的状态。")

    def _select_status(self, status: str) -> None:
        combo_index = self._status_combo.findData(status)
        if combo_index >= 0:
            self._status_combo.setCurrentIndex(combo_index)

    def _update_metrics(self) -> None:
        metrics = _status_metrics(self._base_rows)
        for name, card in self._metric_cards.items():
            card.set_value(metrics[name])
        self._appeal_secondary_metrics_label.setText(
            f"已提交 {metrics['已提交']} 条　｜　"
            f"已出结果 {metrics['已出结果']} 条　｜　"
            f"结果完成率 {metrics['结果完成率']}　｜　"
            f"等待平台结果 {metrics['等待平台结果']} 条　｜　"
            f"结果待确认 {metrics['结果待确认']} 条　｜　"
            f"待出结果金额 {metrics['待出结果金额']}　｜　"
            f"已提交涉及总金额 {metrics['已提交涉及总金额']}"
        )

    def _update_trend(self) -> None:
        days = 7 if self._trend_range_combo.currentIndex() == 0 else 30
        _replace_chart(
            self._trend_view,
            _build_trend_chart(self._all_rows, days),
        )

    def _on_poll_data(self, rows: list[dict]) -> None:
        if self._appeal_rows_loaded and rows == self._all_rows:
            memory_monitor.increment_runtime_metric("report_refreshes_skipped")
            self._last_updated_label.setText(
                f"最后更新时间：{datetime.now():%H:%M:%S}"
            )
            return
        self._appeal_rows_loaded = True
        self._all_rows = rows
        self._apply_filters()
        self._update_trend()
        self._last_updated_label.setText(f"最后更新时间：{datetime.now():%H:%M:%S}")

    def _export_excel(self) -> None:
        self._export("Excel")

    def _export_csv(self) -> None:
        self._export("CSV")

    def _export(self, kind: str) -> None:
        if not self._current_rows:
            msgbox.information(self, "没有数据", "当前筛选条件下没有可导出的订单。")
            return
        suffix = "xlsx" if kind == "Excel" else "csv"
        path, _ = QFileDialog.getSaveFileName(
            self, f"导出当前筛选结果（{kind}）", f"当前筛选订单明细.{suffix}",
            f"{kind} 文件 (*.{suffix})",
        )
        if not path:
            return
        try:
            if kind == "Excel":
                export_service.export_orders_to_excel(self._current_rows, path)
            else:
                export_service.export_orders_to_csv(self._current_rows, path)
        except Exception as exc:
            msgbox.critical(self, "导出失败", str(exc))
            return
        msgbox.information(self, "导出完成", f"已导出当前筛选结果 {len(self._current_rows)} 条：\n{path}")

    def _selected_row(self) -> dict | None:
        selection = self._table.selectionModel().selectedRows()
        if not selection:
            msgbox.information(self, "请选择订单", "请先在明细表中选择一条订单。")
            return None
        return self._model.row_at(selection[0])

    def _show_selected_detail(self) -> None:
        row = self._selected_row()
        if row is not None:
            _OrderDetailDialog(row, self).exec_()

    def _on_row_double_clicked(self, index: QModelIndex) -> None:
        row = self._model.row_at(index)
        if row is not None:
            _OrderDetailDialog(row, self).exec_()
