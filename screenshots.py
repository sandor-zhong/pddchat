"""通用截图与文件名工具。

从 pdd_chat.py 抽出：这些函数被多模块共用（main.py、run_appeal_batch.py、
appeal_submit.py 都会保存错误截图），与"聊天页"本身无关。
"""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Page

from paths import BASE_DIR, DEBUG_DIR


def to_relative_evidence_path(path: Path) -> str:
    """把凭证截图的绝对路径转换成相对项目根目录的字符串，方便存入 JSON。"""
    try:
        return path.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return str(path)


def safe_filename(value: str) -> str:
    """将订单编号转换成安全的文件名。"""
    cleaned = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        value,
    )
    return cleaned.strip() or "unknown_order"


def save_error_screenshot(
    page: Page,
    order_no: str,
) -> Path | None:
    """查询失败时保存页面截图。"""
    DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        DEBUG_DIR
        / f"error_{safe_filename(order_no)}.png"
    )

    try:
        page.screenshot(
            path=str(screenshot_path),
            full_page=True,
        )
        return screenshot_path

    except Exception as error:
        # 保留 traceback 便于定位（P1 顺路清理）
        import traceback

        print(f"保存错误截图失败：{error}")
        traceback.print_exc()
        return None
