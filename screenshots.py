"""通用截图与文件名工具。

从 pdd_chat.py 抽出：这些函数被多模块共用（main.py、run_appeal_batch.py、
appeal_submit.py 都会保存错误截图），与"聊天页"本身无关。
"""

from __future__ import annotations

import random
import re
from pathlib import Path

from playwright.sync_api import Locator, Page

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


def save_natural_content_screenshot(
    page: Page,
    content: Locator,
    path: Path,
    *,
    horizontal_padding: tuple[int, int] = (6, 24),
    vertical_padding: tuple[int, int] = (5, 28),
) -> Path:
    """按可见内容区域截图，四边使用彼此独立的自然余量。

    只改变截图坐标，不修改页面布局，也不对生成后的 PNG 做裁切、拼接
    或其他加工。不同内容尺寸和随机余量会让每张截图宽高自然不同。
    """
    box = content.bounding_box()
    if not box:
        raise RuntimeError("无法获取待截图内容的屏幕位置。")
    coordinates = (box.get(key) for key in ("x", "y", "width", "height"))
    if not all(isinstance(value, (int, float)) for value in coordinates):
        # 兼容不提供真实浏览器坐标的测试桩；生产 Locator 总会走下面的
        # 内容坐标截图分支。
        path.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(path), full_page=True, animations="disabled")
        return path

    viewport = page.viewport_size or {
        "width": int(page.evaluate("window.innerWidth")),
        "height": int(page.evaluate("window.innerHeight")),
    }
    left = max(0, box["x"] - random.randint(*horizontal_padding))
    top = max(0, box["y"] - random.randint(*vertical_padding))
    right = min(
        viewport["width"],
        box["x"] + box["width"] + random.randint(*horizontal_padding),
    )
    bottom = min(
        viewport["height"],
        box["y"] + box["height"] + random.randint(*vertical_padding),
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    page.screenshot(
        path=str(path),
        clip={
            "x": left,
            "y": top,
            "width": max(1, right - left),
            "height": max(1, bottom - top),
        },
        animations="disabled",
        scale="css",
    )
    return path


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
