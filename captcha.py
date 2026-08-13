"""滑块/字符/旋转验证码处理。

从 pdd_chat.py 抽出：验证码是 PDD 后台随时可能弹的横切能力，
应被所有页面操作共用，与"聊天页"无关。
"""

from __future__ import annotations

import base64
import json
import math
import os
import random
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image
from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from calculate_demo_angle import calculate_angle as _calculate_angle
from paths import DEBUG_DIR
from stealth import human_type, jitter_wait

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

# 旋转验证码专用选择器（区别于普通滑块/点选验证码）
ROTATE_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__rotateslider, "
    ".captcha-wrapper__rotateslider"
)
# 字符验证码专用选择器
TEXT_CAPTCHA_SELECTOR = (
    "#slide-captcha-dialog.captcha-wrapper__text, "
    ".captcha-wrapper__text"
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
# 备用选择器（容器内泛化搜索，覆盖未来版本变化）
CAPTCHA_BG_FALLBACK = "img:first-of-type, canvas:first-of-type"
CAPTCHA_CIRCLE_FALLBACK = "img:nth-of-type(2), canvas:nth-of-type(2)"
CAPTCHA_TRACK_FALLBACK = "[class*='handle'], [class*='track']"
CAPTCHA_BUTTON_FALLBACK = "[class*='button']:not([type='submit'])"

# 等待人工滑完的最长时间：5 分钟
CAPTCHA_WAIT_TIMEOUT = 5 * 60 * 1000


def is_captcha_visible(page: Page) -> bool:
    """
    检测当前页面上是否弹出了滑块验证码。

    这个函数会在每个关键步骤后被频繁调用，所以：
    - 只做存在性判断，不做任何等待，开销必须接近于零；
    - 全程兜底异常。页面正在跳转时 locator 可能抛错，
      检测函数自己绝不能成为新的崩溃来源。
    """

    try:
        dialog = page.locator(CAPTCHA_SELECTOR)

        if dialog.count() == 0:
            return False

        return dialog.first.is_visible()

    except Exception:
        return False


def is_rotate_captcha(page: Page) -> bool:
    """检测当前验证码是否是旋转验证码（可自动破解的类型）。

    拼多多存在多种验证码（普通滑块、点选图形、旋转拼图），
    只有旋转验证码（容器带 captcha-wrapper__rotateslider 类）
    才能用图片角度识别方案自动破解。
    """
    try:
        dialog = page.locator(ROTATE_CAPTCHA_SELECTOR)
        if dialog.count() == 0:
            return False

        return dialog.first.is_visible()

    except Exception:
        return False


def is_text_captcha(page: Page) -> bool:
    """检测当前验证码是否是字符验证码（容器带 captcha-wrapper__text 类）。"""
    try:
        dialog = page.locator(TEXT_CAPTCHA_SELECTOR)
        if dialog.count() == 0:
            return False

        return dialog.first.is_visible()

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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

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
    print("检测到滑块验证码，自动化已暂停。")
    print("请在打开的浏览器窗口中手动完成滑块拼图。")
    print("滑完后程序会自动继续，不需要回到控制台按键。")
    print(f"最长等待 {wait_text}。")

    if screenshot_path:
        print(f"当前页面截图：{screenshot_path}")

    print("=" * 60)

    # 控制台响铃，提醒可能已经离开电脑的人回来处理
    print("\a", end="", flush=True)

    try:
        # state="hidden" 同时覆盖"节点被移除"和"节点还在但隐藏"两种消失方式
        page.wait_for_selector(
            CAPTCHA_SELECTOR,
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
                CAPTCHA_SELECTOR,
                state="hidden",
                timeout=5_000,
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


def try_auto_solve_text_captcha(page: Page) -> bool:
    """尝试自动破解字符验证码。

    流程：截图验证码图片 → AI 识别字符 → 点击输入框 → 模拟打字输入。
    任意步骤失败都返回 False，让调用方回退到人工处理。
    """
    try:
        captcha = page.locator(CAPTCHA_SELECTOR).first
        if not captcha.is_visible():
            return False

        if not is_text_captcha(page):
            return False

        # ── 1. 截图验证码图片 ──
        img_el = captcha.locator(TEXT_CAPTCHA_IMG_SELECTOR)
        if img_el.count() == 0:
            print("字符验证码：无法定位验证码图片。")
            return False

        print("字符验证码：正在截图并 AI 识别...")
        img_bytes = img_el.first.screenshot()
        img_b64 = base64.b64encode(img_bytes).decode("ascii")
        data_uri = f"data:image/png;base64,{img_b64}"

        # ── 2. AI 识别 ──
        result = _ai_recognize_text_captcha(data_uri)
        if not result:
            print("字符验证码：AI 识别结果为空，回退人工。")
            return False

        print(f"字符验证码：AI 识别结果 → \"{result}\"")

        # ── 3. 点击输入框并输入 ──
        input_el = captcha.locator(TEXT_CAPTCHA_INPUT_SELECTOR)
        if input_el.count() == 0:
            print("字符验证码：无法定位输入框。")
            return False

        input_el.first.click()
        jitter_wait(page, 200)
        # human_type 签名是 (page, locator, text, ...)，之前少传 page 导致每次 TypeError
        # 被下面 except Exception 吞掉，误报"AI 识别失败"。
        human_type(page, input_el.first, result)

        # ── 4. 验证结果 ──
        jitter_wait(page, 500)
        try:
            page.wait_for_selector(
                CAPTCHA_SELECTOR,
                state="hidden",
                timeout=5_000,
            )
            print("字符验证码：验证码已消失，破解成功！")
            jitter_wait(page, 500)
            return True
        except PlaywrightTimeoutError:
            # 验证码输入后可能再过几秒才生效，多等一会
            page.wait_for_timeout(2_000)
            if not is_captcha_visible(page):
                print("字符验证码：延迟检测到验证码消失，破解成功！")
                return True
            print("字符验证码：输入后验证码仍然存在，破解失败。")
            return False

    except Exception as exc:
        print(f"字符验证码：发生异常，回退人工。{exc}")
        return False


def _ai_recognize_text_captcha(data_uri: str) -> str:
    """将验证码图片 data URI 发送给 AI 模型识别，返回识别出的文字。"""
    import os

    from openai import OpenAI

    api_key = os.getenv("ARK_API_KEY")
    if not api_key:
        print("字符验证码：未设置 ARK_API_KEY 环境变量，无法识别。")
        return ""

    client = OpenAI(
        base_url="https://ark.cn-beijing.volces.com/api/v3",
        api_key=api_key.strip(),
        timeout=30,
        max_retries=1,
    )

    response = client.chat.completions.create(
        model="ep-20260811104144-2xf9c",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_uri}},
                    {
                        "type": "text",
                        "text": (
                            "识别图片中的字母和数字。"
                            "忽略图标、卡通装饰和背景纹理。"
                            "严格区分大小写，只输出识别结果，"
                            "不要输出解释、标点或其他内容。"
                        ),
                    },
                ],
            }
        ],
        temperature=0,
        max_tokens=30,
    )

    content = response.choices[0].message.content
    return content.strip() if content else ""


# 模块级开关：是否启用自动破解验证码（由 main.py 根据命令行参数设置）
_auto_solve_captcha = True


def set_auto_solve_captcha(enabled: bool) -> None:
    """外部设置是否开启自动破解验证码。"""
    global _auto_solve_captcha
    _auto_solve_captcha = enabled
    mode = "自动破解" if enabled else "人工处理"
    print(f"[验证码模式] 已设置为：{mode}")


def ensure_no_captcha(
    page: Page,
    timeout: int = CAPTCHA_WAIT_TIMEOUT,
) -> None:
    """
    关键步骤前后调用：有验证码就先尝试自动破解，失败再等人工。
    等待超时会抛出异常，在批量流程里会被单个订单捕获，不会中断整个批次。
    """
    if not is_captcha_visible(page):
        return

    if not _auto_solve_captcha:
        print("验证码模式为人工处理，跳过自动破解。")
        print("请在浏览器中完成验证码，程序将在验证码通过后自动继续。")
        if not wait_for_human_captcha(page, timeout=timeout):
            raise RuntimeError(
                "滑块验证码在等待时间内没有被处理完成。"
            )
        return

    # 字符验证码？AI 识别后填入，最多重试 3 次
    if is_text_captcha(page):
        for attempt in range(1, 4):
            print(f"尝试 AI 识别字符验证码...（第 {attempt}/3 次）")
            if try_auto_solve_text_captcha(page):
                print("字符验证码自动破解成功。")
                return
            if attempt < 3 and is_captcha_visible(page):
                print(f"第 {attempt} 次 AI 识别失败，准备重试...")
                jitter_wait(page, 500)

        print("字符验证码 3 次均失败，回退到人工处理模式。")
        if not wait_for_human_captcha(page, timeout=timeout):
            raise RuntimeError(
                "滑块验证码在等待时间内没有被处理完成。"
            )
        return

    # 旋转验证码：图片角度识别 + 拖动
    if is_rotate_captcha(page):
        for attempt in range(1, 4):
            print(f"尝试自动破解旋转验证码...（第 {attempt}/3 次）")
            if try_auto_solve_rotate_captcha(page):
                print("旋转验证码自动破解成功。")
                return
            if attempt < 3 and is_captcha_visible(page):
                print(f"第 {attempt} 次破解失败，准备重试...")
                jitter_wait(page, 500)

        print("旋转验证码 3 次均失败，回退到人工处理模式。")
    else:
        print("当前弹出未知类型的验证码，无法自动破解。")

    # 回退人工
    if not wait_for_human_captcha(page, timeout=timeout):
        raise RuntimeError(
            "滑块验证码在等待时间内没有被处理完成。"
        )
