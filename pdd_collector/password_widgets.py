from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap
from PyQt5.QtWidgets import QLineEdit, QStyledItemDelegate, QTableWidgetItem


def eye_icon(visible=True):
    pixmap = QPixmap(22, 20)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(QPen(QColor('#385b7a'), 1.6))
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


def add_password_eye(edit, visible=True):
    action = edit.addAction(eye_icon(visible), QLineEdit.TrailingPosition)
    def update(show):
        edit.setEchoMode(QLineEdit.Normal if show else QLineEdit.Password)
        action.setIcon(eye_icon(show))
        action.setText('隐藏密码' if show else '显示密码')
        action.setToolTip(action.text())
    action.triggered.connect(lambda: update(edit.echoMode() != QLineEdit.Normal))
    update(visible)
    return action


class PasswordItem(QTableWidgetItem):
    def data(self, role):
        if role == Qt.DisplayRole and self.tableWidget() is not None and not self.tableWidget().passwords_visible:
            return '••••••••' if super().data(Qt.EditRole) else ''
        return super().data(role)


class PasswordDelegate(QStyledItemDelegate):
    def createEditor(self, parent, option, index):
        edit = QLineEdit(parent)
        edit.setEchoMode(QLineEdit.Normal if self.parent().passwords_visible else QLineEdit.Password)
        return edit
