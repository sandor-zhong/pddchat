"""用 Playwright 测试本地旋转拼图 HTML。

流程：
1. Playwright 打开页面，对背景图和圆形图元素截图；
2. 用图片算法计算最佳旋转角度；
3. 读取页面中滑轨和滑块的实际宽度；
4. 用 角度 / 360 × 最大行程 换算拖动距离；
5. 使用 Playwright 拖动本地滑块；
6. 检查页面是否出现"验证通过（本地演示）"。

安装：
    uv add playwright pillow numpy
    uv run playwright install chromium

运行：
    uv run python playwright_test_demo.py

无界面运行：
    uv run python playwright_test_demo.py --headless
"""

from __future__ import annotations

import argparse
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.sync_api import Browser, Page, sync_playwright

from calculate_demo_angle import calculate_angle


CURRENT_DIR = Path(__file__).resolve().parent
DEFAULT_HTML = CURRENT_DIR / "pdd_rotate_captcha_demo.html"
DEFAULT_SCREENSHOT = CURRENT_DIR / "playwright_validation_result.png"


def calculate_drag_distance(
    angle: float,
    track_width: float,
    button_width: float,
) -> tuple[float, float]:
    """把角度换算为滑块位移，返回（最大行程，目标位移）。"""

    maximum_distance = track_width - button_width
    if maximum_distance <= 0:
        raise ValueError(
            f"滑轨宽度必须大于滑块宽度：track={track_width}, button={button_width}",
        )

    normalized_angle = angle % 360
    target_distance = normalized_angle / 360 * maximum_distance
    return maximum_distance, target_distance


def extract_images_via_playwright(page: Page) -> tuple[Image.Image, Image.Image]:
    """对页面中背景图和圆形图元素分别截图，返回 RGBA 图片。"""

    bg_bytes = page.locator(".rotateslider-img-bg").screenshot()
    circle_bytes = page.locator(".rotateslider-circle").screenshot()

    bg = Image.open(BytesIO(bg_bytes)).convert("RGBA")
    circle = Image.open(BytesIO(circle_bytes)).convert("RGBA")
    return bg, circle


def drag_slider(page: Page, angle: float, steps: int) -> tuple[float, float]:
    """读取页面尺寸、换算距离并执行一次鼠标拖动。"""

    track = page.locator(".rotateslide-handlebar")
    button = page.locator("#slide-button")

    track.wait_for(state="visible")
    button.wait_for(state="visible")

    track_box = track.bounding_box()
    button_box = button.bounding_box()
    if track_box is None or button_box is None:
        raise RuntimeError("无法取得滑轨或滑块的位置")

    maximum_distance, target_distance = calculate_drag_distance(
        angle=angle,
        track_width=track_box["width"],
        button_width=button_box["width"],
    )

    start_x = button_box["x"] + button_box["width"] / 2
    start_y = button_box["y"] + button_box["height"] / 2
    end_x = start_x + target_distance

    page.mouse.move(start_x, start_y)
    page.mouse.down()
    page.mouse.move(end_x, start_y, steps=steps)
    page.mouse.up()

    return maximum_distance, target_distance


def open_browser(playwright, *, headless: bool, slow_mo: int, channel: str | None) -> Browser:
    """打开 Chromium；可选使用本机 Edge 或 Chrome 通道。"""

    if headless:
        raise ValueError("系统鼠标操作需要可见的浏览器窗口。")
    launch_options: dict[str, object] = {
        "headless": headless,
        "slow_mo": slow_mo,
    }
    if channel:
        launch_options["channel"] = channel

    return playwright.chromium.launch(**launch_options)


def run_test(args: argparse.Namespace) -> None:
    html_path = args.html.resolve()
    screenshot_path = args.screenshot.resolve()

    if not html_path.exists():
        raise SystemExit(f"HTML 文件不存在：{html_path}")

    with sync_playwright() as playwright:
        browser = open_browser(
            playwright,
            headless=args.headless,
            slow_mo=args.slow_mo,
            channel=args.channel,
        )

        try:
            context = browser.new_context(no_viewport=True)
            from native_mouse import install_system_mouse
            install_system_mouse(context)
            page = context.new_page()
            page.goto(html_path.as_uri(), wait_until="domcontentloaded")

            # 第一步：对页面中图片元素截图，用图片算法动态计算角度。
            background, circle = extract_images_via_playwright(page)
            angle_result = calculate_angle(background, circle)
            calculated_angle = angle_result.best_angle

            print(f"图片算法计算角度：{calculated_angle}°（CSS 顺时针）")
            print(f"最低圆周颜色误差：{angle_result.best_score:.2f}")

            maximum_distance, target_distance = drag_slider(
                page,
                angle=calculated_angle,
                steps=args.steps,
            )

            status = page.locator(".captcha-status")
            page.locator(".captcha-wrapper.is-success").wait_for(
                state="visible",
                timeout=args.timeout,
            )
            status_text = status.inner_text()

            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(screenshot_path), full_page=True)

            print(f"滑块最大行程：{maximum_distance:.2f}px")
            print(
                "换算公式："
                f"{calculated_angle} / 360 × {maximum_distance:.2f} "
                f"= {target_distance:.2f}px",
            )
            print(f"实际拖动距离：{target_distance:.2f}px")
            print(f"页面结果：{status_text}")
            print(f"测试截图：{screenshot_path}")

            if not args.headless and args.pause_ms > 0:
                page.wait_for_timeout(args.pause_ms)
        finally:
            browser.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="计算本地旋转拼图角度并使用 Playwright 拖动验证",
    )
    parser.add_argument(
        "html",
        nargs="?",
        type=Path,
        default=DEFAULT_HTML,
        help=f"HTML 文件，默认：{DEFAULT_HTML.name}",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=DEFAULT_SCREENSHOT,
        help=f"结果截图路径，默认：{DEFAULT_SCREENSHOT.name}",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="使用无界面浏览器",
    )
    parser.add_argument(
        "--channel",
        choices=("msedge", "chrome"),
        help="使用电脑中已经安装的 Edge 或 Chrome",
    )
    parser.add_argument(
        "--slow-mo",
        type=int,
        default=15,
        help="Playwright 每步减速毫秒数，默认 15",
    )
    parser.add_argument(
        "--steps",
        type=int,
        default=30,
        help="拖动过程的鼠标移动步数，默认 30",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5_000,
        help="等待验证成功的超时时间（毫秒），默认 5000",
    )
    parser.add_argument(
        "--pause-ms",
        type=int,
        default=1_500,
        help="有界面模式成功后停留时间（毫秒），默认 1500",
    )
    parser.add_argument("--viewport-width", type=int, default=420)
    parser.add_argument("--viewport-height", type=int, default=520)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run_test(args)


if __name__ == "__main__":
    main()
