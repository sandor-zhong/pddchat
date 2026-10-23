"""F 页"凭证截图归档"预览：双击/右键订单明细行时弹出，展示这条订单的
聊天记录凭证截图 + 错误截图 + 填表后截图。

orders 表里存的都是相对 BASE_DIR（主项目根目录）的相对路径，这里统一
拼 PROJECT_PATHS.root 还原成绝对路径读文件——两边路径规则必须保持一致，
这个约定写在 screenshots.py 的 to_relative_evidence_path 里（已经在
实施计划阶段确认过实现细节）。
"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QPixmap
from PyQt5.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..services.project_paths import PROJECT_PATHS


class ScreenshotPreviewDialog(QDialog):
    def __init__(
        self,
        title: str,
        labeled_paths: list[tuple[str, str]],
        parent: QWidget | None = None,
    ) -> None:
        """labeled_paths: [(说明文字, 相对路径字符串), ...]，比如
        [("聊天记录凭证 1/3", "evidence/xxx/1.png"), ("填表截图", "debug/xxx.png")]"""
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(800, 700)

        self._paths = [
            (label, PROJECT_PATHS.root / rel) for label, rel in labeled_paths
        ]
        self._index = 0

        layout = QVBoxLayout(self)

        self._caption = QLabel()
        self._caption.setAlignment(Qt.AlignCenter)
        layout.addWidget(self._caption)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self._image_label = QLabel()
        self._image_label.setAlignment(Qt.AlignCenter)
        scroll.setWidget(self._image_label)
        layout.addWidget(scroll, stretch=1)

        nav_row = QHBoxLayout()
        self._prev_button = QPushButton("上一张")
        self._next_button = QPushButton("下一张")
        self._prev_button.clicked.connect(self._show_prev)
        self._next_button.clicked.connect(self._show_next)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        nav_row.addWidget(self._prev_button)
        nav_row.addWidget(self._next_button)
        nav_row.addStretch(1)
        nav_row.addWidget(close_button)
        layout.addLayout(nav_row)

        if not self._paths:
            self._caption.setText("这条记录没有可预览的截图。")
            self._prev_button.setEnabled(False)
            self._next_button.setEnabled(False)
        else:
            self._render_current()

    def _render_current(self) -> None:
        label, path = self._paths[self._index]
        self._caption.setText(f"{label}  （{self._index + 1}/{len(self._paths)}）")
        self._prev_button.setEnabled(len(self._paths) > 1)
        self._next_button.setEnabled(len(self._paths) > 1)

        if not path.is_file():
            self._image_label.setText(f"文件不存在：{path}")
            self._image_label.setPixmap(QPixmap())
            return

        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._image_label.setText(f"无法读取图片：{path}")
            return
        # 按当前对话框宽度缩放，避免大图把窗口撑爆；保持宽高比。
        scaled = pixmap.scaledToWidth(760, Qt.SmoothTransformation)
        self._image_label.setPixmap(scaled)

    def _show_prev(self) -> None:
        self._index = (self._index - 1) % len(self._paths)
        self._render_current()

    def _show_next(self) -> None:
        self._index = (self._index + 1) % len(self._paths)
        self._render_current()
