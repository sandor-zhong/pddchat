"""登录态识别 + 聊天搜索页会话入口。

从 pdd_chat.py 抽出：这些函数处理"进入 mms-chat/search 页面 + 校验
登录态"这一层——它们的关注点是 PDD 后台整体的会话/页面加载状态，
不属于"聊天记录抓取"本身。

保留在同一模块的选择器（get_order_search_radio / get_order_input /
get_query_button）是聊天搜索页的顶层入口控件，是登录后立即操作的对象，
放在一起便于并行改造时替换。
"""

from __future__ import annotations

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    TimeoutError as PlaywrightTimeoutError,
)

from blockers import clear_blockers
from paths import DEBUG_DIR, START_URL
from stealth import jitter_wait

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

    # 冷启动 PDD 后台经常 15s+ 才渲染 radio；只等默认 10s 会误判"未登录"，
    # 进而弹出 input() 阻塞交互，批处理场景直接卡死。
    if is_chat_page_ready(page, timeout=20_000):
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
