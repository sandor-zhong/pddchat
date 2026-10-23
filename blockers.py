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
import json
import os

from playwright.sync_api import (
    Error as PlaywrightError,
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
from stealth import human_click, human_delay, jitter_wait

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
            human_click(page, closer.first)
            modal.wait_for(state="hidden", timeout=5000)
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


def save_click_diagnostics(
    page: Page, locator: Locator, description: str, attempt: int, error: Exception,
) -> str | None:
    """每次失败立即留存现场；采集异常不覆盖原始点击错误，也不操作页面。"""
    data = {
        "description": description, "attempt": attempt,
        "error_type": type(error).__name__, "original_error": str(error),
        "observed_at": datetime.now().isoformat(timespec="milliseconds"),
        "observation_note": "状态采样于点击失败后；命中测试为元素中心，未必是实际点击点。",
        "capture_errors": {},
    }
    try:
        folder = DEBUG_DIR / "click_diagnostics"
        folder.mkdir(parents=True, exist_ok=True)
        stem = folder / f"click_{datetime.now():%Y%m%d_%H%M%S_%f}_{os.getpid()}_{attempt}"
    except Exception as exc:
        print(f"[点击诊断] 无法创建诊断目录：{exc}")
        return None

    def capture(name, action):
        try:
            data[name] = action()
        except Exception as exc:
            data["capture_errors"][name] = str(exc)

    capture("url", lambda: page.url)
    capture("locator", lambda: str(locator))
    capture("targets", lambda: locator.evaluate_all("""elements => ({
        count: elements.length,
        samples: elements.slice(0, 5).map(el => {
            const rect = el.getBoundingClientRect(), style = getComputedStyle(el);
            const x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
            const hit = el.ownerDocument.elementFromPoint(x, y);
            const summary = node => node ? {
                tag: node.tagName, id: node.id, class: node.className,
                html: node.outerHTML.slice(0, 8192)
            } : null;
            return {
                target: summary(el), connected: el.isConnected,
                rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                has_client_rects: Boolean(el.getClientRects().length),
                display: style.display, visibility: style.visibility,
                opacity: style.opacity, pointer_events: style.pointerEvents,
                disabled: el.matches(':disabled') || el.getAttribute('aria-disabled') === 'true',
                data_disabled: el.getAttribute('data-disabled'),
                viewport: {width: window.innerWidth, height: window.innerHeight},
                center_hit: summary(hit), center_hits_target: Boolean(hit && (hit === el || el.contains(hit))),
                animations: el.getAnimations().map(a => ({play_state: a.playState}))
            };
        })
    })"""))
    capture("dropdowns", lambda: page.locator('[data-testid="beast-core-portal"]').evaluate_all("""nodes =>
        nodes.slice(0, 10).map(el => ({
            visible: Boolean(el.getClientRects().length) && getComputedStyle(el).visibility !== 'hidden',
            html: el.outerHTML.slice(0, 65536)
        }))"""))
    screenshot = stem.with_suffix(".png")

    def capture_screenshot():
        page.screenshot(path=str(screenshot), full_page=False, timeout=3000)
        return str(screenshot)

    capture("screenshot", capture_screenshot)
    html_path = stem.with_suffix(".html")
    try:
        sections = [item["html"] for item in data.get("dropdowns", [])]
        sections += [item["target"]["html"] for item in data.get("targets", {}).get("samples", [])]
        html_path.write_text("\n<hr>\n".join(sections), encoding="utf-8")
        data["html_path"] = str(html_path)
    except Exception as exc:
        data["capture_errors"]["html"] = str(exc)
    report = stem.with_suffix(".json")
    try:
        report.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        print(f"[点击诊断] 无法保存诊断报告：{exc}")
        return None
    print(f"[点击诊断] 第 {attempt} 次失败现场：{report}")
    return str(report)


def _click_failure(description: str, attempts: int, error: Exception, report: str | None) -> RuntimeError:
    return RuntimeError(
        f"点击【{description}】未完成：尝试 {attempts} 次后失败。"
        f"原始错误：{type(error).__name__}: {error}"
        + (f"；点击诊断：{report}" if report else "")
    )


def dismiss_new_message_popup(page: Page) -> bool:
    """关闭右侧新消息浮层，只点击其标题栏的关闭图标。"""
    # 消息浮层可能在全屏模态框下方，不能先点被遮挡的关闭图标。
    if find_blocking_modal(page) is not None:
        return False
    popup = page.locator('#umd-msgbox-handler:visible')
    if not popup.count():
        return False
    closer = popup.locator(
        '[class*="ImportantList_msgbox-header"] [class*="ImportantList_close"]'
    )
    if closer.count() != 1 or not closer.is_visible():
        raise RuntimeError("新消息浮层遮挡页面，但无法定位唯一关闭按钮。")
    closer.click(timeout=5000)
    popup.wait_for(state="hidden", timeout=5000)
    print("已关闭新消息浮层。")
    return True


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

    # 按层级清理：先移除模态遮罩，再处理右侧消息浮层。
    for _ in range(5):
        if find_blocking_modal(page) is None:
            dismiss_new_message_popup(page)
            return
        if not dismiss_notification_modal(page):
            break

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
    dismiss_new_message_popup(page)
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
    report = None

    for attempt in range(1, retries + 1):
        clear_blockers(page)
        # 拟人化：每次点击前加随机小延迟，避免机械的毫秒级连续操作
        human_delay(80, 250)

        try:
            locator.wait_for(state="visible", timeout=timeout)
            human_click(page, locator)
            return
        except PlaywrightTimeoutError as error:
            last_error = error
            report = save_click_diagnostics(page, locator, description, attempt, error)
            print(
                f"点击【{description}】第 {attempt} 次未成功，"
                f"原始错误：{error}"
            )
        except PlaywrightError as error:
            report = save_click_diagnostics(page, locator, description, attempt, error)
            raise _click_failure(description, attempt, error, report) from error

    if last_error is None:
        raise ValueError("点击重试次数必须大于0。")
    raise _click_failure(description, retries, last_error, report) from last_error


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
    report = None

    for attempt in range(1, retries + 1):
        # 模拟看到元素后的反应时间
        human_delay(80, 250)

        try:
            locator.wait_for(state="visible", timeout=timeout)
            human_click(page, locator)
            return
        except PlaywrightTimeoutError as error:
            last_error = error
            report = save_click_diagnostics(page, locator, description, attempt, error)
            print(
                f"点击【{description}】第 {attempt} 次未成功，"
                f"原始错误：{error}"
            )
        except PlaywrightError as error:
            report = save_click_diagnostics(page, locator, description, attempt, error)
            raise _click_failure(description, attempt, error, report) from error

    if last_error is None:
        raise ValueError("点击重试次数必须大于0。")
    raise _click_failure(description, retries, last_error, report) from last_error
