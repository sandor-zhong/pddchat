"""滑块/字符/旋转/图片点选验证码处理。

从 pdd_chat.py 抽出：验证码是 PDD 后台随时可能弹的横切能力，
应被所有页面操作共用，与"聊天页"无关。
"""

from __future__ import annotations

import base64
import json
import math
import os
import random
import re
import time
import uuid
from datetime import datetime
from io import BytesIO
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from PIL import Image
from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from calculate_demo_angle import calculate_angle as _calculate_angle
from paths import DEBUG_DIR, BASE_DIR, SHOP_ID
import captcha_diagnostics
from stealth import human_click, human_type, jitter_wait, without_business_pacing

# ============================================================
# 滑块验证码处理
# ============================================================
#
# 拼多多后台在任意页面、任意时刻都可能弹出滑块验证码。
# 这里只负责"发现验证码 -> 停下来 -> 提醒人 -> 等人滑完 -> 继续"，
# 不会去自动滑动或绕过验证码：验证码本来就是用来区分人和程序的，
# 替它通过就失去了意义。程序在这里让路，把控制权交还给人。

# 两种样式的验证码（背景图不同）外层结构一致，都带这两个标志。
# 注意：不能用 MDL_innerWrapper_5-155-0 这类类名，后缀是组件版本哈希，
# 同一套组件在不同页面就已经是 _5-163-0、1198e34 等不同值，升级还会再变。
# id 和普通类名才是稳定锚点。
CAPTCHA_SELECTOR = "#slide-captcha-dialog, .captcha-wrapper"
VISIBLE_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog:visible, .captcha-wrapper:visible"
)

# 旋转验证码专用选择器（区别于普通滑块/点选验证码）
ROTATE_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__rotateslider, "
    ".captcha-wrapper__rotateslider"
)
VISIBLE_ROTATE_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__rotateslider:visible, "
    ".captcha-wrapper__rotateslider:visible"
)
# 字符验证码专用选择器
TEXT_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__text, "
    ".captcha-wrapper__text"
)
VISIBLE_TEXT_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__text:visible, "
    ".captcha-wrapper__text:visible"
)
# 图片点选验证码专用选择器
PICTURE_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__picture, "
    ".captcha-wrapper__picture"
)
VISIBLE_PICTURE_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__picture:visible, "
    ".captcha-wrapper__picture:visible"
)

# 旋转验证码自动破解 — 元素选择器（与拼多多 DOM 一一对应）
CAPTCHA_BG_SELECTOR = ".rotateslider-img-bg"
CAPTCHA_CIRCLE_SELECTOR = ".rotateslider-circle"
CAPTCHA_TRACK_SELECTOR = ".rotateslide-handlebar"
CAPTCHA_BUTTON_SELECTOR = "#slide-button"
CAPTCHA_REFRESH_SELECTOR = ".captcha-refresh"

# 旋转验证码识别的最低置信度（NCC 分数）——低于此值直接放弃拖动、回退人工。
# 现网历史 debug 样本中最小观察到 0.25，先设 0.15 作为"明显错误"的下限，
# 需要观察调优时可用环境变量 PDDCHAT_ROTATE_MIN_SCORE 覆盖。
ROTATE_CAPTCHA_MIN_SCORE = float(
    os.environ.get("PDDCHAT_ROTATE_MIN_SCORE", "0.15")
)

# 字符验证码自动破解 — 元素选择器
TEXT_CAPTCHA_IMG_SELECTOR = ".captcha-source img"
TEXT_CAPTCHA_INPUT_SELECTOR = "#captchaInput"
PICTURE_CAPTCHA_IMG_SELECTOR = "#captchaClickImg, .picture-img img"
PICTURE_CAPTCHA_QUESTION_SELECTOR = ".picture-text__default, .picture-text"
PICTURE_CAPTCHA_MIN_CONFIDENCE = float(
    os.environ.get("PDDCHAT_PICTURE_MIN_CONFIDENCE", "0.55")
)
# 备用选择器（容器内泛化搜索，覆盖未来版本变化）
CAPTCHA_BG_FALLBACK = "img:first-of-type, canvas:first-of-type"
CAPTCHA_CIRCLE_FALLBACK = "img:nth-of-type(2), canvas:nth-of-type(2)"
CAPTCHA_TRACK_FALLBACK = "[class*='handle'], [class*='track']"
CAPTCHA_BUTTON_FALLBACK = "[class*='button']:not([type='submit'])"

# 等待人工滑完的最长时间：5 分钟
CAPTCHA_WAIT_TIMEOUT = 5 * 60 * 1000
# 自动验证统一等待 5 秒；每种可自动处理的验证码最多尝试 5 次。
CAPTCHA_VERIFY_TIMEOUT = 5_000
CAPTCHA_AUTO_MAX_ATTEMPTS = 5


def is_captcha_visible(page: Page) -> bool:
    """
    检测当前页面上是否弹出了滑块验证码。

    这个函数会在每个关键步骤后被频繁调用，所以：
    - 只做存在性判断，不做任何等待，开销必须接近于零；
    - 全程兜底异常。页面正在跳转时 locator 可能抛错，
      检测函数自己绝不能成为新的崩溃来源。
    """

    try:
        return page.locator(VISIBLE_CAPTCHA_SELECTOR).count() > 0

    except Exception:
        return False


def is_rotate_captcha(page: Page) -> bool:
    """检测当前验证码是否是旋转验证码（可自动破解的类型）。

    拼多多存在多种验证码（普通滑块、点选图形、旋转拼图），
    只有旋转验证码（容器带 captcha-wrapper__rotateslider 类）
    才能用图片角度识别方案自动破解。
    """
    try:
        return page.locator(VISIBLE_ROTATE_CAPTCHA_SELECTOR).count() > 0

    except Exception:
        return False


def is_text_captcha(page: Page) -> bool:
    """检测当前验证码是否是字符验证码（容器带 captcha-wrapper__text 类）。"""
    try:
        return page.locator(VISIBLE_TEXT_CAPTCHA_SELECTOR).count() > 0

    except Exception:
        return False


def is_picture_captcha(page: Page) -> bool:
    """检测当前验证码是否是图片点选验证码。"""
    try:
        return page.locator(VISIBLE_PICTURE_CAPTCHA_SELECTOR).count() > 0
    except Exception:
        return False


def describe_timeout(timeout: int) -> str:
    """把毫秒超时说成人话，不足一分钟时按秒显示，避免出现"0 分钟"。"""

    if timeout < 60_000:
        return f"{timeout // 1000} 秒"

    return f"{timeout // 60_000} 分钟"


def save_captcha_screenshot(page: Page) -> Path | None:
    """出现验证码时留一张截图，方便事后确认当时页面的状态。"""

    DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    # 同一秒内可能连续重试，带上毫秒以免后一次截图覆盖前一次。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]

    screenshot_path = (
        DEBUG_DIR / f"captcha_{timestamp}.png"
    )

    try:
        # 只截可视区域，不能用 full_page=True：整页截图会强制滚动页面、
        # 可能造成布局位移，干扰"点选图形"这类依赖精确坐标的验证码作答。
        page.screenshot(
            path=str(screenshot_path),
        )

        return screenshot_path

    except Exception as error:
        print(f"保存验证码截图失败：{error}")
        return None


def wait_for_human_captcha(
    page: Page,
    timeout: int = CAPTCHA_WAIT_TIMEOUT,
) -> bool:
    """
    暂停自动化，等待人工在浏览器里完成滑块拼图。

    返回 True 表示当前没有验证码或验证码已经消失，可以继续；
    返回 False 表示等待超时，验证码仍然挡在页面上。
    """

    if not is_captcha_visible(page):
        return True

    screenshot_path = save_captcha_screenshot(page)

    wait_text = describe_timeout(timeout)

    print()
    print("=" * 60)
    print("检测到验证码，自动化已暂停。")
    print("请在打开的浏览器窗口中手动完成验证码。")
    print("验证通过后程序会自动继续，不需要回到控制台按键。")
    print(f"最长等待 {wait_text}。")

    if screenshot_path:
        print(f"当前页面截图：{screenshot_path}")

    print("=" * 60)

    # 控制台响铃，提醒可能已经离开电脑的人回来处理
    print("\a", end="", flush=True)

    try:
        # state="hidden" 同时覆盖"节点被移除"和"节点还在但隐藏"两种消失方式
        page.wait_for_selector(
            VISIBLE_CAPTCHA_SELECTOR,
            state="hidden",
            timeout=timeout,
        )

    except PlaywrightTimeoutError:
        print()
        print(f"等待 {wait_text} 后验证码仍未完成。")
        return False

    # 验证通过后页面通常会重新加载数据，稍等渲染稳定
    jitter_wait(page, 1_000)

    print("验证码已完成，继续执行。")
    print()

    return True


def _find_captcha_element(
    page: Page,
    primary: str,
    fallback: str,
) -> Locator | None:
    """在 captcha 容器内查找元素，主选择器优先，失败则用备用。"""
    captcha = page.locator(CAPTCHA_SELECTOR).first

    # 主选择器（captcha 容器内）
    el = captcha.locator(primary)
    if el.count() > 0:
        return el.first

    # 备用选择器（captcha 容器内）
    el = captcha.locator(fallback)
    if el.count() > 0:
        return el.first

    # 最后兜底：整个页面范围内搜索
    el = page.locator(primary)
    if el.count() > 0:
        return el.first

    return None


def dump_rotate_captcha_info(
    page: Page,
    *,
    best_angle: float,
    best_score: float,
    target_distance: float,
    max_distance: float,
    track_box: dict[str, float] | None,
    button_box: dict[str, float] | None,
    bg_size: tuple[int, int],
    circle_size: tuple[int, int],
) -> Path | None:
    """把旋转验证码的 DOM 源码和关键计算参数存档，供事后分析。

    存档文件（debug/ 目录）：
      rotate_captcha_<时间戳>.html  — 验证码容器 outerHTML
      rotate_captcha_<时间戳>.json  — 计算角度、拖动距离、元素尺寸等

    存档失败只打日志，不影响自动破解主流程。
    """
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    html_path: Path | None = None
    json_path: Path | None = None

    try:
        captcha = page.locator(CAPTCHA_SELECTOR).first
        html = ""
        if captcha.count() > 0:
            html = captcha.evaluate("el => el.outerHTML")
        html_path = DEBUG_DIR / f"rotate_captcha_{timestamp}.html"
        html_path.write_text(html, encoding="utf-8")
    except Exception as error:
        print(f"保存旋转验证码 HTML 失败：{error}")

    try:
        payload = {
            "timestamp": timestamp,
            "best_angle_deg": round(best_angle, 4),
            "best_score": round(best_score, 6),
            "target_distance_px": round(target_distance, 2),
            "max_distance_px": round(max_distance, 2),
            "track_box": track_box,
            "button_box": button_box,
            "bg_size": list(bg_size),
            "circle_size": list(circle_size),
        }
        json_path = DEBUG_DIR / f"rotate_captcha_{timestamp}.json"
        json_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as error:
        print(f"保存旋转验证码参数失败：{error}")

    if html_path is not None or json_path is not None:
        print(
            "旋转验证码已存档："
            f"{html_path.name if html_path else '—'} / "
            f"{json_path.name if json_path else '—'}"
        )
    return html_path or json_path


def _human_drag_slider(
    page: Page,
    start_x: float,
    start_y: float,
    end_x: float,
    total_steps: int = 50,
) -> None:
    """拟人化滑块拖动：随机轨迹 + 变速 + 中途停顿 + 过冲回拉。

    生成一条带有 Y 轴抖动、先加速后减速的贝塞尔曲线轨迹，
    在拖动过程中随机插入微小停顿，最后略微过冲目标位置再拉回来。
    """
    # ── ① 拖动前：在起点附近微小晃动（模拟人眼确认位置） ──
    wobble_points = random.randint(2, 4)
    for _ in range(wobble_points):
        wx = start_x + random.uniform(-3, 3)
        wy = start_y + random.uniform(-3, 3)
        page.mouse.move(wx, wy)
        page.wait_for_timeout(random.randint(40, 100))

    # 停顿一下，模拟"准备开始拖动"
    page.wait_for_timeout(random.randint(100, 300))

    page.mouse.down()

    # ── ② 生成贝塞尔缓动轨迹 ──
    total_distance = end_x - start_x
    # ease_in_out 采样点：用 sin 曲线变换实现慢→快→慢
    raw_steps = [i / total_steps for i in range(total_steps + 1)]
    eased_steps = []
    for t in raw_steps:
        # 使用 cubic ease-in-out
        if t < 0.5:
            et = 4 * t * t * t
        else:
            et = 1 - pow(-2 * t + 2, 3) / 2
        eased_steps.append(et)

    # Y 轴抖动幅度
    y_jitter_base = random.uniform(3, 7)

    # 随机选择 1-3 个中途停顿点
    num_pauses = random.randint(1, 3)
    pause_indices = sorted(random.sample(
        range(total_steps // 4, total_steps * 3 // 4), num_pauses,
    ))

    prev_t = 0.0
    for i, et in enumerate(eased_steps):
        # Y 轴：随机低频正弦抖动 + 一点随机噪声
        phase = i / total_steps * math.pi * random.uniform(3, 6)
        y_offset = math.sin(phase) * y_jitter_base + random.uniform(-1, 1)

        current_x = start_x + et * total_distance
        current_y = start_y + y_offset

        # 每步之间的间隔也模拟变速：密集段快、稀疏段慢
        step_delta = abs(et - prev_t)
        base_interval = int(5 + step_delta * 200)  # 5~20ms
        interval = max(2, base_interval + random.randint(-2, 2))
        prev_t = et

        page.mouse.move(current_x, current_y)
        page.wait_for_timeout(interval)

        # 在停顿点暂停更久（模拟手指/鼠标短暂停顿）
        if i in pause_indices:
            page.wait_for_timeout(random.randint(80, 200))

    # ── ③ 过冲回拉（模拟滑块惯性过头再拉回） ──
    overshoot = random.uniform(3, 8)
    overshoot_x = end_x + overshoot
    for ox in _overshoot_traj(end_x, overshoot_x, 6):
        page.mouse.move(ox, start_y)
        page.wait_for_timeout(random.randint(8, 15))

    # ── ④ 松手前短暂停顿（模拟人眼确认对齐） ──
    page.wait_for_timeout(random.randint(150, 400))
    page.mouse.up()


def _overshoot_traj(
    start: float, peak: float, steps: int,
) -> list[float]:
    """生成 过冲→拉回 的小轨迹：先往外再回到原始目标。"""
    traj = []
    for i in range(steps + 1):
        t = i / steps
        # 抛物线：在 0.5 处到达峰值，在 1.0 回到 start
        val = peak + (start - peak) * (2 * t - 1) ** 2
        traj.append(val)
    return traj


def _extract_captcha_image_bytes(page: Page, el: Locator) -> bytes | None:
    """以 Base64 方式提取验证码图片元素的原始像素字节。

    不调用 screenshot()：优先取元素 src 的 data URI（原始图，不含 CSS 变换），
    src 为 http/blob URL 时在页面内 fetch 转 data URI；元素为 canvas 时用
    toDataURL。全部失败返回 None，由调用方回退人工处理。
    """
    try:
        result = el.evaluate(
            """(el) => {
                if (el.tagName === 'CANVAS') {
                    return { ok: true, value: el.toDataURL('image/png') };
                }
                const src = el.getAttribute('src') || '';
                if (src.startsWith('data:')) {
                    return { ok: true, value: src };
                }
                return { ok: false, value: src };
            }"""
        )
    except Exception as error:
        print(f"  读取验证码图片 src 失败：{error}")
        return None

    if not result["ok"]:
        # src 是 http/blob URL：页面内 fetch（携带 cookie）转 data URI
        src_url = result["value"]
        if not src_url:
            print("  验证码图片元素没有 src，无法提取。")
            return None
        try:
            data_uri = page.evaluate(
                """(url) => fetch(url, { credentials: 'include' })
                    .then((r) => r.blob())
                    .then((b) => new Promise((resolve, reject) => {
                        const fr = new FileReader();
                        fr.onload = () => resolve(fr.result);
                        fr.onerror = () => reject(new Error('FileReader 读取失败'));
                        fr.readAsDataURL(b);
                    }))""",
                src_url,
            )
        except Exception as error:
            print(f"  抓取验证码图片 URL 失败：{error}")
            return None
        if not data_uri or not data_uri.startswith("data:"):
            print("  抓取验证码图片 URL 后未得到 data URI。")
            return None
    else:
        data_uri = result["value"]

    # 解析 data URI 还原原始字节
    try:
        header, encoded = data_uri.split(",", 1)
        if ";base64" in header:
            return base64.b64decode(encoded)
        from urllib.parse import unquote_to_bytes

        return unquote_to_bytes(encoded)
    except Exception as error:
        print(f"  解析验证码图片 data URI 失败：{error}")
        return None


def try_auto_solve_rotate_captcha(page: Page) -> bool:
    """尝试自动破解旋转验证码。

    先判断当前验证码类型，只有 captcha-wrapper__rotateslider 才走自动破解；
    流程：截图背景图+圆形图 → 图片算法计算角度 → 换算拖动距离 → 拖动滑块。
    任意步骤失败都返回 False，让调用方回退到人工处理。
    返回 True 表示验证码已通过、页面恢复。
    """
    try:
        # 确认验证码确实还在
        captcha = page.locator(CAPTCHA_SELECTOR).first
        if not captcha.is_visible():
            return False

        # 判断验证码类型：只有旋转验证码才走自动破解
        if not is_rotate_captcha(page):
            return False

        # ── 1. 定位并截图图片元素 ──
        bg_el = _find_captcha_element(
            page, CAPTCHA_BG_SELECTOR, CAPTCHA_BG_FALLBACK,
        )
        circle_el = _find_captcha_element(
            page, CAPTCHA_CIRCLE_SELECTOR, CAPTCHA_CIRCLE_FALLBACK,
        )
        if bg_el is None or circle_el is None:
            print("自动破解：无法定位验证码图片元素，回退人工。")
            return False

        print("自动破解：正在以 Base64 方式提取验证码图片...")
        bg_bytes = _extract_captcha_image_bytes(page, bg_el)
        circle_bytes = _extract_captcha_image_bytes(page, circle_el)
        if bg_bytes is None or circle_bytes is None:
            print("自动破解：Base64 提取验证码图片失败，回退人工。")
            return False

        bg = Image.open(BytesIO(bg_bytes)).convert("RGBA")
        circle = Image.open(BytesIO(circle_bytes)).convert("RGBA")
        print(f"  背景图：{bg.width}×{bg.height}  圆形图：{circle.width}×{circle.height}")

        # ── 2. 计算最佳旋转角度 ──
        print("自动破解：正在计算最佳旋转角度...")
        result = _calculate_angle(bg, circle)
        best_angle = result.best_angle
        print(f"  最佳 CSS 顺时针角度：{best_angle}°（NCC 分数={result.best_score:.3f}）")

        # 置信度阈值：分数太低说明匹配不可靠，强拖只会触发风控计数。
        # 直接回退人工，让人来滑一次比乱拖更好。
        if result.best_score < ROTATE_CAPTCHA_MIN_SCORE:
            print(
                f"自动破解：NCC 分数 {result.best_score:.3f} < "
                f"阈值 {ROTATE_CAPTCHA_MIN_SCORE:.3f}，放弃拖动，回退人工。"
            )
            return False

        # ── 3. 定位滑块组件 ──
        track = _find_captcha_element(
            page, CAPTCHA_TRACK_SELECTOR, CAPTCHA_TRACK_FALLBACK,
        )
        button = _find_captcha_element(
            page, CAPTCHA_BUTTON_SELECTOR, CAPTCHA_BUTTON_FALLBACK,
        )
        if track is None or button is None:
            print("自动破解：无法定位滑块元素，回退人工。")
            return False

        track_box = track.bounding_box()
        button_box = button.bounding_box()
        if track_box is None or button_box is None:
            print("自动破解：无法读取滑块尺寸，回退人工。")
            return False

        # ── 4. 换算拖动距离 ──
        max_distance = track_box["width"] - button_box["width"]
        if max_distance <= 0:
            print("自动破解：滑轨宽度异常，回退人工。")
            return False

        target_distance = (best_angle % 360) / 360.0 * max_distance
        print(f"  滑轨={track_box['width']:.0f}px 滑块={button_box['width']:.0f}px "
              f"最大行程={max_distance:.0f}px 目标={target_distance:.0f}px")

        # ── 存档：验证码 DOM 源码 + 计算角度 + 拖动距离，供事后分析 ──
        dump_rotate_captcha_info(
            page,
            best_angle=best_angle,
            best_score=result.best_score,
            target_distance=target_distance,
            max_distance=max_distance,
            track_box=track_box,
            button_box=button_box,
            bg_size=(bg.width, bg.height),
            circle_size=(circle.width, circle.height),
        )

        start_x = button_box["x"] + button_box["width"] / 2
        start_y = button_box["y"] + button_box["height"] / 2
        end_x = start_x + target_distance

        # ── 5. 执行拟人化拖动 ──
        print("自动破解：正在拟人化拖动滑块...")
        _human_drag_slider(page, start_x, start_y, end_x)

        # ── 6. 验证结果 ──
        try:
            page.wait_for_selector(
                VISIBLE_CAPTCHA_SELECTOR,
                state="hidden",
                timeout=CAPTCHA_VERIFY_TIMEOUT,
            )
            print("自动破解：验证码已消失，破解成功！")
            jitter_wait(page, 1_000)
            return True
        except PlaywrightTimeoutError:
            print("自动破解：拖动后验证码仍然存在，破解失败。")
            return False

    except Exception as exc:
        print(f"自动破解：发生异常，回退人工。{exc}")
        return False


_active_attempt: dict | None = None


def _recorded_solve(page, captcha_type, solver, challenge_id, attempt):
    global _active_attempt
    record = {
        "id": uuid.uuid4().hex, "challenge_id": challenge_id,
        "shop": SHOP_ID or "default", "type": captcha_type, "attempt": attempt,
        "started_at": datetime.now().isoformat(), "passed": False,
        "messages": [], "capture_errors": [],
    }
    artifacts = {}
    started = time.monotonic()
    try:
        selector = VISIBLE_TEXT_CAPTCHA_SELECTOR if captcha_type == "字符" else VISIBLE_PICTURE_CAPTCHA_SELECTOR
        element = page.locator(selector).last
        for name, capture in (
            ("element.html", lambda: element.evaluate("el => el.outerHTML").encode("utf-8")),
            ("captcha.png", lambda: element.screenshot(timeout=3000)),
        ):
            try:
                artifacts[name] = capture()
            except Exception as exc:
                record["capture_errors"].append(f"{name}: {type(exc).__name__}")
        _active_attempt = {"record": record, "artifacts": artifacts}
        record["passed"] = bool(solver(page))
        return record["passed"]
    finally:
        _active_attempt = None
        record["elapsed_seconds"] = round(time.monotonic() - started, 3)
        record["finished_at"] = datetime.now().isoformat()
        try:
            captcha_diagnostics.save_attempt(BASE_DIR / "debug" / "captcha_attempts", record, artifacts)
        except Exception as exc:
            print(f"[验证码诊断] 记录保存失败：{type(exc).__name__}: {exc}")


def _captcha_diagnostic(message: str) -> None:
    if _active_attempt is not None and len(_active_attempt["record"]["messages"]) < 64:
        _active_attempt["record"]["messages"].append(message[:2000])
    print(f"[验证码诊断] {message}", flush=True)
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        with (DEBUG_DIR / "captcha_diagnostics.log").open("a", encoding="utf-8") as stream:
            stream.write(f"{datetime.now().isoformat()} {message}\n")
    except OSError:
        pass


def _normalize_text_captcha_result(value: str) -> str:
    """只接受可直接填入验证码输入框的字母数字结果。"""
    text = str(value or "").strip().strip("`").strip()
    if not text:
        return ""
    compact = re.sub(r"\s+", "", text)
    if compact.upper() == "UNKNOWN":
        return ""
    if not re.fullmatch(r"[A-Za-z0-9]{1,16}", compact):
        return ""
    return compact


class CaptchaRefreshError(RuntimeError):
    """没有确认换题成功，禁止再次识别旧图。"""


_CAPTCHA_REFRESH_STATE_JS = """() => {
    const visible = el => !!(el && el.getClientRects().length &&
        getComputedStyle(el).visibility !== 'hidden');
    const containers = [...document.querySelectorAll('#slide-captcha-dialog, .captcha-wrapper')].filter(visible);
    const box = containers[containers.length - 1];
    if (!box) return {kind: 'none', sources: [], ready: false};
    const kind = box.classList.contains('captcha-wrapper__text') ? '字符' :
        box.classList.contains('captcha-wrapper__picture') ? '点选' :
        box.classList.contains('captcha-wrapper__rotateslider') ? '旋转' : '未知';
    // captchaImg 是刷新图标，不能作为验证码原图判断是否换题。
    const images = [...box.querySelectorAll('.captcha-source img, #captchaClickImg, .rotateslider-img-bg img, img.rotateslider-img-bg')];
    return {kind, sources: images.map(img => img.currentSrc || img.src),
        question: (box.querySelector('.picture-text')?.textContent || '').trim(),
        ready: images.length > 0 && images.every(img => img.complete && img.naturalWidth > 0)};
}"""


def _refresh_and_confirm_captcha(page: Page, label: str) -> None:
    """重新定位当前容器，最多点击两次，每次等五秒确认换图或换题型。"""
    network_events = []
    def network(event, phase):
        try:
            url = urlsplit(event.url)
            if any(word in url.path.lower() for word in ("captcha", "verify", "challenge")) and len(network_events) < 20:
                network_events.append({"phase": phase, "path": url.path,
                                       "status": getattr(event, "status", None) if phase == "response" else None})
        except Exception:
            pass
    def requested(request):
        network(request, "request")
    def responded(response):
        network(response, "response")
    def failed(request):
        network(request, "requestfailed")
    def capture(number, phase):
        if _active_attempt is None:
            return
        try:
            box = page.locator(VISIBLE_CAPTCHA_SELECTOR).last
            prefix = f"refresh_{number}_{phase}"
            _active_attempt["artifacts"][prefix + ".png"] = box.screenshot(timeout=1500)
            _active_attempt["artifacts"][prefix + ".html"] = box.evaluate("el => el.outerHTML").encode("utf-8")
        except Exception as exc:
            _active_attempt["record"]["capture_errors"].append(f"refresh_{phase}: {type(exc).__name__}")
    page.on("request", requested)
    page.on("response", responded)
    page.on("requestfailed", failed)
    try:
        before = page.evaluate(_CAPTCHA_REFRESH_STATE_JS)
        if before["kind"] == "none":
            _captcha_diagnostic(f"{label}刷新前验证码已消失")
            return
        for click_attempt in range(1, 3):
            # 刷新可能重建 DOM 或切换题型，不能复用旧题型的容器。
            container = page.locator(VISIBLE_CAPTCHA_SELECTOR).last
            button = container.locator(CAPTCHA_REFRESH_SELECTOR + ":visible").first
            if button.count() == 0:
                raise CaptchaRefreshError("当前验证码未找到可见刷新按钮")
            _captcha_diagnostic(f"{label}找到刷新按钮，准备第 {click_attempt}/2 次点击")
            capture(click_attempt, "before")
            icon = button.locator("img:visible").first
            target = icon if icon.count() else button
            _captcha_diagnostic("刷新点击目标：" + ("按钮内的图片图标" if target is icon else "外层按钮（未找到图片图标）"))
            target.click(timeout=3_000)
            _captcha_diagnostic(f"{label}已点击刷新按钮，等待新图片加载（最多5秒）")
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                after = page.evaluate(_CAPTCHA_REFRESH_STATE_JS)
                if after["kind"] != "none" and after["kind"] != before["kind"]:
                    capture(click_attempt, "after")
                    _captcha_diagnostic(f"刷新已生效：验证码由{before['kind']}变为{after['kind']}，下一轮重新判断题型")
                    return
                if after["ready"] and after["sources"] != before["sources"]:
                    capture(click_attempt, "after")
                    _captcha_diagnostic("刷新已生效：验证码图片已变化且加载完成")
                    return
                page.wait_for_timeout(200)
            capture(click_attempt, "after")
            _captcha_diagnostic("刷新尚未确认生效：未检测到新图片或新题型")
        raise CaptchaRefreshError("两次点击刷新后仍未确认换题，跳过当前订单，避免重复识别旧图片")
    except Exception as exc:
        _captcha_diagnostic(f"验证码刷新未完成：{type(exc).__name__}: {exc}")
        if isinstance(exc, CaptchaRefreshError):
            raise
        raise CaptchaRefreshError("验证码刷新异常，跳过当前订单") from exc
    finally:
        page.remove_listener("request", requested)
        page.remove_listener("response", responded)
        page.remove_listener("requestfailed", failed)
        _captcha_diagnostic("刷新期间相关请求记录：" + json.dumps(network_events, ensure_ascii=False))
        if _active_attempt is not None:
            _active_attempt["record"].setdefault("refresh_network", []).extend(network_events)


def _refresh_text_captcha(page: Page, captcha: Locator) -> None:
    _refresh_and_confirm_captcha(page, "字符验证码")


def _same_captcha_after_verification(page: Page, before: dict) -> bool:
    """平台可能在验证失败后自动换题；只有原题仍在时才需要手动刷新。"""
    after = page.evaluate(_CAPTCHA_REFRESH_STATE_JS)
    if after["kind"] == "none":
        _captcha_diagnostic("等待后验证码已消失，无需刷新")
        return False
    if any(after.get(key) != before.get(key) for key in ("kind", "sources", "question")):
        _captcha_diagnostic("等待5秒后验证码已变化，无需点击刷新，下一轮重新识别当前题型")
        return False
    _captcha_diagnostic("等待5秒后验证码仍与操作前相同，准备点击刷新")
    return True


def _refresh_visible_captcha(page: Page, captcha_type: str) -> None:
    """刷新当前仍可见的验证码，供没有容器引用的处理流程使用。"""
    try:
        visible = page.locator(VISIBLE_CAPTCHA_SELECTOR)
        if visible.count() == 0:
            return
        refresh = visible.last.locator(CAPTCHA_REFRESH_SELECTOR)
        if refresh.count() == 0 or not refresh.first.is_visible():
            _captcha_diagnostic(f"{captcha_type}验证码仍存在，但未找到刷新按钮")
            return
        refresh.first.click()
        jitter_wait(page, 700)
        _captcha_diagnostic(f"{captcha_type}验证码 5 秒后仍存在，已点击刷新")
    except Exception as exc:
        _captcha_diagnostic(
            f"刷新{captcha_type}验证码失败：{type(exc).__name__}: {exc}"
        )


def try_auto_solve_text_captcha(page: Page) -> bool:
    """尝试自动破解字符验证码。

    流程：截图验证码图片 → AI 识别字符 → 点击输入框 → 模拟打字输入。
    任意步骤失败都返回 False，让调用方回退到人工处理。
    """
    stage = "检查验证码容器"
    captcha: Locator | None = None
    try:
        visible_captchas = page.locator(VISIBLE_TEXT_CAPTCHA_SELECTOR)
        if visible_captchas.count() == 0:
            _captcha_diagnostic(f"{stage}：容器不可见，尚未输入")
            return False
        captcha = visible_captchas.last

        # ── 1. 提取验证码原图 ──
        stage = "定位验证码图片"
        img_el = captcha.locator(TEXT_CAPTCHA_IMG_SELECTOR)
        if img_el.count() == 0:
            _captcha_diagnostic(f"{stage}失败，尚未输入")
            _refresh_text_captcha(page, captcha)
            return False

        print("字符验证码：正在提取原图并 AI 识别...")
        stage = "提取验证码原图"
        img_bytes = _extract_captcha_image_bytes(page, img_el.first)
        if img_bytes is None:
            _captcha_diagnostic("原图提取失败，改用元素截图")
            img_bytes = img_el.first.screenshot()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        data_uri = f"data:image/png;base64,{img_b64}"

        # ── 2. AI 识别 ──
        stage = "调用识别接口"
        try:
            raw_result = _ai_recognize_text_captcha(data_uri)
        except Exception as exc:
            if _is_captcha_ai_timeout(exc):
                _captcha_diagnostic(
                    "字符验证码 AI 识别超过 60 秒，正在刷新验证码后重试"
                )
                _refresh_text_captcha(page, captcha)
                return False
            raise
        result = _normalize_text_captcha_result(raw_result)
        if not result:
            _captcha_diagnostic(
                f"{stage}未返回可填写的纯字母数字结果，尚未输入；"
                f"AI 原始返回={raw_result!r}"
            )
            _refresh_text_captcha(page, captcha)
            return False

        print(f"字符验证码：AI 识别结果 → \"{result}\"")

        # ── 3. 点击输入框并输入 ──
        stage = "定位输入框"
        input_el = captcha.locator(TEXT_CAPTCHA_INPUT_SELECTOR)
        if input_el.count() == 0:
            _captcha_diagnostic(f"{stage}失败，尚未输入")
            _refresh_text_captcha(page, captcha)
            return False

        target_input = input_el.first
        before_verification = page.evaluate(_CAPTCHA_REFRESH_STATE_JS)
        stage = "移动鼠标并点击输入框"
        human_click(page, target_input)
        jitter_wait(page, 200)
        stage = "清空输入框"
        target_input.press("Control+A")
        target_input.press("Backspace")
        stage = "输入字符"
        human_type(page, target_input, result, click_first=False)
        _captcha_diagnostic(f"已自动输入 AI 识别结果 {result!r}，等待平台验证")
        stage = "等待平台验证"

        # ── 4. 验证结果 ──
        try:
            page.wait_for_selector(
                VISIBLE_CAPTCHA_SELECTOR,
                state="hidden",
                timeout=CAPTCHA_VERIFY_TIMEOUT,
            )
            print("字符验证码：验证码已消失，破解成功！")
            jitter_wait(page, 500)
            return True
        except PlaywrightTimeoutError:
            _captcha_diagnostic("输入后验证码仍然存在，未确认通过")
            if _same_captcha_after_verification(page, before_verification):
                _refresh_text_captcha(page, captcha)
            return False

    except CaptchaRefreshError:
        raise
    except Exception as exc:
        _captcha_diagnostic(f"{stage}异常：{type(exc).__name__}: {exc}")
        if captcha is not None:
            _refresh_text_captcha(page, captcha)
        return False


def _refresh_picture_captcha(page: Page, captcha: Locator) -> None:
    _refresh_and_confirm_captcha(page, "点选验证码")


def _dump_picture_captcha_info(
    *,
    question: str,
    result: dict[str, object],
    image_box: dict[str, float],
    click_x: float,
    click_y: float,
) -> Path | None:
    """保存不含图片内容的点选定位参数，供误点或泄漏排查。"""
    try:
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]
        path = DEBUG_DIR / f"picture_captcha_{timestamp}.json"
        path.write_text(
            json.dumps(
                {
                    "timestamp": timestamp,
                    "question": question,
                    "model_result": result,
                    "image_box": image_box,
                    "viewport_click": {"x": click_x, "y": click_y},
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path
    except Exception as exc:
        _captcha_diagnostic(f"保存点选验证码定位参数失败：{type(exc).__name__}: {exc}")
        return None


def try_auto_solve_picture_captcha(page: Page) -> bool:
    """用视觉模型定位点选目标，换算为当前图片坐标并点击。"""
    stage = "检查点选验证码容器"
    captcha: Locator | None = None
    try:
        visible_captchas = page.locator(VISIBLE_PICTURE_CAPTCHA_SELECTOR)
        if visible_captchas.count() == 0:
            _captcha_diagnostic(f"{stage}：容器不可见，尚未点击")
            return False
        captcha = visible_captchas.last

        stage = "读取点选题目"
        question_el = captcha.locator(PICTURE_CAPTCHA_QUESTION_SELECTOR)
        if question_el.count() == 0:
            raise RuntimeError("未找到题目元素")
        question = question_el.first.inner_text().strip()
        if not question:
            raise RuntimeError("题目为空")

        stage = "定位点选图片"
        image_el = captcha.locator(PICTURE_CAPTCHA_IMG_SELECTOR)
        if image_el.count() == 0:
            raise RuntimeError("未找到点选图片")
        image = image_el.first
        image.scroll_into_view_if_needed()

        # 使用元素截图而非原图 src。模型看到的像素与元素显示区域一一对应，
        # 页面缩放、CSS 尺寸和 devicePixelRatio 都不会改变比例换算。
        stage = "截取点选图片"
        image_bytes = image.screenshot()
        data_uri = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")

        stage = "调用点选识别接口"
        try:
            result = _ai_locate_picture_captcha(data_uri, question)
        except Exception as exc:
            if _is_captcha_ai_timeout(exc):
                _captcha_diagnostic("点选验证码 AI 识别超过 60 秒，正在刷新后重试")
            else:
                _captcha_diagnostic(f"{stage}失败：{type(exc).__name__}: {exc}")
            _refresh_picture_captcha(page, captcha)
            return False

        confidence = float(result["confidence"])
        if confidence < PICTURE_CAPTCHA_MIN_CONFIDENCE:
            _captcha_diagnostic(
                f"点选定位置信度 {confidence:.3f} 低于阈值 "
                f"{PICTURE_CAPTCHA_MIN_CONFIDENCE:.3f}，刷新后重试"
            )
            _refresh_picture_captcha(page, captcha)
            return False

        stage = "换算点击坐标"
        # 模型请求期间页面可能发生布局变化，所以在点击前重新读取元素框。
        image_box = image.bounding_box()
        if image_box is None:
            raise RuntimeError("点选图片不可见")
        box_values = tuple(float(image_box[key]) for key in ("x", "y", "width", "height"))
        if not all(math.isfinite(value) for value in box_values):
            raise RuntimeError("点选图片位置包含非有限数值")
        if image_box["width"] <= 0 or image_box["height"] <= 0:
            raise RuntimeError("点选图片尺寸异常")
        point = result["target_point_2d"]
        point_x, point_y = float(point[0]), float(point[1])
        click_x = image_box["x"] + point_x / 1000 * image_box["width"]
        click_y = image_box["y"] + point_y / 1000 * image_box["height"]

        info_path = _dump_picture_captcha_info(
            question=question,
            result=result,
            image_box=image_box,
            click_x=click_x,
            click_y=click_y,
        )
        _captcha_diagnostic(
            f"点选题目={question!r}，目标={result['target_name']!r}，"
            f"置信度={confidence:.3f}，点击=({click_x:.1f}, {click_y:.1f})，"
            f"参数={info_path or '保存失败'}"
        )

        stage = "点击目标"
        before_verification = page.evaluate(_CAPTCHA_REFRESH_STATE_JS)
        page.mouse.move(click_x, click_y)
        jitter_wait(page, 150)
        page.mouse.click(click_x, click_y)
        stage = "等待平台验证"
        try:
            page.wait_for_selector(
                VISIBLE_CAPTCHA_SELECTOR,
                state="hidden",
                timeout=CAPTCHA_VERIFY_TIMEOUT,
            )
            print("点选验证码：验证码已消失，破解成功！")
            jitter_wait(page, 500)
            return True
        except PlaywrightTimeoutError:
            _captcha_diagnostic("点击后验证码仍然存在，检查是否已自动换题")
            if _same_captcha_after_verification(page, before_verification):
                _refresh_picture_captcha(page, captcha)
            return False
    except CaptchaRefreshError:
        raise
    except Exception as exc:
        _captcha_diagnostic(f"{stage}异常：{type(exc).__name__}: {exc}")
        if captcha is not None:
            _refresh_picture_captcha(page, captcha)
        return False


def _ai_locate_picture_captcha(
    data_uri: str,
    question: str,
) -> dict[str, object]:
    """使用 qwen_client 定位点选目标，单次最多等待 60 秒。"""
    import qwen_client

    if _active_attempt is not None:
        _active_attempt["artifacts"]["image.png"] = base64.b64decode(data_uri.split(",", 1)[1])
        _active_attempt["record"]["question"] = question

    env_file = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_file, override=False)
    api_key = os.getenv("QWEN_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"未找到 QWEN_API_KEY（已检查系统环境变量和 {env_file}）；"
            "点选验证码需要配置千问接口密钥"
        )
    with qwen_client.create_client(api_key=api_key, timeout=60) as client:
        result = qwen_client.locate_geometry_captcha_data_uri(
            client,
            data_uri,
            question,
            enable_thinking=False,
        )
        if _active_attempt is not None:
            _active_attempt["record"]["model_result"] = result
        return result


def _ai_recognize_text_captcha(data_uri: str) -> str:
    """使用 qwen_client 的千问模型识别验证码，单次最多等待 60 秒。"""
    import qwen_client

    if _active_attempt is not None:
        _active_attempt["artifacts"]["image.png"] = base64.b64decode(data_uri.split(",", 1)[1])

    # GUI -> 调度器 -> 店铺 worker 是多层子进程。不能指望最外层 GUI 已经把
    # 项目 .env 注入系统环境；每个真正调用识别接口的 worker 都在这里按
    # captcha.py 所在目录显式加载一次，且不覆盖用户已经设置的系统变量。
    env_file = Path(__file__).resolve().parent / ".env"
    load_dotenv(dotenv_path=env_file, override=False)
    api_key = os.getenv("QWEN_API_KEY")
    if not api_key:
        raise RuntimeError(
            f"未找到 QWEN_API_KEY（已检查系统环境变量和 {env_file}）；"
            "字符验证码现已改用 qwen_client.py，请配置千问接口密钥"
        )

    with qwen_client.create_client(api_key=api_key, timeout=60) as client:
        result = qwen_client.recognize_captcha_data_uri(
            client,
            data_uri,
            enable_thinking=False,
        )
        if _active_attempt is not None:
            _active_attempt["record"]["model_result"] = result
        return result


def _is_captcha_ai_timeout(exc: BaseException) -> bool:
    """识别 OpenAI/httpx 包装前后的超时异常。"""
    try:
        import httpx
        from openai import APITimeoutError

        timeout_types = (APITimeoutError, httpx.TimeoutException, TimeoutError)
    except ImportError:
        timeout_types = (TimeoutError,)

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, timeout_types):
            return True
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return False


# 模块级开关：是否启用自动破解验证码（由 main.py 根据命令行参数设置）
_auto_solve_captcha = True

# 模块级开关：兼容旧调用方。自动模式下所有验证码五次失败均跳过当前任务；
# 人工模式仍从一开始等待人工处理。
_give_up_after_auto_solve_fails = False


def set_auto_solve_captcha(enabled: bool) -> None:
    """外部设置是否开启自动破解验证码。"""
    global _auto_solve_captcha
    _auto_solve_captcha = enabled
    mode = "自动破解" if enabled else "人工处理"
    print(f"[验证码模式] 已设置为：{mode}")


def set_give_up_after_auto_solve_fails(enabled: bool) -> None:
    """兼容旧调用方；自动模式现在统一在五次失败后跳过当前订单。"""
    global _give_up_after_auto_solve_fails
    _give_up_after_auto_solve_fails = enabled
    print("[验证码模式] 自动处理 5 次失败后：跳过当前订单")


@without_business_pacing
def ensure_no_captcha(
    page: Page,
    timeout: int = CAPTCHA_WAIT_TIMEOUT,
) -> None:
    """
    关键步骤前后调用：字符验证码由 AI 分析并自动填写，点选验证码由 AI
    分析目标坐标并自动点击。自动模式下所有类型共用五次尝试，五次未通过
    就抛出异常并跳过当前订单；人工模式仍等待人工处理。异常会由批量流程
    按单捕获，不会中断整个批次。
    """
    if not is_captcha_visible(page):
        return

    if not _auto_solve_captcha:
        print("验证码模式为人工处理，跳过自动破解。")
        print("请在浏览器中完成验证码，程序将在验证码通过后自动继续。")
        if not wait_for_human_captcha(page, timeout=timeout):
            raise RuntimeError(
                "验证码在等待时间内没有被处理完成。"
            )
        return

    # 刷新可能改变题型；所有题型共享尝试次数，每轮重新分派。
    challenge_id = uuid.uuid4().hex
    previous_type = None
    for attempt in range(1, CAPTCHA_AUTO_MAX_ATTEMPTS + 1):
        if not is_captcha_visible(page):
            return
        if is_picture_captcha(page):
            captcha_type, solver = "点选", try_auto_solve_picture_captcha
        elif is_text_captcha(page):
            captcha_type, solver = "字符", try_auto_solve_text_captcha
        elif is_rotate_captcha(page):
            captcha_type, solver = "旋转", try_auto_solve_rotate_captcha
        else:
            captcha_type, solver = "未知", None
            _captcha_diagnostic(
                f"未知验证码开始第 {attempt}/{CAPTCHA_AUTO_MAX_ATTEMPTS} 次自动处理"
            )
            _refresh_visible_captcha(page, captcha_type)
            shot = save_captcha_screenshot(page)
            _captcha_diagnostic(
                f"第 {attempt}/{CAPTCHA_AUTO_MAX_ATTEMPTS} 次未完成；"
                f"现场截图：{shot or '保存失败'}"
            )
            previous_type = captcha_type
            if attempt < CAPTCHA_AUTO_MAX_ATTEMPTS:
                jitter_wait(page, 500)
            continue

        if previous_type is not None and previous_type != captcha_type:
            _captcha_diagnostic(
                f"验证码类型由{previous_type}变为{captcha_type}，切换处理方式，累计次数不重置"
            )
        previous_type = captcha_type
        _captcha_diagnostic(
            f"{captcha_type}验证码开始第 {attempt}/{CAPTCHA_AUTO_MAX_ATTEMPTS} 次自动处理"
        )
        passed = (
            _recorded_solve(page, captcha_type, solver, challenge_id, attempt)
            if captcha_type in ("字符", "点选") else solver(page)
        )
        if passed:
            print(f"{captcha_type}验证码自动处理成功。")
            return
        if not is_captcha_visible(page):
            return
        # 字符和点选处理函数内部已经刷新；旋转在这里刷新一次。
        if captcha_type == "旋转":
            _refresh_visible_captcha(page, captcha_type)
        shot = save_captcha_screenshot(page)
        _captcha_diagnostic(
            f"第 {attempt}/{CAPTCHA_AUTO_MAX_ATTEMPTS} 次未完成；现场截图：{shot or '保存失败'}"
        )
        if attempt < CAPTCHA_AUTO_MAX_ATTEMPTS:
            jitter_wait(page, 500)

    if not is_captcha_visible(page):
        return
    _captcha_diagnostic(
        "验证码自动处理 5 次均失败，跳过当前订单"
    )
    raise RuntimeError("验证码自动处理 5 次均失败，跳过当前订单。")
