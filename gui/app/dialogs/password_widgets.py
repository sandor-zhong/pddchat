"""Password display helpers for the editable account table."""

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QLineEdit, QStyledItemDelegate, QTableWidgetItem


def eye_icon(visible: bool) -> QIcon:
    pixmap = QPixmap(22, 20)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor("#385b7a"), 1.6))
    path = QPainterPath()
    path.moveTo(2, 10)
    path.quadTo(11, 0, 20, 10)
    path.quadTo(11, 20, 2, 10)
    painter.drawPath(path)
    painter.drawEllipse(8, 7, 6, 6)
    if not visible:
        painter.drawLine(3, 3, 19, 17)
    painter.end()
    return QIcon(pixmap)


class PasswordItem(QTableWidgetItem):
    def data(self, role):
        table = self.tableWidget()
        if role == Qt.DisplayRole and table is not None and not table.passwords_visible:
            return "••••••••" if super().data(Qt.EditRole) else ""
        return super().data(role)


class PasswordDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        editor = QLineEdit(parent)
        editor.setEchoMode(QLineEdit.Normal if self.parent().passwords_visible else QLineEdit.Password)
        return editor
