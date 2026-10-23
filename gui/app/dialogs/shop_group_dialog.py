"""店铺所属组设置：支持单店调整和批量勾选店铺统一分组。"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..services import msgbox, shop_repo


def _group_names(group_by_shop: dict[str, str]) -> list[str]:
    return sorted(
        {name.strip() for name in group_by_shop.values() if name.strip()},
        key=str.casefold,
    )


class ShopGroupDialog(QDialog):
    """设置一家店铺的所属组。"""

    def __init__(self, shop: str, parent=None) -> None:
        super().__init__(parent)
        self.shop = shop
        self.saved = False

        self.setWindowTitle(f"设置所属组 - {shop}")
        self.setMinimumWidth(400)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "输入新组名，或从已有组名中选择。一个店铺只能属于一个组，"
            "保存新组后会自动替换原来的分组。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        group_by_shop = shop_repo.list_shop_groups()
        current = group_by_shop.get(shop)
        layout.addWidget(QLabel("所属组："))
        self._group_combo = QComboBox()
        self._group_combo.setEditable(True)
        self._group_combo.addItems(_group_names(group_by_shop))
        self._group_combo.setCurrentText(current or "")
        if self._group_combo.lineEdit() is not None:
            self._group_combo.lineEdit().setPlaceholderText("例如：组1")
        layout.addWidget(self._group_combo)

        self._clear_check = QCheckBox("清除所属组（设为未分组）")
        self._clear_check.stateChanged.connect(
            lambda state: self._group_combo.setEnabled(not bool(state))
        )
        layout.addWidget(self._clear_check)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save_clicked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_save_clicked(self) -> None:
        group_name = (
            None
            if self._clear_check.isChecked()
            else self._group_combo.currentText().strip()
        )
        if group_name == "":
            msgbox.warning(self, "未填写组名", "请输入组名，或勾选“清除所属组”。")
            return
        shop_repo.set_shop_group(self.shop, group_name)
        self.saved = True
        detail = (
            f"「{self.shop}」已设为未分组。"
            if group_name is None
            else f"「{self.shop}」已归入「{group_name}」。"
        )
        msgbox.information(self, "已保存", detail)
        self.accept()


class BatchShopGroupDialog(QDialog):
    """选择多家店铺，统一设置或清除所属组。"""

    def __init__(
        self,
        shops: list[str],
        group_by_shop: dict[str, str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.saved = False
        self.saved_count = 0

        self.setWindowTitle("批量设置店铺分组")
        self.setMinimumSize(520, 560)
        layout = QVBoxLayout(self)

        hint = QLabel(
            "先填写或选择组名，再勾选要归入该组的店铺。"
            "店铺原来已有分组时，保存后会移动到新组。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        layout.addWidget(QLabel("目标组："))
        self._group_combo = QComboBox()
        self._group_combo.setEditable(True)
        self._group_combo.addItems(_group_names(group_by_shop))
        self._group_combo.setCurrentText("")
        if self._group_combo.lineEdit() is not None:
            self._group_combo.lineEdit().setPlaceholderText("例如：组1")
        layout.addWidget(self._group_combo)

        self._clear_check = QCheckBox("清除所选店铺的分组（设为未分组）")
        self._clear_check.stateChanged.connect(
            lambda state: self._group_combo.setEnabled(not bool(state))
        )
        layout.addWidget(self._clear_check)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("搜索店铺名或当前组名……")
        self._search_box.textChanged.connect(self._apply_shop_filter)
        layout.addWidget(self._search_box)

        selection_row = QHBoxLayout()
        self._selection_label = QLabel()
        selection_row.addWidget(self._selection_label)
        selection_row.addStretch(1)
        select_all_button = QPushButton("全选")
        select_none_button = QPushButton("全不选")
        select_all_button.clicked.connect(lambda: self._set_visible_checked(True))
        select_none_button.clicked.connect(lambda: self._set_visible_checked(False))
        selection_row.addWidget(select_all_button)
        selection_row.addWidget(select_none_button)
        layout.addLayout(selection_row)

        self._shop_list = QListWidget()
        for shop in sorted(set(shops), key=str.casefold):
            current = group_by_shop.get(shop) or "未分组"
            item = QListWidgetItem(f"{shop}  （当前：{current}）")
            item.setData(Qt.UserRole, shop)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self._shop_list.addItem(item)
        self._shop_list.itemChanged.connect(self._update_selection_label)
        layout.addWidget(self._shop_list, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Save).setText("批量保存")
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.accepted.connect(self._on_save_clicked)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_selection_label()

    def selected_shops(self) -> list[str]:
        return [
            str(self._shop_list.item(index).data(Qt.UserRole))
            for index in range(self._shop_list.count())
            if self._shop_list.item(index).checkState() == Qt.Checked
        ]

    def _apply_shop_filter(self, keyword: str) -> None:
        keyword = keyword.strip().casefold()
        for index in range(self._shop_list.count()):
            item = self._shop_list.item(index)
            item.setHidden(bool(keyword) and keyword not in item.text().casefold())

    def _set_visible_checked(self, checked: bool) -> None:
        state = Qt.Checked if checked else Qt.Unchecked
        self._shop_list.blockSignals(True)
        for index in range(self._shop_list.count()):
            item = self._shop_list.item(index)
            if not item.isHidden():
                item.setCheckState(state)
        self._shop_list.blockSignals(False)
        self._update_selection_label()

    def _update_selection_label(self, *_args) -> None:
        self._selection_label.setText(
            f"已选择 {len(self.selected_shops())} / {self._shop_list.count()} 家店铺"
        )

    def _on_save_clicked(self) -> None:
        shops = self.selected_shops()
        if not shops:
            msgbox.warning(self, "未选择店铺", "请至少选择一家店铺。")
            return
        group_name = (
            None
            if self._clear_check.isChecked()
            else self._group_combo.currentText().strip()
        )
        if group_name == "":
            msgbox.warning(self, "未填写组名", "请输入组名，或勾选“清除分组”。")
            return

        self.saved_count = shop_repo.set_shop_group_batch(shops, group_name)
        self.saved = True
        detail = (
            f"已清除 {self.saved_count} 家店铺的分组。"
            if group_name is None
            else f"已将 {self.saved_count} 家店铺归入「{group_name}」。"
        )
        msgbox.information(self, "批量设置完成", detail)
        self.accept()
