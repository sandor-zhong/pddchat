"""B 页"设置处理上限"对话框：配置一家店铺单次采集最多处理多少个可申诉
订单（默认只读第一页，通常约 20 个；配置后允许翻页凑够这个数量，见
run_appeal_batch.py 的 collect_all_appealable_order_numbers）。

跟 AddShopDialog 一样是个小型 QDialog（同样的布局/按钮风格），但不需要
它那套 QThread+Worker 机制——这里只是一次同步的本地 SQLite 读写，没有
子进程、不用等待，没有理由做成异步。

用户是否点了保存通过 self.saved 属性暴露给调用方（跟 AddShopDialog.
result_shop 是同样的约定：exec_() 返回值本身也能看 Accepted/Rejected，
但调用方现有代码习惯读一个专属属性）。
"""

from __future__ import annotations

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from ..services import msgbox, shop_repo

# 跟 main.py 的 _MAX_ORDERS_PAGE_SAFETY_NET（200）配套：200 页 × 现实中
# 页面常见的每页行数，留足够余量覆盖到这里能配置的最大值。以后如果调大
# 这个上限，main.py 那个安全网页数上限要跟着一起调，不然配置了很大的值
# 也翻不到那么多页。
_SPIN_MIN = 1
_SPIN_MAX = 999
_SPIN_DEFAULT = 20  # 仅用作勾选"启用自定义上限"瞬间的初始值（PDD 表格
# 一页大概的行数，选它当起点比较直观），不代表任何硬编码的页面大小假设。


class ShopMaxOrdersDialog(QDialog):
    def __init__(self, shop: str, parent=None) -> None:
        super().__init__(parent)
        self.shop = shop
        self.saved = False

        self.setWindowTitle(f"设置处理上限 - {shop}")
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)

        hint = QLabel(
            "默认每次只读取申诉列表第一页（约 20 个可申诉订单）。\n"
            "如果这家店铺经常一天超过这个数量，可以启用自定义上限，"
            "系统会自动翻页凑够设置的数量。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        current = shop_repo.get_shop_max_orders(shop)

        self._enable_check = QCheckBox("启用自定义处理上限")
        layout.addWidget(self._enable_check)

        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("最多处理订单数："))
        self._spin = QSpinBox()
        self._spin.setRange(_SPIN_MIN, _SPIN_MAX)
        self._spin.setValue(current if current is not None else _SPIN_DEFAULT)
        spin_row.addWidget(self._spin)
        spin_row.addStretch(1)
        layout.addLayout(spin_row)

        # 先把初始状态摆好，再连信号——避免 setChecked() 这次程序化赋值
        # 被信号处理器当成一次"用户刚勾选/取消"来处理（跟 run_page.py
        # 里 _auto_enable_check 的初始化顺序是同一个规避思路）。
        self._enable_check.setChecked(current is not None)
        self._spin.setEnabled(current is not None)
        self._enable_check.stateChanged.connect(
            lambda state: self._spin.setEnabled(bool(state))
        )

        self._save_button = QPushButton("保存")
        self._save_button.clicked.connect(self._on_save_clicked)
        layout.addWidget(self._save_button)

        # QDialogButtonBox 的标准按钮文案默认是英文（"Cancel"）——跟
        # AddShopDialog 一样手动改成中文更省事可靠（见其文档说明）。
        self._buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self._buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    def _on_save_clicked(self) -> None:
        value = self._spin.value() if self._enable_check.isChecked() else None
        shop_repo.set_shop_max_orders(self.shop, value)
        self.saved = True
        if value is None:
            msgbox.information(
                self, "已保存",
                f"「{self.shop}」已恢复默认处理行为（只读取申诉列表第一页）。",
            )
        else:
            msgbox.information(
                self, "已保存",
                f"「{self.shop}」单次最多处理 {value} 个可申诉订单，"
                "下次该店铺运行时生效。",
            )
        self.accept()


class BatchShopMaxOrdersDialog(QDialog):
    """为多家店铺统一设置处理上限；默认选中全部传入店铺。"""

    def __init__(self, shops: list[str], parent=None) -> None:
        super().__init__(parent)
        self.saved = False
        self.saved_count = 0

        self.setWindowTitle("批量设置店铺处理上限")
        self.setMinimumSize(480, 520)

        layout = QVBoxLayout(self)
        hint = QLabel(
            "选择要统一设置的店铺。保存后，这些店铺下次运行申诉任务时"
            "会使用相同的单次处理上限。"
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._search_box = QLineEdit()
        self._search_box.setPlaceholderText("搜索店铺名……")
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
        for shop in sorted(set(shops)):
            item = QListWidgetItem(shop)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self._shop_list.addItem(item)
        self._shop_list.itemChanged.connect(self._update_selection_label)
        layout.addWidget(self._shop_list, stretch=1)

        self._enable_check = QCheckBox("启用统一的自定义处理上限")
        self._enable_check.setChecked(True)
        layout.addWidget(self._enable_check)

        spin_row = QHBoxLayout()
        spin_row.addWidget(QLabel("每家店铺最多处理订单数："))
        self._spin = QSpinBox()
        self._spin.setRange(_SPIN_MIN, _SPIN_MAX)
        self._spin.setValue(_SPIN_DEFAULT)
        self._spin.setSuffix(" 单")
        spin_row.addWidget(self._spin)
        spin_row.addStretch(1)
        layout.addLayout(spin_row)
        self._enable_check.stateChanged.connect(
            lambda state: self._spin.setEnabled(bool(state))
        )

        reset_hint = QLabel("取消勾选后保存，可让选中的店铺批量恢复默认处理行为。")
        reset_hint.setStyleSheet("color: #757575;")
        reset_hint.setWordWrap(True)
        layout.addWidget(reset_hint)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        self._buttons.button(QDialogButtonBox.Save).setText("批量保存")
        self._buttons.button(QDialogButtonBox.Cancel).setText("取消")
        self._buttons.accepted.connect(self._on_save_clicked)
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

        self._update_selection_label()

    def selected_shops(self) -> list[str]:
        return [
            self._shop_list.item(index).text()
            for index in range(self._shop_list.count())
            if self._shop_list.item(index).checkState() == Qt.Checked
        ]

    def _apply_shop_filter(self, keyword: str) -> None:
        keyword = keyword.strip()
        for index in range(self._shop_list.count()):
            item = self._shop_list.item(index)
            item.setHidden(bool(keyword) and keyword not in item.text())

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

        value = self._spin.value() if self._enable_check.isChecked() else None
        self.saved_count = shop_repo.set_shop_max_orders_batch(shops, value)
        self.saved = True
        if value is None:
            detail = f"已让 {self.saved_count} 家店铺恢复默认处理行为。"
        else:
            detail = (
                f"已将 {self.saved_count} 家店铺的单次处理上限统一设置为 "
                f"{value} 单，下次运行时生效。"
            )
        msgbox.information(self, "批量设置完成", detail)
        self.accept()
