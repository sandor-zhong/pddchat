from __future__ import annotations

import base64
import json
import math
import random
import re
from io import BytesIO
from pathlib import Path

from datetime import datetime

from PIL import Image
from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

from calculate_demo_angle import calculate_angle as _calculate_angle
from stealth import create_stealth_context, human_delay, human_type, jitter_wait


# ============================================================
# 基础配置
# ============================================================

START_URL = (
    "https://mms.pinduoduo.com/"
    "mms-chat/search?msfrom=mms_sidenav"
)

BASE_DIR = Path(__file__).resolve().parent

# 浏览器登录状态保存位置
PROFILE_DIR = BASE_DIR / "state" / "pdd_chat_profile"

# 聊天记录保存位置
OUTPUT_DIR = BASE_DIR / "output"

# 查询失败截图保存位置
DEBUG_DIR = BASE_DIR / "debug"

# 每一页聊天记录凭证截图
EVIDENCE_DIR = BASE_DIR / "evidence"


# 默认测试订单编号
DEFAULT_ORDER_NO = "260430-189593086262417"


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

# 任何 beast-core 弹窗都可能盖在按钮上，让点击打不中。
# data-testid 不带版本哈希，是稳定锚点。
BLOCKING_MODAL_SELECTOR = '[data-testid="beast-core-modal"]'

# 只关"通知型"弹窗：右上角的 X，以及"知道了"这类纯确认按钮。
# 绝不点提交、确认、删除、同意之类会产生真实后果的按钮。
DISMISS_SELECTORS = (
    '[data-testid="beast-core-modal-icon-close"]',
    'button:has-text("知道了")',
    'button:has-text("我知道了")',
    'button:has-text("关闭")',
)


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
        print(f"  最佳 CSS 顺时针角度：{best_angle}°（误差={result.best_score:.2f}）")

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
        human_type(input_el.first, result)

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


# ============================================================
# 弹窗清障
# ============================================================
#
# 挡住点击的不只有验证码，还有"新消息"通知面板、"知道了"提示框
# 这类普通弹窗。它们不是风控机制，可以直接关掉；验证码则必须交给
# 人工。下面按这个原则分类处理。

def find_blocking_modal(page: Page) -> Locator | None:
    """找出当前可见的、可能遮挡点击的弹窗。没有则返回 None。"""

    try:
        modal = page.locator(BLOCKING_MODAL_SELECTOR)

        count = modal.count()

        for index in range(count):
            candidate = modal.nth(index)

            if candidate.is_visible():
                return candidate

        return None

    except Exception:
        return None


def dismiss_notification_modal(page: Page) -> bool:
    """
    自动关掉【非验证码】的通知类弹窗。

    两条硬性约束：
    - 弹窗里只要含验证码，一律不碰，返回 False 交给人工；
    - 只点关闭类控件（X / 知道了），绝不触碰弹窗里的其它按钮，
      避免误触"提交""确认"这种会产生真实后果的操作。
    """

    if is_captcha_visible(page):
        return False

    modal = find_blocking_modal(page)

    if modal is None:
        return False

    for selector in DISMISS_SELECTORS:
        try:
            closer = modal.locator(selector)

            if closer.count() == 0 or not closer.first.is_visible():
                continue

            closer.first.click(timeout=5_000)
            jitter_wait(page, 500)

            print(f"已自动关闭挡住操作的弹窗（{selector}）。")

            return True

        except Exception:
            continue

    return False


def dump_blocker(page: Page, reason: str) -> None:
    """
    把认不出来的遮挡物存档，供事后分析。

    这样下次再遇到新样式的弹窗时不用靠猜——直接看存档就知道
    它到底是什么、该怎么识别。
    """

    DEBUG_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        modal = find_blocking_modal(page)

        if modal is not None:
            html = modal.evaluate("element => element.outerHTML")

            html_path = DEBUG_DIR / f"blocker_{timestamp}.html"

            html_path.write_text(html, encoding="utf-8")

            print(f"遮挡物 HTML 已存档：{html_path}")

    except Exception as error:
        print(f"保存遮挡物 HTML 失败：{error}")

    try:
        screenshot_path = DEBUG_DIR / f"blocker_{timestamp}.png"

        page.screenshot(
            path=str(screenshot_path),
            full_page=True,
        )

        print(f"遮挡物截图已存档：{screenshot_path}（原因：{reason}）")

    except Exception as error:
        print(f"保存遮挡物截图失败：{error}")


def clear_blockers(
    page: Page,
    timeout: int = CAPTCHA_WAIT_TIMEOUT,
) -> None:
    """
    统一清障入口：把挡在页面上的东西按类别处理干净。

    优先级：
    1. 验证码      -> 暂停，等人工滑完（绝不代劳）
    2. 通知类弹窗  -> 自动关掉
    3. 认不出的遮挡物 -> 存档 + 响铃，等人工处理
    4. 干净        -> 立刻返回，几乎零开销
    """

    if is_captcha_visible(page):
        ensure_no_captcha(page, timeout=timeout)
        return

    if find_blocking_modal(page) is None:
        return

    if dismiss_notification_modal(page):
        return

    # 关不掉又不是验证码：存证后交给人工，不要闷头重试到超时
    dump_blocker(page, reason="无法自动关闭的弹窗")

    wait_text = describe_timeout(timeout)

    print()
    print("=" * 60)
    print("有弹窗挡住了页面操作，且无法自动关闭。")
    print("请在浏览器窗口中手动处理掉它。")
    print("处理完程序会自动继续，不需要回控制台按键。")
    print(f"最长等待 {wait_text}。")
    print("=" * 60)
    print("\a", end="", flush=True)

    try:
        page.wait_for_selector(
            BLOCKING_MODAL_SELECTOR,
            state="hidden",
            timeout=timeout,
        )

    except PlaywrightTimeoutError:
        raise RuntimeError(
            f"弹窗在 {wait_text} 内没有被处理掉，无法继续操作。"
        )

    jitter_wait(page, 500)

    print("弹窗已处理，继续执行。")
    print()


def safe_click(
    page: Page,
    locator: Locator,
    description: str,
    timeout: int = 8_000,
    retries: int = 3,
) -> None:
    """
    带清障重试的点击。

    这是本模块最重要的一个函数。经验教训：光在点击【之后】检查
    验证码是没用的——弹窗挡住的正是点击本身，点击会先抛异常，
    后面的检查根本执行不到。所以必须把清障绑定在动作上：

    - 点之前先清一次障；
    - 首次用较短的超时快速失败，而不是干等默认的 30 秒；
    - 一旦超时，说明多半是被什么东西挡住了，清障后重试。
    """

    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        clear_blockers(page)
        # 拟人化：每次点击前加随机小延迟，避免机械的毫秒级连续操作
        human_delay(80, 250)

        try:
            locator.click(timeout=timeout)
            return

        except PlaywrightTimeoutError as error:
            last_error = error

            print(
                f"点击【{description}】第 {attempt} 次未成功，"
                "检查是否有弹窗遮挡……"
            )

    dump_blocker(page, reason=f"点击 {description} 始终失败")

    raise RuntimeError(
        f"点击【{description}】失败：重试 {retries} 次后仍被遮挡或不可点击。"
    ) from last_error


# ============================================================
# 页面基础操作
# ============================================================

def open_chat_page(page: Page) -> None:
    """进入拼多多聊天记录查询页面。"""

    print("正在进入聊天记录页面……")

    try:
        page.goto(
            START_URL,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

    except PlaywrightTimeoutError:
        # 拼多多后台可能存在接口长时间加载的情况。
        # goto 超时不一定表示页面完全打不开。
        print("页面导航超过 60 秒，继续检查页面内容。")

    # domcontentloaded 返回时风控弹窗往往还没注入，稍等一下再看。
    # 这只是让提示来得更快，真正的兜底是各处的 safe_click。
    jitter_wait(page, 1_500)

    clear_blockers(page)


def get_order_search_radio(page: Page) -> Locator:
    """获取“按订单/违规会话编号查询”选项。"""

    return page.locator(
        'label[data-testid="beast-core-radio"]'
    ).filter(
        has_text="按订单/违规会话编号查询"
    )


def get_order_input(page: Page) -> Locator:
    """获取订单编号输入框。"""

    return page.get_by_placeholder(
        "请输入订单/违规会话编号"
    )


def get_query_button(page: Page) -> Locator:
    """获取查询按钮。"""

    return page.get_by_role(
        "button",
        name="查询",
        exact=True,
    )


def is_chat_page_ready(
    page: Page,
    timeout: int = 10_000,
) -> bool:
    """
    判断聊天记录查询页面是否已经加载完成。

    不使用订单输入框判断，因为输入框可能需要先选择查询方式。
    """

    radio = get_order_search_radio(page)

    try:
        radio.wait_for(
            state="visible",
            timeout=timeout,
        )
        return True

    except PlaywrightTimeoutError:
        return False


def find_mms_page(
    context: BrowserContext,
    default_page: Page,
) -> Page:
    """
    从所有标签页中找到拼多多商家后台页面。

    登录时可能跳转或打开新标签页，所以不能始终使用原来的 page。
    """

    available_pages = [
        page
        for page in context.pages
        if not page.is_closed()
    ]

    # 优先查找聊天记录页面
    for candidate in reversed(available_pages):
        if "/mms-chat/search" in candidate.url:
            return candidate

    # 其次查找拼多多商家后台页面
    for candidate in reversed(available_pages):
        if "mms.pinduoduo.com" in candidate.url:
            return candidate

    return default_page


# ============================================================
# 登录状态处理
# ============================================================

def wait_for_login(
    context: BrowserContext,
    page: Page,
) -> Page:
    """
    检测和处理登录状态。

    返回最终使用的 Page，因为登录过程中可能出现新标签页。
    """

    open_chat_page(page)

    if is_chat_page_ready(page):
        print("检测到已有登录状态。")
        print(f"当前页面：{page.url}")
        return page

    print()
    print("=" * 60)
    print("当前没有进入聊天记录查询页面。")
    print("请在打开的浏览器中完成拼多多商家后台登录。")
    print("登录成功后，不需要手动查找聊天记录页面。")
    print("=" * 60)

    input("登录完成后，回到控制台按回车继续：")

    # 登录可能发生在另一个标签页
    page = find_mms_page(
        context=context,
        default_page=page,
    )

    print(f"登录后的页面地址：{page.url}")

    # 登录完成后重新进入聊天记录页面
    open_chat_page(page)

    if not is_chat_page_ready(
        page,
        timeout=60_000,
    ):
        DEBUG_DIR.mkdir(
            parents=True,
            exist_ok=True,
        )

        screenshot_path = (
            DEBUG_DIR / "login_or_page_error.png"
        )

        try:
            page.screenshot(
                path=str(screenshot_path),
                full_page=True,
            )
        except Exception as screenshot_error:
            print(f"保存截图失败：{screenshot_error}")

        print()
        print("登录后仍然没有检测到聊天记录页面。")
        print(f"当前页面地址：{page.url}")
        print(f"错误截图位置：{screenshot_path}")

        raise RuntimeError(
            "登录后未成功进入聊天记录查询页面。"
        )

    print("登录成功，已进入聊天记录查询页面。")

    return page


# ============================================================
# 查询方式和订单输入
# ============================================================

def select_order_search_mode(page: Page) -> None:
    """选择“按订单/违规会话编号查询”方式。"""

    radio = get_order_search_radio(page)

    radio.wait_for(
        state="visible",
        timeout=30_000,
    )

    if radio.count() != 1:
        raise RuntimeError(
            "“按订单/违规会话编号查询”选项数量异常："
            f"{radio.count()}"
        )

    checked = radio.get_attribute("data-checked")

    if checked != "true":
        safe_click(
            page=page,
            locator=radio,
            description="按订单/违规会话编号查询",
        )

    try:
        page.wait_for_function(
            """
            () => {
                const labels = Array.from(
                    document.querySelectorAll(
                        'label[data-testid="beast-core-radio"]'
                    )
                );

                const target = labels.find((label) => {
                    return label.textContent
                        ?.includes("按订单/违规会话编号查询");
                });

                return (
                    target?.getAttribute("data-checked")
                    === "true"
                );
            }
            """,
            timeout=10_000,
        )

    except PlaywrightTimeoutError:
        # 某些页面状态更新较慢，但输入框可能已经显示。
        print("未检测到单选框状态变化，继续检查订单输入框。")

    order_input = get_order_input(page)

    order_input.wait_for(
        state="visible",
        timeout=20_000,
    )

    print("已选择：按订单/违规会话编号查询")


def fill_order_number(
    page: Page,
    order_no: str,
) -> None:
    """填写订单编号（使用拟人键盘输入，避免瞬时 .fill() 被风控检测）。"""

    order_no = order_no.strip()

    if not order_no:
        raise ValueError("订单编号不能为空。")

    order_input = get_order_input(page)

    order_input.wait_for(
        state="visible",
        timeout=20_000,
    )

    # 用模拟真人逐键输入替代 order_input.fill(order_no)
    # .fill() 是瞬间完成的，不产生 keydown/keyup 事件，极易被风控识别
    human_type(page, order_input, order_no)

    current_value = order_input.input_value()

    if current_value != order_no:
        raise RuntimeError(
            "订单编号填写失败："
            f"期望值={order_no}，实际值={current_value}"
        )

    print(f"已输入订单编号：{order_no}")


# ============================================================
# 查询结果等待
# ============================================================

def get_result_signature(page: Page) -> str:
    """
    获取当前查询结果区域的文本签名。

    用于判断点击查询后，结果是否发生变化。
    """

    result_panel = page.locator(
        ".search-order-sn-msg-table"
    )

    if result_panel.count() == 0:
        return ""

    try:
        return result_panel.inner_text(
            timeout=3_000,
        ).strip()

    except PlaywrightTimeoutError:
        return ""


def start_result_observer(page: Page) -> None:
    """监听查询结果区域的 DOM 变化。"""

    page.evaluate(
        """
        () => {
            if (window.__pddChatObserver) {
                window.__pddChatObserver.disconnect();
            }

            window.__pddChatMutationCount = 0;

            const resultPanel =
                document.querySelector(
                    ".search-order-sn-msg-table"
                );

            const target =
                resultPanel
                || document.querySelector(".search-content")
                || document.body;

            window.__pddChatObserver =
                new MutationObserver(() => {
                    window.__pddChatMutationCount += 1;
                });

            window.__pddChatObserver.observe(
                target,
                {
                    childList: true,
                    subtree: true,
                    characterData: true,
                    attributes: true,
                }
            );
        }
        """
    )


def wait_for_query_result(
    page: Page,
    old_signature: str,
) -> None:
    """等待查询完成并等待聊天记录渲染。"""

    try:
        page.wait_for_function(
            """
            (oldText) => {
                const mutationCount =
                    window.__pddChatMutationCount || 0;

                const panel =
                    document.querySelector(
                        ".search-order-sn-msg-table"
                    );

                const currentText =
                    panel?.innerText?.trim() || "";

                const hasMessages =
                    document.querySelectorAll(
                        ".new-message-list .message-item"
                    ).length > 0;

                const hasResultTitle =
                    Array.from(
                        document.querySelectorAll("*")
                    ).some((element) => {
                        return (
                            element.children.length === 0
                            && element.textContent?.trim()
                            === "查询结果"
                        );
                    });

                return (
                    mutationCount > 0
                    || currentText !== oldText
                    || hasMessages
                    || hasResultTitle
                );
            }
            """,
            arg=old_signature,
            timeout=30_000,
        )

    except PlaywrightTimeoutError:
        # 超时很可能是验证码/弹窗把请求拦住了。这里如果闷头继续，
        # 就会拿着一张陈旧的页面去采集，产出错的数据——比直接报错更糟。
        # 所以先清障，清掉了就再等一轮真正的结果。
        print("未检测到结果变化，检查是否有弹窗遮挡……")

        clear_blockers(page)

        try:
            page.wait_for_function(
                """
                () => {
                    const mutationCount =
                        window.__pddChatMutationCount || 0;

                    const hasMessages =
                        document.querySelectorAll(
                            ".new-message-list .message-item"
                        ).length > 0;

                    const hasBlankText =
                        document.querySelector(
                            ".search-order-sn-msg-table .table-blank-text"
                        ) !== null;

                    return (
                        mutationCount > 0
                        || hasMessages
                        || hasBlankText
                    );
                }
                """,
                timeout=30_000,
            )

        except PlaywrightTimeoutError:
            print(
                "清障后仍未检测到结果变化，"
                "继续读取当前页面结果。"
            )

    # 等待前端最后一批 DOM 渲染
    jitter_wait(page, 1_000)


# ============================================================
# 聊天记录提取
# ============================================================

def is_chat_records_empty(page: Page) -> bool:
    """
    判断查询结果是否为"暂无聊天记录"（订单确实没有聊天记录）。

    拼多多页面在这种情况下，聊天列表容器（.new-message-list）
    仍然存在于 DOM 中，但父级 .result-col-body 会被设置成
    display:none，导致该容器永远不会变成"可见"状态；如果照常
    等待它 visible 会白等 20 秒后抛出超时异常。真正代表"没有
    聊天记录"的标志，是与结果表格同级的 .table-blank-text 提示。

    调用前应确保 wait_for_query_result() 已经执行完毕，
    页面已经渲染稳定。
    """

    blank_text = page.locator(
        ".search-order-sn-msg-table .table-blank-text"
    ).filter(has_text="暂无聊天记录")

    if blank_text.count() == 0:
        return False

    try:
        return blank_text.first.is_visible()
    except Exception:
        return False


def to_relative_evidence_path(path: Path) -> str:
    """把凭证截图的绝对路径转换成相对项目根目录的字符串，方便存入 JSON。"""

    try:
        return path.relative_to(BASE_DIR).as_posix()
    except ValueError:
        return str(path)


def save_no_chat_record_screenshot(
    page: Page,
    order_no: str,
    run_id: str,
) -> Path:
    """
    订单查询结果为"暂无聊天记录"时，保存整页截图作为凭证。

    即使没有聊天内容，也需要留存"确实查询过、确实没有记录"
    的证据，因此这里截取整个页面（包含订单号、查询结果标题、
    "暂无聊天记录"提示），而不是跳过截图。
    """

    screenshot_dir = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / run_id
    )

    screenshot_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        screenshot_dir / "no_chat_record.png"
    )

    result_table = page.locator(
        ".search-order-sn-msg-table"
    )

    try:
        result_table.wait_for(
            state="visible",
            timeout=10_000,
        )
    except PlaywrightTimeoutError:
        print("查询结果区域 10 秒内未显示，仍尝试截图。")

    page.screenshot(
        path=str(screenshot_path),
        full_page=True,
        animations="disabled",
        scale="css",
    )

    print(f'"暂无聊天记录"凭证截图：{screenshot_path}')

    return screenshot_path


def get_current_page_signature(page: Page) -> str:
    """
    获取当前聊天记录页的特征。

    用于判断翻页后聊天内容是否发生变化，
    避免重复采集同一页或陷入死循环。
    """

    message_items = page.locator(
        ".new-message-list .message-item"
    )

    return message_items.evaluate_all(
        """
        (items) => {
            const records = items.map((item) => {
                const sender =
                    item.querySelector(".message-name")
                    ?.textContent?.trim() || "";

                const time =
                    item.querySelector(".message-time")
                    ?.textContent?.trim() || "";

                const content =
                    item.querySelector(".message-body")
                    ?.innerText?.trim() || "";

                const images = Array.from(
                    item.querySelectorAll(".message-body img")
                ).map((image) => {
                    return image.getAttribute("src") || "";
                });

                return {
                    sender,
                    time,
                    content,
                    images,
                };
            });

            return JSON.stringify(records);
        }
        """
    )


def extract_current_page_records(
    page: Page,
    page_number: int,
) -> list[dict]:
    """提取当前这一页的聊天记录。"""

    message_items = page.locator(
        ".new-message-list .message-item"
    )

    if message_items.count() == 0:
        return []

    records = message_items.evaluate_all(
        """
        (items) => {
            const cleanText = (value) => {
                return (value || "")
                    .replace(/\\u00a0/g, " ")
                    .trim();
            };

            return items.map((item, index) => {
                const senderElement =
                    item.querySelector(".message-name");

                const timeElement =
                    item.querySelector(".message-time");

                const bodyElement =
                    item.querySelector(".message-body");

                const avatarElement =
                    item.querySelector(".message-avatar");

                const textElements = Array.from(
                    item.querySelectorAll(".message-text")
                );

                let content = textElements
                    .map((element) => {
                        return cleanText(
                            element.innerText
                            || element.textContent
                        );
                    })
                    .filter(Boolean)
                    .join("\\n");

                if (!content && bodyElement) {
                    content = cleanText(
                        bodyElement.innerText
                        || bodyElement.textContent
                    );
                }

                const imageUrls = Array.from(
                    item.querySelectorAll(
                        ".message-body img"
                    )
                )
                    .map((image) => {
                        return image.getAttribute("src") || "";
                    })
                    .filter(Boolean);

                return {
                    page_index: index + 1,

                    sender: cleanText(
                        senderElement?.innerText
                        || senderElement?.textContent
                    ),

                    time: cleanText(
                        timeElement?.innerText
                        || timeElement?.textContent
                    ),

                    content,

                    is_system_message:
                        item.classList.contains(
                            "system-message"
                        ),

                    avatar_url:
                        avatarElement?.getAttribute("src")
                        || "",

                    image_urls: imageUrls,
                };
            });
        }
        """
    )

    for record in records:
        record["page_number"] = page_number

    return records


def get_expected_message_total(page: Page) -> int | None:
    """
    从“共有 123 条”中读取理论消息总数。

    读取不到时返回 None。
    """

    pagination = page.locator(
        '[data-testid="beast-core-pagination"]'
    )

    if pagination.count() == 0:
        return None

    try:
        pagination_text = pagination.inner_text(
            timeout=3_000,
        )
    except PlaywrightTimeoutError:
        return None

    matched = re.search(
        r"共有\s*(\d+)\s*条",
        pagination_text,
    )

    if not matched:
        return None

    return int(matched.group(1))


def click_next_page(page: Page) -> dict:
    """
    点击分页组件中的下一页。

    因为目前没有多页情况下的完整分页 HTML，
    这里同时支持：
    - aria-label 包含下一页或 next
    - title 包含下一页
    - data-testid 包含 next
    - class 中包含 next、right、arrow-right
    - 分页组件最后一个可操作项
    """

    result = page.evaluate(
        """
        () => {
            const root = document.querySelector(
                '[data-testid="beast-core-pagination"]'
            );

            if (!root) {
                return {
                    clicked: false,
                    reason: "没有找到分页组件"
                };
            }

            const rootStyle = window.getComputedStyle(root);

            if (
                rootStyle.display === "none"
                || rootStyle.visibility === "hidden"
            ) {
                return {
                    clicked: false,
                    reason: "分页组件未显示，当前可能只有一页"
                };
            }

            const normalize = (value) => {
                return String(value || "")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase();
            };

            const getDescription = (element) => {
                return normalize([
                    element.textContent,
                    element.getAttribute("aria-label"),
                    element.getAttribute("title"),
                    element.getAttribute("data-testid"),
                    element.getAttribute("class"),
                    element.querySelector("[data-testid]")
                        ?.getAttribute("data-testid"),
                    element.querySelector("[class]")
                        ?.getAttribute("class"),
                ].join(" "));
            };

            const isDisabled = (element) => {
                if (!element) {
                    return true;
                }

                const description = getDescription(element);

                return (
                    element.disabled === true
                    || element.getAttribute("disabled") !== null
                    || element.getAttribute("aria-disabled") === "true"
                    || element.getAttribute("data-disabled") === "true"
                    || /disabled|disable/.test(description)
                );
            };

            const elements = Array.from(
                root.querySelectorAll(
                    "button, a, li"
                )
            );

            const scored = elements
                .map((element) => {
                    const description =
                        getDescription(element);

                    let score = 0;

                    if (description.includes("下一页")) {
                        score += 100;
                    }

                    if (
                        /(^|\\s|_|-)next($|\\s|_|-)/.test(
                            description
                        )
                    ) {
                        score += 80;
                    }

                    if (
                        /arrow.?right|chevron.?right|page.?right/.test(
                            description
                        )
                    ) {
                        score += 60;
                    }

                    if (
                        /(^|\\s|_|-)right($|\\s|_|-)/.test(
                            description
                        )
                    ) {
                        score += 30;
                    }

                    if (
                        description.includes("上一页")
                        || /prev|previous|arrow.?left/.test(
                            description
                        )
                    ) {
                        score -= 200;
                    }

                    if (
                        description.includes("共有")
                        || description.includes("total")
                    ) {
                        score -= 200;
                    }

                    return {
                        element,
                        score,
                        description,
                    };
                })
                .filter((item) => item.score > 0)
                .sort((a, b) => b.score - a.score);

            let target = scored[0]?.element || null;

            /*
             * 如果无法根据属性识别下一页，
             * 尝试取分页组件的最后一个直接子元素。
             */
            if (!target) {
                const directChildren = Array.from(
                    root.children
                ).filter((element) => {
                    return element.matches(
                        "li, button, a"
                    );
                });

                if (directChildren.length >= 3) {
                    target = directChildren[
                        directChildren.length - 1
                    ];
                }
            }

            if (!target) {
                return {
                    clicked: false,
                    reason: "没有识别到下一页按钮"
                };
            }

            const clickable = target.matches(
                "button, a"
            )
                ? target
                : (
                    target.querySelector("button, a")
                    || target
                );

            if (
                isDisabled(target)
                || isDisabled(clickable)
            ) {
                return {
                    clicked: false,
                    reason: "已经到最后一页"
                };
            }

            clickable.scrollIntoView({
                block: "center",
                inline: "center",
            });

            clickable.click();

            return {
                clicked: true,
                reason: "已点击下一页",
                description:
                    getDescription(clickable),
            };
        }
        """
    )

    return result


def wait_for_next_page_loaded(
    page: Page,
    previous_signature: str,
) -> bool:
    """等待点击下一页后聊天记录发生变化。"""

    try:
        page.wait_for_function(
            """
            (previousSignature) => {
                const items = Array.from(
                    document.querySelectorAll(
                        ".new-message-list .message-item"
                    )
                );

                if (items.length === 0) {
                    return false;
                }

                const records = items.map((item) => {
                    const sender =
                        item.querySelector(".message-name")
                        ?.textContent?.trim() || "";

                    const time =
                        item.querySelector(".message-time")
                        ?.textContent?.trim() || "";

                    const content =
                        item.querySelector(".message-body")
                        ?.innerText?.trim() || "";

                    const images = Array.from(
                        item.querySelectorAll(
                            ".message-body img"
                        )
                    ).map((image) => {
                        return image.getAttribute("src") || "";
                    });

                    return {
                        sender,
                        time,
                        content,
                        images,
                    };
                });

                const currentSignature =
                    JSON.stringify(records);

                return (
                    currentSignature
                    !== previousSignature
                );
            }
            """,
            arg=previous_signature,
            timeout=20_000,
        )

        # 等待当前页剩余内容渲染完成
        jitter_wait(page, 800)

        return True

    except PlaywrightTimeoutError:
        return False

def expand_chat_scroll_area(page: Page) -> dict:
    """
    找到聊天记录内部的滚动容器，并临时展开全部内容。

    所有被修改的行内样式都会保存在浏览器中，
    截图完成后由 restore_chat_scroll_area() 恢复。
    """

    return page.evaluate(
        """
        () => {
            const messageList = document.querySelector(
                ".new-message-list"
            );

            if (!messageList) {
                throw new Error(
                    "没有找到 .new-message-list"
                );
            }

            const isScrollable = (element) => {
                const style = window.getComputedStyle(element);

                const overflowY = style.overflowY;

                return (
                    ["auto", "scroll", "overlay"]
                        .includes(overflowY)
                    && element.scrollHeight
                        > element.clientHeight + 2
                );
            };

            /*
             * 从聊天列表开始向上查找真正拥有滚动条的父元素。
             */
            let scrollContainer = messageList;

            while (
                scrollContainer
                && scrollContainer !== document.body
            ) {
                if (isScrollable(scrollContainer)) {
                    break;
                }

                scrollContainer =
                    scrollContainer.parentElement;
            }

            /*
             * 没识别到明显滚动容器时，
             * 仍然从聊天列表自身开始展开。
             */
            if (
                !scrollContainer
                || scrollContainer === document.body
            ) {
                scrollContainer = messageList;
            }

            const beforeInfo = {
                tag: scrollContainer.tagName,
                className:
                    scrollContainer.className || "",
                clientHeight:
                    scrollContainer.clientHeight,
                scrollHeight:
                    scrollContainer.scrollHeight,
                scrollTop:
                    scrollContainer.scrollTop,
            };

            /*
             * 防止上次异常退出后残留备份。
             */
            if (window.__pddEvidenceStyleBackups) {
                window.__pddEvidenceStyleBackups
                    .forEach((backup) => {
                        if (backup.originalStyle === null) {
                            backup.element.removeAttribute(
                                "style"
                            );
                        } else {
                            backup.element.setAttribute(
                                "style",
                                backup.originalStyle
                            );
                        }
                    });
            }

            const backups = [];

            const rememberElement = (element) => {
                if (
                    backups.some(
                        (item) => item.element === element
                    )
                ) {
                    return;
                }

                backups.push({
                    element,
                    originalStyle:
                        element.getAttribute("style"),
                });
            };

            /*
             * 从滚动容器一直修改到聊天页面主体。
             * 这样可以避免父元素的 height、max-height、
             * overflow 再次裁剪聊天记录。
             */
            const stopElement =
                messageList.closest(
                    "#pdd-app-skeleton-main-content"
                )
                || messageList.closest(".search-content")
                || document.body;

            let current = scrollContainer;

            while (current) {
                rememberElement(current);

                current.style.setProperty(
                    "height",
                    "auto",
                    "important"
                );

                current.style.setProperty(
                    "max-height",
                    "none",
                    "important"
                );

                current.style.setProperty(
                    "overflow",
                    "visible",
                    "important"
                );

                current.style.setProperty(
                    "overflow-y",
                    "visible",
                    "important"
                );

                current.style.setProperty(
                    "contain",
                    "none",
                    "important"
                );

                if (
                    current === stopElement
                    || current === document.body
                ) {
                    break;
                }

                current = current.parentElement;
            }

            /*
             * 聊天列表自己也可能带 max-height。
             */
            rememberElement(messageList);

            messageList.style.setProperty(
                "height",
                "auto",
                "important"
            );

            messageList.style.setProperty(
                "max-height",
                "none",
                "important"
            );

            messageList.style.setProperty(
                "overflow",
                "visible",
                "important"
            );

            /*
             * 从第一条聊天记录开始截图。
             */
            scrollContainer.scrollTop = 0;

            window.__pddEvidenceStyleBackups = backups;
            window.__pddEvidenceScrollContainer =
                scrollContainer;

            const afterInfo = {
                clientHeight:
                    scrollContainer.clientHeight,
                scrollHeight:
                    scrollContainer.scrollHeight,
            };

            return {
                found: true,
                before: beforeInfo,
                after: afterInfo,
                modifiedElementCount: backups.length,
            };
        }
        """
    )


def restore_chat_scroll_area(page: Page) -> None:
    """恢复截图前临时修改的网页样式。"""

    page.evaluate(
        """
        () => {
            const backups =
                window.__pddEvidenceStyleBackups || [];

            /*
             * 从外向内倒序恢复，避免恢复过程中布局互相影响。
             */
            [...backups]
                .reverse()
                .forEach((backup) => {
                    if (!backup.element) {
                        return;
                    }

                    if (backup.originalStyle === null) {
                        backup.element.removeAttribute(
                            "style"
                        );
                    } else {
                        backup.element.setAttribute(
                            "style",
                            backup.originalStyle
                        );
                    }
                });

            delete window.__pddEvidenceStyleBackups;
            delete window.__pddEvidenceScrollContainer;
        }
        """
    )


def wait_for_chat_images(page: Page) -> None:
    """
    等待当前页聊天记录中的图片加载完成。

    避免截图中出现空白图片或图片加载到一半。
    """

    try:
        page.wait_for_function(
            """
            () => {
                const images = Array.from(
                    document.querySelectorAll(
                        ".new-message-list img"
                    )
                );

                return images.every((image) => {
                    return (
                        image.complete
                        && image.naturalWidth > 0
                    );
                });
            }
            """,
            timeout=10_000,
        )

    except PlaywrightTimeoutError:
        print(
            "部分聊天图片在 10 秒内未加载完成，"
            "继续保存截图。"
        )


def save_page_evidence_screenshot(
    page: Page,
    order_no: str,
    page_number: int,
    run_id: str,
) -> Path:
    """
    保存当前分页的完整聊天记录截图。

    截图前会临时展开聊天列表内部的滚动区域，
    因此能够截取滚动条下面的全部聊天记录。
    """

    screenshot_dir = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / run_id
    )

    screenshot_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    screenshot_path = (
        screenshot_dir
        / f"page_{page_number:03d}.png"
    )

    message_list = page.locator(
        ".new-message-list"
    )

    message_list.wait_for(
        state="visible",
        timeout=20_000,
    )

    wait_for_chat_images(page)

    try:
        expand_info = expand_chat_scroll_area(page)

        before = expand_info.get("before", {})
        after = expand_info.get("after", {})

        print(
            "聊天滚动区域："
            f'可见高度 {before.get("clientHeight", 0)}，'
            f'内容总高度 {before.get("scrollHeight", 0)}'
        )

        print(
            "展开后的区域："
            f'可见高度 {after.get("clientHeight", 0)}，'
            f'内容总高度 {after.get("scrollHeight", 0)}'
        )

        # 等待网页重新排版稳定
        jitter_wait(page, 800)

        page.screenshot(
            path=str(screenshot_path),
            full_page=True,
            animations="disabled",
            scale="css",
        )

        print(
            f"第 {page_number} 页完整凭证截图："
            f"{screenshot_path}"
        )

        return screenshot_path

    finally:
        # 即使截图失败也要恢复网页，否则会影响翻页
        try:
            restore_chat_scroll_area(page)
            jitter_wait(page, 300)

        except Exception as restore_error:
            print(
                "恢复聊天区域样式失败："
                f"{restore_error}"
            )

def collect_all_chat_records(
    page: Page,
    order_no: str,
    max_pages: int = 200,
) -> tuple[list[dict], Path | None]:
    """
    采集所有分页中的聊天记录，并为每一页保存凭证截图。

    max_pages 是安全限制，防止网页异常时无限翻页。

    返回 (聊天记录列表, "暂无聊天记录"凭证截图路径)。
    订单确实有聊天记录时，第二个值为 None。
    """

    # 本次运行的唯一编号，避免多次采集时截图相互覆盖
    run_id = datetime.now().strftime(
        "%Y%m%d_%H%M%S"
    )

    if is_chat_records_empty(page):
        print(
            f"订单 {order_no} 查询结果为"
            '"暂无聊天记录"，本订单没有聊天记录。'
        )

        no_chat_screenshot = save_no_chat_record_screenshot(
            page=page,
            order_no=order_no,
            run_id=run_id,
        )

        return [], no_chat_screenshot

    all_records: list[dict] = []
    visited_signatures: set[str] = set()

    expected_total = get_expected_message_total(
        page
    )

    if expected_total is not None:
        print(
            f"分页显示共有 {expected_total} 条聊天记录。"
        )

    current_page = 1

    while current_page <= max_pages:
        message_list = page.locator(
            ".new-message-list"
        )

        message_list.wait_for(
            state="visible",
            timeout=20_000,
        )

        # 确保当前页的消息已经渲染
        page.locator(
            ".new-message-list .message-item"
        ).first.wait_for(
            state="visible",
            timeout=20_000,
        )

        current_signature = (
            get_current_page_signature(page)
        )

        if not current_signature:
            print(
                f"第 {current_page} 页没有获取到有效内容，"
                "停止采集。"
            )
            break

        # 防止重复采集同一页
        if current_signature in visited_signatures:
            print(
                "检测到重复页面，停止翻页，"
                "避免重复采集或死循环。"
            )
            break

        visited_signatures.add(
            current_signature
        )

        # 先保存当前页截图
        screenshot_path = (
            save_page_evidence_screenshot(
                page=page,
                order_no=order_no,
                page_number=current_page,
                run_id=run_id,
            )
        )

        # 再提取当前页数据
        page_records = (
            extract_current_page_records(
                page=page,
                page_number=current_page,
            )
        )

        # JSON 中保存截图相对路径
        relative_screenshot_path = (
            to_relative_evidence_path(screenshot_path)
        )

        for record in page_records:
            record["evidence_screenshot"] = (
                relative_screenshot_path
            )

        print(
            f"第 {current_page} 页采集完成，"
            f"本页 {len(page_records)} 条。"
        )

        all_records.extend(page_records)

        # 如果已经达到页面显示的总数量，可以提前结束
        if (
            expected_total is not None
            and len(all_records) >= expected_total
        ):
            print(
                "已达到分页显示的聊天记录总数，"
                "停止继续翻页。"
            )
            break

        # 翻页的点击发生在 JS 内部，会绕过遮挡直接派发事件、不抛错，
        # 但后续的数据刷新仍可能被风控拦住，所以要先清障再翻页。
        clear_blockers(page)

        next_result = click_next_page(page)

        # JS 内部直接调 el.click()，没有触发 mouse 事件；
        # 翻页后加一段随机延迟，模拟用户翻页→等加载→继续阅读的自然节奏
        human_delay(600, 1500)

        if not next_result.get("clicked"):
            print(
                "停止翻页："
                f'{next_result.get("reason", "未知原因")}'
            )
            break

        changed = wait_for_next_page_loaded(
            page=page,
            previous_signature=current_signature,
        )

        if not changed:
            print(
                "点击下一页后聊天内容没有发生变化，"
                "停止翻页。"
            )
            break

        current_page += 1

    else:
        print(
            f"已经达到最大翻页限制 "
            f"{max_pages} 页，停止继续采集。"
        )

    # 生成所有消息的全局编号
    for global_index, record in enumerate(
        all_records,
        start=1,
    ):
        record["index"] = global_index

    print()
    print(
        f"所有分页采集结束，共采集 "
        f"{len(all_records)} 条聊天记录。"
    )

    evidence_folder = (
        EVIDENCE_DIR
        / safe_filename(order_no)
        / run_id
    )

    print(
        f"凭证截图保存目录："
        f"{evidence_folder}"
    )

    if (
        expected_total is not None
        and len(all_records) != expected_total
    ):
        print(
            "警告：实际采集数量与分页显示数量不一致。"
        )
        print(
            f"页面显示：{expected_total} 条，"
            f"实际采集：{len(all_records)} 条。"
        )

    return all_records, None


# ============================================================
# 完整查询流程
# ============================================================

def query_chat_records(
    page: Page,
    order_no: str,
) -> tuple[list[dict], Path | None]:
    """
    根据订单编号查询聊天记录。

    返回 (聊天记录列表, "暂无聊天记录"凭证截图路径)。
    订单确实有聊天记录时，第二个值为 None。
    """

    order_no = order_no.strip()

    if not order_no:
        raise ValueError("订单编号不能为空。")

    select_order_search_mode(page)

    fill_order_number(
        page=page,
        order_no=order_no,
    )

    query_button = get_query_button(page)

    query_button.wait_for(
        state="visible",
        timeout=20_000,
    )

    if not query_button.is_enabled():
        raise RuntimeError("查询按钮当前不可用。")

    old_signature = get_result_signature(page)

    start_result_observer(page)

    print(f"正在查询订单：{order_no}")

    safe_click(
        page=page,
        locator=query_button,
        description="查询按钮",
    )

    # 点击查询会触发后端请求，是验证码高发时机
    clear_blockers(page)

    wait_for_query_result(
        page=page,
        old_signature=old_signature,
    )

    records, no_chat_screenshot = collect_all_chat_records(
        page=page,
        order_no=order_no,
        max_pages=200,
    )

    return records, no_chat_screenshot


# ============================================================
# 数据输出
# ============================================================

def print_chat_records(
    records: list[dict],
) -> None:
    """在控制台打印聊天记录。"""

    print()
    print("=" * 70)
    print("聊天记录")
    print("=" * 70)

    if not records:
        print("当前订单没有获取到聊天记录。")
        print("=" * 70)
        return

    for record in records:
        message_type = (
            "系统消息"
            if record["is_system_message"]
            else "普通消息"
        )

        print(
            f'[{record["index"]}] '
            f'{record["time"]} '
            f'{record["sender"]} '
            f'({message_type})'
        )

        if record["content"]:
            print(record["content"])
        elif record["image_urls"]:
            print("[图片消息]")
        else:
            print("[未识别的非文本消息]")

        if record["image_urls"]:
            for image_url in record["image_urls"]:
                print(f"图片：{image_url}")

        print("-" * 70)


def safe_filename(value: str) -> str:
    """将订单编号转换成安全的文件名。"""

    cleaned = re.sub(
        r'[<>:"/\\|?*]',
        "_",
        value,
    )

    return cleaned.strip() or "unknown_order"


def save_chat_records(
    order_no: str,
    records: list[dict],
    no_chat_record_screenshot: Path | None = None,
    issue_type: str = "",
) -> Path:
    """
    将聊天记录保存成 JSON 文件。

    no_chat_record_screenshot：订单查询结果为"暂无聊天记录"时的
    凭证截图路径；订单确实有聊天记录时传 None。
    issue_type：订单问题类型（"商品问题"/"服务问题"），从申诉列表表格采集。
    """

    OUTPUT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    filename = (
        f"chat_{safe_filename(order_no)}.json"
    )

    output_file = OUTPUT_DIR / filename

    evidence_screenshots = list(
        dict.fromkeys(
            record.get("evidence_screenshot", "")
            for record in records
            if record.get("evidence_screenshot")
        )
    )

    if no_chat_record_screenshot is not None:
        evidence_screenshots.append(
            to_relative_evidence_path(no_chat_record_screenshot)
        )

    result = {
        "order_no": order_no,
        "issue_type": issue_type,
        "message_count": len(records),
        "evidence_screenshots": evidence_screenshots,
        "messages": records,
    }

    output_file.write_text(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return output_file


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
        print(f"保存错误截图失败：{error}")
        return None


# ============================================================
# 程序入口
# ============================================================

def main() -> None:
    PROFILE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    order_no = input(
        "请输入订单编号，直接回车使用默认值 "
        f"{DEFAULT_ORDER_NO}："
    ).strip()

    if not order_no:
        order_no = DEFAULT_ORDER_NO

    with sync_playwright() as playwright:
        context = create_stealth_context(
            playwright,
            user_data_dir=str(PROFILE_DIR),
        )

        page: Page | None = None

        try:
            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            page = wait_for_login(
                context=context,
                page=page,
            )

            records, no_chat_screenshot = query_chat_records(
                page=page,
                order_no=order_no,
            )

            print_chat_records(records)

            output_file = save_chat_records(
                order_no=order_no,
                records=records,
                no_chat_record_screenshot=no_chat_screenshot,
            )

            print()
            print(f"聊天记录已保存到：{output_file}")

            input("操作完成，按回车关闭浏览器：")

        except Exception as error:
            print()
            print("=" * 60)
            print("程序执行失败")
            print(f"错误类型：{type(error).__name__}")
            print(f"错误内容：{error}")
            print("=" * 60)

            if page is not None and not page.is_closed():
                screenshot_path = save_error_screenshot(
                    page=page,
                    order_no=order_no,
                )

                if screenshot_path:
                    print(
                        "错误页面截图已保存到："
                        f"{screenshot_path}"
                    )

            input("按回车关闭浏览器：")

        finally:
            context.close()


if __name__ == "__main__":
    main()