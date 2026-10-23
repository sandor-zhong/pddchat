"""登录态识别 + 聊天搜索页会话入口。

从 pdd_chat.py 抽出：这些函数处理"进入 mms-chat/search 页面 + 校验
登录态"这一层——它们的关注点是 PDD 后台整体的会话/页面加载状态，
不属于"聊天记录抓取"本身。

保留在同一模块的选择器（get_order_search_radio / get_order_input /
get_query_button）是聊天搜索页的顶层入口控件，是登录后立即操作的对象，
放在一起便于并行改造时替换。
"""

from __future__ import annotations

import math
import threading
import time
from urllib.parse import urlparse

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from blockers import clear_blockers
from db import LOGIN_REQUIRED_DETAIL
from paths import DEBUG_DIR, START_URL
from stealth import jitter_wait, business_read_page

# wait_for_login_polling 的默认轮询间隔（秒）与总超时（秒，15 分钟）。
LOGIN_POLL_INTERVAL = 2.5
LOGIN_POLL_TOTAL_TIMEOUT = 900.0
ACCOUNT_LOGIN_URL = "https://mms.pinduoduo.com/login/"

# 只识别可见登录面板内同时存在的两个入口，不依赖随版本变化的样式类名。
LOGIN_PAGE_SELECTOR = (
    '.login-info-section:visible'
    ':has(.login-tab [data-tracking-click-viewid="scan_code"]:visible)'
    ':has(.login-tab [data-tracking-click-viewid="password_log"]:visible)'
)


class LoginRequiredError(RuntimeError):
    """业务页面显示登录面板，当前店铺需要重新登录。"""


class AccountLoginError(RuntimeError):
    """A safe account-login error that never contains the submitted password."""


def account_login(
    context: BrowserContext,
    page: Page,
    username: str,
    password: str,
    stop_event: threading.Event,
    progress=lambda _message: None,
    verification_timeout: float = 300.0,
    login_timeout: float = 60.0,
) -> tuple[bool, str]:
    """Fill the password-login form and wait through human verification."""

    def check() -> None:
        if stop_event.is_set():
            raise AccountLoginError("批量登录已停止。")
        if page.is_closed():
            raise AccountLoginError("登录窗口已关闭。")

    def visible(selector: str) -> bool:
        matches = page.locator(selector)
        return any(matches.nth(index).is_visible() for index in range(matches.count()))

    try:
        progress("正在打开拼多多账号登录页")
        context.clear_cookies()
        page.goto(ACCOUNT_LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        check()
        if urlparse(page.url).hostname != "mms.pinduoduo.com":
            raise AccountLoginError("登录页地址异常，未输入账号密码。")
        page.evaluate("() => { localStorage.clear(); sessionStorage.clear(); }")
        context.clear_cookies()
        page.goto(ACCOUNT_LOGIN_URL, wait_until="domcontentloaded", timeout=45_000)
        check()

        account_tab = page.locator('[data-tracking-click-viewid="password_log"]:visible').first
        account_tab.wait_for(state="visible", timeout=20_000)
        account_tab.click()
        username_input = page.locator("#usernameId:visible").first
        password_input = page.locator("#passwordId:visible").first
        username_input.fill("")
        password_input.fill("")
        check()
        username_input.fill(username)
        password_input.fill(password)
        check()
        progress("账号密码已填写，正在提交登录")
        page.locator(
            'button[data-tracking-click-viewid="account_login_confirmation"]:visible'
        ).first.click()

        normal_deadline = time.monotonic() + login_timeout
        challenge_deadline: float | None = None
        challenge_kind = ""
        last_reported: tuple[str, int] | None = None
        while True:
            page = find_mms_page(context=context, default_page=page)
            check()
            if visible('.verify-phone-container, input[placeholder="请输入短信验证码"]'):
                current_challenge = "短信验证"
            elif visible('#slide-captcha-dialog, .captcha-wrapper__slider'):
                current_challenge = "滑块拼图验证"
            else:
                current_challenge = ""

            if current_challenge:
                if challenge_deadline is None:
                    challenge_deadline = time.monotonic() + verification_timeout
                    challenge_kind = current_challenge
                    page.bring_to_front()
                remaining = math.ceil(challenge_deadline - time.monotonic())
                if remaining <= 0:
                    raise AccountLoginError(
                        f"{challenge_kind}等待超时（最多 {int(verification_timeout)} 秒）。"
                    )
                report = (current_challenge, remaining)
                if last_reported is None or current_challenge != last_reported[0] or remaining % 5 == 0:
                    action = "输入短信验证码并点击“确认”" if current_challenge == "短信验证" else "拖动滑块完成拼图"
                    progress(f"等待人工{current_challenge}，请在浏览器中{action}；剩余 {remaining} 秒")
                    last_reported = report
            else:
                location = urlparse(page.url)
                if location.hostname == "mms.pinduoduo.com" and not location.path.startswith("/login"):
                    progress("登录页面已通过，正在验证登录状态")
                    return True, "账号登录成功，登录状态已保存。"
                if visible('.mms-header__user-info'):
                    return True, "账号登录成功，登录状态已保存。"
                error = page.get_by_text("账号或密码错误", exact=False)
                if any(error.nth(index).is_visible() for index in range(error.count())):
                    raise AccountLoginError("账号或密码错误。")
                if challenge_deadline is None and time.monotonic() >= normal_deadline:
                    raise AccountLoginError("登录提交后未检测到成功或验证页面，请检查页面提示。")
                if challenge_deadline is not None and time.monotonic() >= challenge_deadline:
                    raise AccountLoginError(f"{challenge_kind}完成后仍未检测到登录成功。")
            page.wait_for_timeout(150)
    except AccountLoginError:
        raise
    except Exception:  # noqa: BLE001 - Playwright call logs can contain the password
        check()
        raise AccountLoginError("账号登录操作未完成，请检查网络或登录页面后重试。") from None


def is_login_url(url: str) -> bool:
    """只匹配拼多多商家登录地址，忽略查询参数、锚点及末尾斜杠。"""
    parsed = urlparse(url)
    return (
        parsed.scheme in {"http", "https"}
        and parsed.hostname == "mms.pinduoduo.com"
        and parsed.path.rstrip("/") == "/login"
    )


def is_login_page(page: Page) -> bool:
    """优先识别登录页重定向，同时兼容原网址内弹出的登录面板。"""
    if isinstance(page.url, str) and is_login_url(page.url):
        return True
    return page.locator(LOGIN_PAGE_SELECTOR).first.is_visible()


def raise_if_login_page(page: Page) -> None:
    if is_login_page(page):
        raise LoginRequiredError(
            "检测到拼多多登录页（扫码登录 / 账号登录），店铺尚未登录或登录已失效。"
            "请在「店铺管理」中右键该店铺，选择「重新登录」，完成后再次查询。"
        )


def wait_for_content_or_login(page: Page, content: Locator, timeout: int = 20_000) -> None:
    """等待业务内容，并持续检查登录重定向及登录面板。"""
    raise_if_login_page(page)
    candidate = content.filter(visible=True).or_(page.locator(LOGIN_PAGE_SELECTOR)).first
    deadline = time.monotonic() + timeout / 1000 if timeout > 0 else None
    while True:
        remaining_ms = max(1, int((deadline - time.monotonic()) * 1000)) if deadline is not None else 500
        try:
            candidate.wait_for(state="visible", timeout=min(500, remaining_ms))
        except PlaywrightTimeoutError:
            raise_if_login_page(page)
            if deadline is not None and time.monotonic() >= deadline:
                raise
            continue
        raise_if_login_page(page)
        return

# ============================================================
# 页面基础操作
# ============================================================

def open_chat_page(page: Page) -> None:
    """进入拼多多聊天记录查询页面。"""

    # 复议流程会保留一个专用聊天标签页并连续查询多笔订单。已经处于
    # 查询页时直接复用当前页面，避免每笔订单都 goto 一次、页面来回跳。
    if "/mms-chat/search" in page.url and is_chat_page_ready(page, timeout=1_500):
        clear_blockers(page)
        print("复用已打开的聊天记录页面。")
        return
    
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
    if is_chat_page_ready(page, timeout=1_500):
        business_read_page(page)


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
    interactive: bool = True,
) -> Page:
    """
    检测和处理登录状态。

    interactive=True（默认，人工盯着终端跑单店铺场景）：检测不到登录状态
    就打印提示、input() 阻塞等人工完成登录后回车继续，行为跟以前一样。

    interactive=False（main.py --serve 常驻模式）：调度器起的子进程没人
    守在终端，stdin 被调度器占着发协议指令——这时候如果还用 input() 等
    人工登录，子进程会直接卡死（既不报错也不退出，调度器只能干等到超时）。
    所以 interactive=False 时检测不到登录状态直接抛异常，不等待、不阻塞；
    调用方（run_serve_mode 的 init 处理）会把这个异常转成协议里的 error
    消息，调度器收到后立刻把这家店铺记录下来、跳过、换下一家还没处理过的
    店铺补位，而不是卡住一整个响应超时的时长才反应过来。

    返回最终使用的 Page，因为登录过程中可能出现新标签页。
    """

    open_chat_page(page)

    # 冷启动 PDD 后台经常 15s+ 才渲染 radio；只等默认 10s 会误判"未登录"，
    # 进而弹出 input() 阻塞交互，批处理场景直接卡死。
    if is_chat_page_ready(page, timeout=20_000):
        print("检测到已有登录状态。")
        print(f"当前页面：{page.url}")
        return page

    if not interactive:
        raise RuntimeError(LOGIN_REQUIRED_DETAIL)

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


def wait_for_login_polling(
    context: BrowserContext,
    page: Page,
    poll_interval: float = LOGIN_POLL_INTERVAL,
    timeout: float = LOGIN_POLL_TOTAL_TIMEOUT,
) -> tuple[bool, str]:
    """检测登录状态的轮询变体：不调用阻塞的 input()，每隔 poll_interval
    秒检查一次是否已经进入聊天记录查询页面，直到成功或总超时。

    供 main.py 的 --login-only 模式用（gui/ 子项目"新增店铺/重新登录"
    功能背后的子进程）——GUI 没有终端可以让人在那按回车，只能靠这种轮询
    方式判断"人工是不是已经在弹出的浏览器里完成登录了"。跟
    wait_for_login(interactive=True) 的区别只在"怎么知道登录完成"，
    其余逻辑（登录可能发生在新标签页、要重新定位 mms 页面、重新进入
    聊天记录页面校验）保持一致。

    不会抛异常（Playwright 本身出现意外错误除外）——调用方直接把返回的
    message 当作协议里的 message 字段上报即可，不需要额外 try/except
    包一层来转换异常文案。

    返回 (success, message)：
    - 一开始就已经是登录状态：(True, "检测到已有登录状态")；
    - 轮询期间检测到登录完成：(True, "登录成功，已进入聊天记录查询页面")；
    - 超过 timeout 仍未检测到：(False, "等待 N 秒后仍未检测到登录完成")。
    """

    open_chat_page(page)

    # 冷启动 PDD 后台经常 15s+ 才渲染 radio，用跟 wait_for_login 一致的
    # 20s 首次检测超时，避免刚打开就误判"未登录"。
    if is_chat_page_ready(page, timeout=20_000):
        return True, "检测到已有登录状态"

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(poll_interval)

        # 登录过程中页面可能跳转到新标签页，每轮都重新定位一次 mms 页面
        # （复用 find_mms_page，跟 wait_for_login 阻塞回车之后的做法一致）。
        page = find_mms_page(context=context, default_page=page)

        # 短超时快速探测；命中了再重新进一次聊天记录页面做完整校验，
        # 避免探测到的只是刚跳转过去、还没完全导航好的中间态。
        if is_chat_page_ready(page, timeout=1_000):
            open_chat_page(page)
            if is_chat_page_ready(page, timeout=10_000):
                return True, "登录成功，已进入聊天记录查询页面"

    return False, f"等待 {int(timeout)} 秒后仍未检测到登录完成"
