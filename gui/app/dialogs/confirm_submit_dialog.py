"""C 页"真实提交"二次确认弹窗。

触发条件：每次点"启动"、且当前选中"真实提交"单选项——不做"记住我的
选择"，高风险操作每次都问。用 QMessageBox.Warning 图标（比 Question
语义更强），默认按钮是"取消"，防止手滑按回车误触真实提交。
"""

from __future__ import annotations

from PyQt5.QtWidgets import QMessageBox, QWidget


def confirm_real_submit(
    parent: QWidget | None,
    shops: list[str],
    pool_size: int,
    manual_captcha: bool,
) -> bool:
    """返回 True 表示用户明确点了"确认真实提交"。"""

    if len(shops) <= 5:
        shop_text = "、".join(shops)
    else:
        shop_text = "、".join(shops[:5]) + f" 等共 {len(shops)} 家"

    captcha_text = "人工手动过验证码" if manual_captcha else "自动破解优先，失败会放弃并跳过该单"

    text = (
        f"即将对以下店铺执行【真实提交】：\n{shop_text}\n\n"
        f"并发上限：同时最多 {pool_size} 家店铺\n"
        f"验证码模式：{captcha_text}\n\n"
        "⚠ 真实提交后申诉记录会实际发送给拼多多平台，不可撤回、不可修改。\n"
        "请确认以上信息无误。"
    )

    box = QMessageBox(QMessageBox.Warning, "确认真实提交", text, parent=parent)
    confirm_button = box.addButton("确认真实提交", QMessageBox.YesRole)
    cancel_button = box.addButton("取消", QMessageBox.NoRole)
    box.setDefaultButton(cancel_button)
    box.exec_()
    return box.clickedButton() is confirm_button
