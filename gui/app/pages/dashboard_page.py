"""A. 总览仪表盘：跨店铺汇总数字条 + 店铺卡片网格。

只读展示，数据来自 shop_repo.list_shop_summaries()（间接读 db.py 的
daily_shop_summary/list_shop_daily_status/last_activity_by_shop），通过
DbPoller 定时刷新，不在主线程同步查库。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from ..services import shop_repo, time_format
from ..services.app_logging import logger
from ..workers.db_poll_worker import DbPoller

_SCHEDULE_STATUS_TEXT = {
    "done": ("今日已完成", "#2e7d32"),
    "failed": ("今日失败，待重试", "#c62828"),
    "skipped": ("排队中，等待优先补位", "#f57c00"),
    "not_started": ("今日尚未开始", "#757575"),
}


class _StatCard(QFrame):
    """顶部汇总数字条里的一张卡片：大号数字 + 小号说明文字。"""

    def __init__(self, title: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("statCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet(
            "#statCard { background: #f5f5f5; border-radius: 8px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)

        self._value_label = QLabel("0")
        value_font = self._value_label.font()
        value_font.setPointSize(22)
        value_font.setBold(True)
        self._value_label.setFont(value_font)
        self._value_label.setAlignment(Qt.AlignLeft)

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #616161;")

        layout.addWidget(self._value_label)
        layout.addWidget(title_label)

    def set_value(self, value: int) -> None:
        self._value_label.setText(str(value))


class _ShopCard(QFrame):
    """店铺卡片网格里的一张：店铺名 + 登录/调度状态 + 今日计数 + 最近活动时间。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("shopCard")
        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumWidth(240)
        self.setStyleSheet(
            "#shopCard { background: white; border: 1px solid #e0e0e0; border-radius: 8px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        self._name_label = QLabel()
        name_font = self._name_label.font()
        name_font.setBold(True)
        name_font.setPointSize(11)
        self._name_label.setFont(name_font)
        self._name_label.setWordWrap(True)

        self._login_label = QLabel()
        self._schedule_label = QLabel()
        self._counts_label = QLabel()
        self._counts_label.setWordWrap(True)
        self._activity_label = QLabel()
        self._activity_label.setStyleSheet("color: #9e9e9e; font-size: 11px;")

        layout.addWidget(self._name_label)
        layout.addWidget(self._login_label)
        layout.addWidget(self._schedule_label)
        layout.addWidget(self._counts_label)
        layout.addWidget(self._activity_label)

    def update_from(self, summary: shop_repo.ShopSummary) -> None:
        title = summary.shop
        if summary.disabled:
            title += "（已停用）"
        self._name_label.setText(title)

        if summary.has_profile and not summary.login_expired:
            self._login_label.setText("● 已登录" if not summary.disabled else "● 已登录（停用中）")
            self._login_label.setStyleSheet("color: #2e7d32;")
        else:
            # has_profile=False（从没登录过/已删登录态目录）和
            # login_expired=True（今天跑过、确认登录已失效）统一显示
            # "未登录"——运营不需要区分这两种情况，处理方式都一样：
            # 去店铺管理页重新登录一次。
            self._login_label.setText("● 未登录")
            self._login_label.setStyleSheet("color: #c62828;")

        text, color = _SCHEDULE_STATUS_TEXT.get(
            summary.schedule_status, (summary.schedule_status, "#757575")
        )
        if summary.schedule_status == "failed" and summary.schedule_detail:
            text += f"（{summary.schedule_detail}）"
        self._schedule_label.setText(text)
        self._schedule_label.setStyleSheet(f"color: {color};")

        self._counts_label.setText(
            f"待处理 {summary.pending_remaining}　"
            f"已提交 {summary.submitted_today}　"
            f"已跳过 {summary.skipped_today}　"
            f"失败 {summary.failed_today}"
        )

        if summary.last_activity:
            self._activity_label.setText(f"最近活动：{time_format.format_local(summary.last_activity)}")
        else:
            self._activity_label.setText("尚未有处理记录")


class DashboardPage(QWidget):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        root = QVBoxLayout(self)

        header = QLabel("总览仪表盘")
        header_font = header.font()
        header_font.setPointSize(14)
        header_font.setBold(True)
        header.setFont(header_font)
        root.addWidget(header)

        # ---- 顶部汇总数字条 ----
        stats_row = QHBoxLayout()
        self._stat_shop_count = _StatCard("店铺总数")
        self._stat_done_today = _StatCard("今日已完成")
        self._stat_pending = _StatCard("待处理订单")
        self._stat_submitted = _StatCard("今日已提交")
        self._stat_failed = _StatCard("今日失败")
        for card in (
            self._stat_shop_count,
            self._stat_done_today,
            self._stat_pending,
            self._stat_submitted,
            self._stat_failed,
        ):
            card.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
            stats_row.addWidget(card)
        root.addLayout(stats_row)

        # ---- 店铺卡片网格 ----
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        grid_host = QWidget()
        self._grid = QGridLayout(grid_host)
        self._grid.setSpacing(12)
        scroll.setWidget(grid_host)
        root.addWidget(scroll, stretch=1)

        self._cards: dict[str, _ShopCard] = {}

        self._poller = DbPoller(fetch=shop_repo.list_shop_summaries, interval_ms=4000)
        self._poller.data_ready.connect(self._on_data)
        self._poller.fetch_error.connect(self._on_fetch_error)
        self._poller.start()

    def _on_fetch_error(self, message: str) -> None:
        logger.warning(f"[DashboardPage] 刷新数据失败：{message}")

    def _on_data(self, summaries: list[shop_repo.ShopSummary]) -> None:
        totals = shop_repo.overall_totals(summaries)
        self._stat_shop_count.set_value(totals["shop_count"])
        self._stat_done_today.set_value(totals["done_today"])
        self._stat_pending.set_value(totals["pending_remaining"])
        self._stat_submitted.set_value(totals["submitted_today"])
        self._stat_failed.set_value(totals["failed_today"])

        columns = 3
        for index, summary in enumerate(summaries):
            card = self._cards.get(summary.shop)
            if card is None:
                card = _ShopCard()
                self._cards[summary.shop] = card
                row, col = divmod(len(self._cards) - 1, columns)
                self._grid.addWidget(card, row, col)
            card.update_from(summary)
