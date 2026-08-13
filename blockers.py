"""弹窗清障 + 安全点击。

从 pdd_chat.py 抽出弹窗识别/关闭/清障流程，并从 stealth.py 合入
stealth_safe_click——两者都是"点击 + 清障"的横切工具，理应共处一室。

两个 click helper 的差异（**不能互换**）：
  - safe_click:           在 modal *打开之前*用。点前 clear_blockers，
                          点击遇挡就重试。适合列表页、按钮、链接。
  - stealth_safe_click:   在 modal *已经打开* 时用（表单/申诉框内部按钮）。
                          绝不调用 clear_blockers——那会把当前 modal
                          本身当通知型弹窗关掉。适合模态框内单选/上传等
                          幂等操作。
"""

from __future__ import annotations

from datetime import datetime

from playwright.sync_api import (
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from captcha import (
    CAPTCHA_WAIT_TIMEOUT,
    describe_timeout,
    ensure_no_captcha,
    is_captcha_visible,
)
from paths import DEBUG_DIR
from stealth import human_delay, jitter_wait


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
    """
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    try:
        modal = find_blocking_modal(page)
        if modal is not None:
            html = modal.evaluate("element => element.outerHTML")
            html_path = DEBUG_DIR / f"blocker_{timestamp}.html"
            html_path.write_text(html, encoding="utf-8")
            print(f"遮挡物 HTML 已存档：{html_path}")
    except Exception as error:
        # P1 顺路：保留 traceback 便于定位
        import traceback

        print(f"保存遮挡物 HTML 失败：{error}")
        traceback.print_exc()

    try:
        screenshot_path = DEBUG_DIR / f"blocker_{timestamp}.png"
        page.screenshot(path=str(screenshot_path), full_page=True)
        print(f"遮挡物截图已存档：{screenshot_path}（原因：{reason}）")
    except Exception as error:
        import traceback

        print(f"保存遮挡物截图失败：{error}")
        traceback.print_exc()


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
    except PlaywrightTimeoutError as err:
        # P1 顺路：保留原始 timeout 上下文
        raise RuntimeError(
            f"弹窗在 {wait_text} 内没有被处理掉，无法继续操作。"
        ) from err

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
    带清障重试的点击。适用于 modal *打开之前* 的场景。

    经验教训：光在点击【之后】检查验证码是没用的——弹窗挡住的正是点击本身，
    点击会先抛异常，后面的检查根本执行不到。所以必须把清障绑定在动作上：

    - 点之前先清一次障；
    - 首次用较短的超时快速失败，而不是干等默认的 30 秒；
    - 一旦超时，说明多半是被什么东西挡住了，清障后重试。

    **不要在 modal 已经打开的情况下用它**——clear_blockers 会把当前 modal
    误判为通知型弹窗关掉。那种场景用 stealth_safe_click。
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


def stealth_safe_click(
    page: Page,
    locator: Locator,
    description: str,
    timeout: int = 8_000,
    retries: int = 3,
) -> None:
    """
    带拟人延迟的点击。适用于 modal *已经打开* 的场景。

    与 safe_click 的关键差异：**不调用 clear_blockers** —— 那会把当前
    模态框本身当通知型弹窗关掉，把用户正在填的表单一起弄没。适合申诉
    模态框内部的单选、上传、提交等按钮点击。
    """
    last_error: Exception | None = None

    for attempt in range(1, retries + 1):
        # 模拟看到元素后的反应时间
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

    raise RuntimeError(
        f"点击【{description}】失败：重试 {retries} 次后仍被遮挡或不可点击。"
    ) from last_error
