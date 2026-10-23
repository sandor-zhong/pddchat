"""QMessageBox 的中文按钮封装。

QMessageBox 的标准按钮（Ok/Yes/No/Cancel……）默认文案是英文——Qt 自带的
中文翻译文件（qt_zh_CN.qm）在这个 PyQt5 打包版本里实测不覆盖这些标准
按钮字符串（翻译加载成功但文案没变），没有更省事的官方本地化路径可用，
所以统一在这里手动把按钮文案改成中文，页面代码改调这几个函数，不要
直接用 QMessageBox.information/warning/critical/question 的静态便捷方法。
"""

from __future__ import annotations

from PyQt5.QtWidgets import QMessageBox, QWidget


def information(parent: QWidget | None, title: str, text: str) -> None:
    box = QMessageBox(QMessageBox.Information, title, text, QMessageBox.Ok, parent)
    box.button(QMessageBox.Ok).setText("好的")
    box.exec_()


def warning(parent: QWidget | None, title: str, text: str) -> None:
    box = QMessageBox(QMessageBox.Warning, title, text, QMessageBox.Ok, parent)
    box.button(QMessageBox.Ok).setText("好的")
    box.exec_()


def critical(parent: QWidget | None, title: str, text: str) -> None:
    box = QMessageBox(QMessageBox.Critical, title, text, QMessageBox.Ok, parent)
    box.button(QMessageBox.Ok).setText("好的")
    box.exec_()


def confirm_yes_no(
    parent: QWidget | None,
    title: str,
    text: str,
    *,
    default_yes: bool = False,
    yes_text: str = "是",
    no_text: str = "否",
) -> bool:
    """返回 True 表示用户点了"是"（或自定义的 yes_text）。

    default_yes=False（默认）时把默认焦点/回车触发的按钮设为"否"——
    高风险操作（删除、真实提交之类）不应该让误按回车变成"确认"。
    """
    box = QMessageBox(QMessageBox.Warning, title, text, parent=parent)
    yes_button = box.addButton(yes_text, QMessageBox.YesRole)
    no_button = box.addButton(no_text, QMessageBox.NoRole)
    box.setDefaultButton(no_button if not default_yes else yes_button)
    box.exec_()
    return box.clickedButton() is yes_button
