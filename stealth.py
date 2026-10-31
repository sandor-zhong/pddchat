"""
Stealth 反检测 & 拟人化操作模块

提供:
1. 浏览器指纹伪装 —— 隐藏 navigator.webdriver 等自动化标记
2. init_script 注入 —— 在页面 JS 执行前篡改关键属性
3. Chrome 启动参数优化 —— 禁用自动化检测标志
4. 拟人化操作工具 —— 随机延迟、真实键盘输入、鼠标移动轨迹

用法（替换原来的 playwright.chromium.launch_persistent_context）:
    from stealth import create_stealth_context

    with sync_playwright() as playwright:
        context = create_stealth_context(playwright, user_data_dir=str(PROFILE_DIR))
"""

from __future__ import annotations

import random
import browser_settings
import time
from contextvars import ContextVar
from functools import wraps
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
)

# ============================================================
# 反检测 Init Script
# ============================================================
#
# 通过 context.add_init_script() 注入，比页面自己的 JS 更早执行。
# 拼多多前端风控脚本读 navigator.webdriver、chrome.runtime 等字段
# 来判断当前浏览器是否由自动化工具驱动；这里在这些字段被读取之前
# 就把它们改成正常浏览器的值。
#
# 每个区块都用 try-catch 单独包裹：某个篡改失败不应影响其他项。

_LEGACY_STEALTH_INIT_SCRIPT = r"""
(function () {
    'use strict';

    // ----------------------------------------------------------
    // 1. navigator.webdriver —— 最关键的自动化标记
    //    设为 false 而不是 undefined，因为部分风控同时检查
    //    typeof 结果。
    // ----------------------------------------------------------
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: function () { return false; },
            configurable: true,
        });
    } catch (e) {}

    // ----------------------------------------------------------
    // 2. window.chrome —— Chrome 专有对象，Playwright 默认不提供
    // ----------------------------------------------------------
    try {
        if (typeof window.chrome === 'undefined' || window.chrome === null) {
            window.chrome = {};
        }
        window.chrome.runtime = window.chrome.runtime || {};
        window.chrome.loadTimes = window.chrome.loadTimes || function () {};
        window.chrome.csi = window.chrome.csi || function () {};
        window.chrome.app = window.chrome.app || {};
    } catch (e) {}

    // ----------------------------------------------------------
    // 3. navigator.plugins —— 插件列表不能为空
    //    正常 Chrome 至少包含 PDF Viewer 和 Native Client
    // ----------------------------------------------------------
    try {
        var plugins = navigator.plugins;
        if (!plugins || plugins.length === 0) {
            var makePluginArray = function () {
                var arr = Object.create(PluginArray.prototype);
                var pdfMime = {
                    type: 'application/pdf',
                    suffixes: 'pdf',
                    description: '',
                };
                var pdfMime2 = {
                    type: 'text/pdf',
                    suffixes: 'pdf',
                    description: '',
                };
                arr[0] = Object.create(Plugin.prototype);
                arr[0].name = 'Chrome PDF Plugin';
                arr[0].filename = 'internal-pdf-viewer';
                arr[0].description = 'Portable Document Format';
                arr[0].length = 2;
                arr[0][0] = pdfMime;
                arr[0][1] = pdfMime2;
                arr[1] = Object.create(Plugin.prototype);
                arr[1].name = 'Chrome PDF Viewer';
                arr[1].filename = 'mhjfbmdgcfjbbpaeojofohoefgiehjai';
                arr[1].description = '';
                arr[1].length = 1;
                arr[1][0] = pdfMime;
                arr[2] = Object.create(Plugin.prototype);
                arr[2].name = 'Native Client';
                arr[2].filename = 'internal-nacl-plugin';
                arr[2].description = '';
                arr[2].length = 0;
                arr.length = 3;
                return arr;
            };
            Object.defineProperty(navigator, 'plugins', {
                get: makePluginArray,
                configurable: true,
            });
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 4. navigator.mimeTypes
    // ----------------------------------------------------------
    try {
        var mimeTypes = navigator.mimeTypes;
        if (!mimeTypes || mimeTypes.length === 0) {
            var makeMimeTypeArray = function () {
                var arr = Object.create(MimeTypeArray.prototype);
                arr[0] = {
                    type: 'application/pdf',
                    suffixes: 'pdf',
                    description: '',
                };
                arr[1] = {
                    type: 'text/pdf',
                    suffixes: 'pdf',
                    description: '',
                };
                arr.length = 2;
                return arr;
            };
            Object.defineProperty(navigator, 'mimeTypes', {
                get: makeMimeTypeArray,
                configurable: true,
            });
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 5. navigator.languages
    // ----------------------------------------------------------
    try {
        Object.defineProperty(navigator, 'languages', {
            get: function () { return ['zh-CN', 'zh', 'en-US', 'en']; },
            configurable: true,
        });
    } catch (e) {}

    // ----------------------------------------------------------
    // 6. navigator.vendor —— 必须是 Google Inc.
    // ----------------------------------------------------------
    try {
        Object.defineProperty(navigator, 'vendor', {
            get: function () { return 'Google Inc.'; },
            configurable: true,
        });
    } catch (e) {}

    // ----------------------------------------------------------
    // 7. navigator.hardwareConcurrency —— CPU 逻辑核心数
    //    少于 4 是明显的虚拟机/容器特征
    // ----------------------------------------------------------
    try {
        var hwConcurrency = navigator.hardwareConcurrency;
        if (typeof hwConcurrency === 'undefined' || hwConcurrency < 4) {
            Object.defineProperty(navigator, 'hardwareConcurrency', {
                get: function () { return 8; },
                configurable: true,
            });
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 8. navigator.deviceMemory —— 设备内存 (GB)
    // ----------------------------------------------------------
    try {
        if (!navigator.deviceMemory || navigator.deviceMemory < 2) {
            Object.defineProperty(navigator, 'deviceMemory', {
                get: function () { return 8; },
                configurable: true,
            });
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 9. permissions.query —— 隐藏自动化特有的权限状态
    // ----------------------------------------------------------
    try {
        var origQuery = window.navigator.permissions.query
            .bind(window.navigator.permissions);
        window.navigator.permissions.query = function (parameters) {
            if (parameters.name === 'notifications') {
                return Promise.resolve({
                    state: Notification.permission,
                    onchange: null,
                });
            }
            return origQuery(parameters);
        };
    } catch (e) {}

    // ----------------------------------------------------------
    // 10. window.outerWidth / outerHeight
    //     自动化工具经常让 outer 和 inner 完全一致，这不正常
    // ----------------------------------------------------------
    try {
        (function fixOuter() {
            if (window.outerWidth === 0 || window.outerWidth <= window.innerWidth) {
                try {
                    Object.defineProperty(window, 'outerWidth', {
                        get: function () { return window.innerWidth + 14; },
                        configurable: true,
                    });
                } catch (e2) {}
            }
            if (window.outerHeight === 0 || window.outerHeight <= window.innerHeight) {
                try {
                    Object.defineProperty(window, 'outerHeight', {
                        get: function () { return window.innerHeight + 88; },
                        configurable: true,
                    });
                } catch (e2) {}
            }
        })();
    } catch (e) {}

    // ----------------------------------------------------------
    // 11. 移除过时的自动化工具痕迹
    // ----------------------------------------------------------
    try {
        delete window.callPhantom;
        delete window._phantom;
        delete window.__phantomas;
        delete window.__nightmare;
        delete window.__selenium_unwrapped;
        delete window.__webdriver_evaluate;
        delete window.__driver_evaluate;
        delete window.__webdriver_script_function;
        delete window.__webdriver_script_func;
        delete window.__webdriver_script_fn;
        delete window.__fxdriver_evaluate;
        delete window.__driver_unwrapped;
        delete window.__webdriver_unwrapped;
        delete window.__selenium_evaluate;
        delete window.__$webdriverAsyncExecutor;
        // 移除 playwright 自身的webdriver 标记
        delete Object.getPrototypeOf(navigator).webdriver;
    } catch (e) {}

    // ----------------------------------------------------------
    // 12. screen.colorDepth —— 必须是 24 或 30
    // ----------------------------------------------------------
    try {
        if (screen.colorDepth < 24) {
            Object.defineProperty(screen, 'colorDepth', {
                get: function () { return 24; },
                configurable: true,
            });
            Object.defineProperty(screen, 'pixelDepth', {
                get: function () { return 24; },
                configurable: true,
            });
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 13. navigator.connection.rtt —— 网络延迟不能为 0
    // ----------------------------------------------------------
    try {
        if (navigator.connection) {
            if (!navigator.connection.rtt || navigator.connection.rtt === 0) {
                Object.defineProperty(navigator.connection, 'rtt', {
                    get: function () { return 80 + Math.floor(Math.random() * 40); },
                    configurable: true,
                });
            }
            if (!navigator.connection.downlink) {
                Object.defineProperty(navigator.connection, 'downlink', {
                    get: function () { return 10; },
                    configurable: true,
                });
            }
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 14. document.hidden / visibilityState
    //     确保页面看起来"可见"（headless 下可能始终 hidden）
    // ----------------------------------------------------------
    try {
        if (document.hidden) {
            Object.defineProperty(document, 'hidden', {
                get: function () { return false; },
                configurable: true,
            });
            Object.defineProperty(document, 'visibilityState', {
                get: function () { return 'visible'; },
                configurable: true,
            });
        }
    } catch (e) {}

    // ----------------------------------------------------------
    // 15. 阻止 navigator 属性的 toString 检测
    //     部分风控会检测 Object.prototype.toString.call(navigator.webdriver)
    // ----------------------------------------------------------
    try {
        var iframe = document.createElement('iframe');
        iframe.style.display = 'none';
        document.head && document.head.appendChild(iframe);
        var contentWindow = iframe.contentWindow;
        if (contentWindow) {
            var cleanFnToString = contentWindow.Function.prototype.toString;
            // 如果检测到 toString 被篡改过则恢复
        }
        iframe.remove();
    } catch (e) {}

    // Canvas 与 WebGL 保持 Chromium 原生实现。同一台设备、同一个持久化
    // profile 的结果应稳定；逐次读取时注入随机噪声反而会形成异常漂移。

})();
"""

# 当前实际使用的稳定脚本。保留旧常量是为了兼容可能直接导入它的外部代码，
# 新建上下文不再篡改插件、硬件、Canvas、WebGL、网络或屏幕参数。
STABLE_FINGERPRINT_INIT_SCRIPT = r"""
(function () {
    'use strict';
    try {
        Object.defineProperty(Navigator.prototype, 'webdriver', {
            get: function () { return false; },
            configurable: true,
        });
    } catch (e) {}
    try {
        delete window.callPhantom;
        delete window._phantom;
        delete window.__phantomas;
        delete window.__nightmare;
        delete window.__selenium_unwrapped;
        delete window.__webdriver_evaluate;
        delete window.__driver_evaluate;
        delete window.__webdriver_script_function;
        delete window.__webdriver_script_func;
        delete window.__webdriver_script_fn;
        delete window.__fxdriver_evaluate;
        delete window.__driver_unwrapped;
        delete window.__webdriver_unwrapped;
        delete window.__selenium_evaluate;
        delete window.__$webdriverAsyncExecutor;
    } catch (e) {}
})();
"""

# 对外名称保持兼容，但指向新的稳定实现。
STEALTH_INIT_SCRIPT = STABLE_FINGERPRINT_INIT_SCRIPT


# ============================================================
# Chrome 启动参数（隐藏自动化特征）
# ============================================================

STEALTH_ARGS = [
    # ---- 核心反检测 ----
    "--disable-blink-features=AutomationControlled",

    # ---- 减少噪声 ----
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-sync",

    # ---- 不弹干扰提示 ----
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-infobars",
    "--start-maximized",
]

# 从默认启动参数中移除的（暴露自动化身份的参数）
STEALTH_IGNORE_DEFAULT_ARGS = [
    "--enable-automation",
]


# ============================================================
# 上下文创建 —— 替换原来的 launch_persistent_context
# ============================================================

def install_warn_center_cleanup(context: BrowserContext) -> None:
    """清理防控中心标签；监听导航以覆盖先打开 about:blank 的弹窗。"""
    def watch(page: Page) -> None:
        def check(frame=None) -> None:
            try:
                if page.is_closed() or (frame is not None and frame != page.main_frame):
                    return
                url = urlsplit(page.url)
                if (url.scheme == "https" and url.netloc == "mms.pinduoduo.com"
                        and url.path == "/orders/warnCenter"):
                    page.close(run_before_unload=False)
                    print("[标签页清理] 已自动关闭无关的防控中心标签页", flush=True)
            except Exception as exc:
                # 弹窗关闭/导航竞争不能中断订单流程。
                print(f"[标签页清理] 防控中心检查或关闭未完成：{exc}", flush=True)

        def detach(*_args) -> None:
            page.remove_listener("framenavigated", check)
            page.remove_listener("close", detach)

        page.on("framenavigated", check)
        page.on("close", detach)
        check()

    context.on("page", watch)
    for page in list(context.pages):
        watch(page)


def create_stealth_context(
    playwright: Any,
    user_data_dir: str | Path,
    headless: bool = False,
    viewport: dict | None = None,
    locale: str = "zh-CN",
    timezone_id: str = "Asia/Shanghai",
    user_agent: str | None = None,
    close_warn_center: bool = False,
    native_properties: bool | None = None,
    browser_engine: str | None = None,
    **extra_context_args: Any,
) -> BrowserContext:  
    """
    创建带反检测能力的持久化浏览器上下文。

    这个方法是对 playwright.chromium.launch_persistent_context 的
    封装，按运行控制页选择 Chrome 或 Firefox，使用各自的登录目录。
    Chrome 专用启动参数只用于 Chrome；Firefox 使用 Playwright 配套版本。

    参数:
        playwright: sync_playwright() 返回的 playwright 对象
        user_data_dir: 持久化用户数据目录（登录态保存在这里）
        headless:     是否无头模式（强烈建议始终 False）
        viewport:     可选固定视口；默认跟随真实浏览器窗口
        locale:       浏览器语言，默认 zh-CN
        timezone_id:  时区，默认 Asia/Shanghai
        **extra_context_args: 传给 launch_persistent_context 的额外参数

    返回:
        已注入反检测脚本的 BrowserContext 实例
    """

    if native_properties is None:
        native_properties = browser_settings.native_properties_enabled()
    engine = browser_settings.browser_engine() if browser_engine is None else browser_engine
    profile = browser_settings.profile_directory(user_data_dir, engine)

    context_args: dict[str, Any] = {
        "user_data_dir": str(profile),
        "headless": headless,

        # 语言和时区指纹
        "locale": locale,
        "timezone_id": timezone_id,

        # HTTPS 证书错误兼容
        "ignore_https_errors": True,

    }
    if engine == "chromium":
        context_args["ignore_default_args"] = STEALTH_IGNORE_DEFAULT_ARGS
        context_args["args"] = STEALTH_ARGS

    # 默认跟随真实浏览器窗口，不制造与宿主屏幕矛盾的固定 viewport。
    # 调用方确实需要固定尺寸时仍可显式传入 viewport。
    if viewport is None:
        context_args["no_viewport"] = True
    else:
        context_args["viewport"] = viewport
        context_args["no_viewport"] = False

    # 显式设置 User-Agent（防止 Playwright UA 含可检测的 Chromium 版本异常）
    if user_agent is not None:
        context_args["user_agent"] = user_agent

    context_args.update(extra_context_args)
    if context_args.get("headless"):
        raise ValueError("系统鼠标操作需要可见的浏览器窗口，不能使用 headless 模式。")

    chrome_path = browser_settings.chrome_executable() if engine == "chromium" else ""
    if chrome_path and "executable_path" not in extra_context_args:
        if not Path(chrome_path).is_file():
            raise RuntimeError(f"所选 Chrome 文件不存在，请在运行控制页重新选择：{chrome_path}")
        context_args.pop("channel", None)
        context_args["executable_path"] = chrome_path

    if native_properties and engine == "chromium":
        context_args["args"] = [arg for arg in context_args.get("args", [])
                                if not arg.startswith("--disable-blink-features=AutomationControlled")]
        context_args.pop("ignore_default_args", None)

    browser_type = playwright.firefox if engine == "firefox" else playwright.chromium
    context = browser_type.launch_persistent_context(**context_args)
    from native_mouse import install_system_mouse
    try:
        install_system_mouse(context)
    except Exception:
        context.close()
        raise

    # 在所有页面（现有 + 未来新开的）加载前注入反检测脚本
    if not native_properties:
        context.add_init_script(STEALTH_INIT_SCRIPT)

    if close_warn_center:
        install_warn_center_cleanup(context)

    return context


# ============================================================
# 拟人化操作工具
# ============================================================

_business_pace: ContextVar[float | None] = ContextVar("business_pace", default=None)


def paced_business_operations(function):
    """为一单业务设置独立节奏；嵌套流程复用，退出或异常时恢复。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        if _business_pace.get() is not None:
            return function(*args, **kwargs)
        token = _business_pace.set(random.uniform(0.8, 1.4))
        try:
            return function(*args, **kwargs)
        finally:
            _business_pace.reset(token)
    return wrapped


def without_business_pacing(function):
    """验证流程使用自己的超时和节奏，不叠加业务阅读停顿。"""
    @wraps(function)
    def wrapped(*args, **kwargs):
        token = _business_pace.set(None)
        try:
            return function(*args, **kwargs)
        finally:
            _business_pace.reset(token)
    return wrapped


def business_pause(page: Page, *, reading: bool = False) -> None:
    """动作边界的有界停顿，不代替页面就绪条件，也不产生额外请求。"""
    pace = _business_pace.get()
    if pace is None:
        return
    if reading:
        low, high = 3500, 6000
    else:
        draw = random.random()
        low, high = (1500, 2400) if draw < 0.55 else (2500, 3600) if draw < 0.9 else (3800, 5000)
    page.wait_for_timeout(min(8000, max(1200, int(varied_interval(low, high) * pace))))


def business_read_page(page: Page) -> None:
    """页面就绪后短距离浏览；只滚动文档，不触碰虚拟列表或弹窗。"""
    if _business_pace.get() is None:
        return
    business_pause(page, reading=True)
    # 使用文档滚动元素而不是鼠标当前位置，避免滚到下拉菜单、聊天记录等。
    scroller = page.evaluate_handle("() => document.scrollingElement")
    before = None
    try:
        info = scroller.evaluate("""node => {
            const blocked = [...document.querySelectorAll(
                '#slide-captcha-dialog, .captcha-wrapper, [role="dialog"], '
                + '[data-testid="beast-core-modal-innerWrapper"]'
            )].some(el => el.getClientRects().length &&
                getComputedStyle(el).visibility !== 'hidden');
            if (!node || blocked) return null;
            return {top: node.scrollTop, maximum: node.scrollHeight - node.clientHeight};
        }""")
        if not isinstance(info, dict) or info.get("maximum", 0) <= 4:
            return
        before = info["top"]
        distance = random.randint(180, 360)
        target = min(info["maximum"], before + distance)
        if target - before < 40:
            target = max(0, before - distance)
        for step in range(1, 5):
            scroller.evaluate("(node, top) => node.scrollTo({top, behavior: 'instant'})",
                              before + (target - before) * step / 4)
            page.wait_for_timeout(random.randint(180, 320))
        business_pause(page, reading=True)
        for step in range(1, 5):
            scroller.evaluate("(node, top) => node.scrollTo({top, behavior: 'instant'})",
                              target + (before - target) * step / 4)
            page.wait_for_timeout(random.randint(180, 320))
    finally:
        # 不遗留 JSHandle；异常时也恢复位置，不影响后续截图和订单定位。
        try:
            if before is not None:
                scroller.evaluate("(node, top) => node.scrollTo({top, behavior: 'instant'})", before)
        finally:
            scroller.dispose()


def varied_interval(min_ms: float, max_ms: float) -> float:
    """有界混合分布：多数短停顿，部分中停顿，少量长停顿。"""
    if min_ms < 0 or max_ms < min_ms:
        raise ValueError("Invalid delay bounds")
    draw = random.random()
    fraction = (random.betavariate(2, 5) if draw < 0.65 else
                random.betavariate(3, 3) if draw < 0.9 else
                random.betavariate(5, 1.5))
    return min_ms + (max_ms - min_ms) * fraction


def human_delay(min_ms: int = 200, max_ms: int = 800) -> None:
    """
    模拟人类操作间隔的随机延迟。

    真实用户"看完内容 → 决定操作 → 执行"之间天然存在随机停顿，
    不存在 0ms 的连续机械操作。默认 200~800ms 覆盖大部分场景。
    """
    delay_seconds = varied_interval(min_ms, max_ms) / 1000.0
    time.sleep(delay_seconds)


def jitter_wait(page: Page, base_ms: int, jitter_ratio: float = 0.25) -> None:
    """
    在固定等待基础上添加随机抖动，避免所有超时都是精确相同的值。

    机械的固定等待（如每隔正好 500ms）是反爬算法的易检测特征。
    本函数在 base_ms ± jitter_ratio 范围内随机化延迟。

    参数:
        page:         Playwright Page
        base_ms:      基础等待时间（毫秒）
        jitter_ratio: 抖动比例，默认 ±25%
    """
    jitter = int(base_ms * jitter_ratio)
    actual_ms = int(varied_interval(max(0, base_ms - jitter), base_ms + jitter))
    actual_ms = max(50, actual_ms)  # 不低于 50ms
    page.wait_for_timeout(actual_ms)


def paste_text(page: Page, locator: Locator, text: str) -> None:
    """替换输入内容并校验；Firefox 使用原生填充，Chrome 保留剪贴板粘贴。"""
    browser = page.context.browser
    if browser is not None and browser.browser_type.name == "firefox":
        locator.fill(text)
        from playwright.sync_api import expect
        expect(locator).to_have_value(text, timeout=3000)
        return
    origin = urlsplit(page.url)
    page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=f"{origin.scheme}://{origin.netloc}",
    )
    locator.focus()
    page.evaluate("text => navigator.clipboard.writeText(text)", text)
    locator.press("Control+A")
    locator.press("Control+V")
    from playwright.sync_api import expect
    expect(locator).to_have_value(text, timeout=3000)


def human_type(
    page: Page,
    locator: Locator,
    text: str,
    click_first: bool = True,
    min_delay: int = 40,
    max_delay: int = 150,
) -> None:  
    """
    模拟真人键盘输入 —— 逐字符输入，每两次按键间有随机间隔。

    熟练中文输入者速度约 50~80 WPM，字符间隔在 40~150ms。
    数字和特殊字符会额外加一点延迟（需要眼睛确认）。

    参数:
        page:        Playwright Page
        locator:     输入框 Locator
        text:        要输入的文本
        click_first: 是否先点击输入框获得焦点
        min_delay:   最小按键间隔 (ms)
        max_delay:   最大按键间隔 (ms)
    """
    if click_first:
        human_click(page, locator)
        human_delay(100, 300)

    next_pause = random.randint(6, 14)
    for i, char in enumerate(text):
        delay = random.randint(min_delay, max_delay)

        # 数字和特殊符号需要更多确认时间
        if char.isdigit() or char in "-_/@:.#":
            delay = random.randint(70, 180)

        pace = _business_pace.get()
        if pace is not None:
            delay = max(delay, min(450, int(random.uniform(200, 320) * pace)))

        # 每 6~14 个字符有一个自然停顿（呼吸 / 换手）
        if i == next_pause:
            human_delay(200, 500)
            business_pause(page)
            next_pause += random.randint(6, 14)

        page.keyboard.type(char, delay=delay)

    human_delay(100, 300)
    business_pause(page, reading=True)


def human_click(
    page: Page,
    locator: Locator,
    move_before_click: bool = True,
) -> None:
    """
    模拟人类点击：先缓慢移动鼠标到目标，再做一次短暂的悬停，最后点击。

    直接"闪现"到元素上的点击是明显的程序特征。
    这里用三次贝塞尔曲线生成自然的鼠标移动轨迹。

    参数:
        page:             Playwright Page
        locator:          目标元素 Locator
        move_before_click: 是否先走一遍鼠标轨迹（关掉则为带延时的直接点击）
    """
    if not move_before_click:
        business_pause(page)
        human_delay(50, 200)
        locator.click()
        return

    business_pause(page)
    try:
        locator.wait_for(state="visible", timeout=8_000)
        locator.scroll_into_view_if_needed(timeout=8_000)
        box = locator.bounding_box()
        if box:
            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

            # 起始位置：页面左上角到中央之间的随机点
            start_x, start_y = getattr(page, "_pdd_mouse_position", (target_x, target_y))

            # 贝塞尔控制点（制造自然弧度）
            cp1_x = start_x + (target_x - start_x) * random.uniform(0.25, 0.55)
            cp1_y = start_y + random.uniform(-60, 60)
            cp2_x = target_x + (start_x - target_x) * random.uniform(0.10, 0.30)
            cp2_y = target_y + random.uniform(-40, 40)

            steps = random.randint(6, 14)
            for i in range(steps + 1):
                progress = i / steps
                t = progress * progress * (3 - 2 * progress)
                x = (
                    (1 - t) ** 3 * start_x
                    + 3 * (1 - t) ** 2 * t * cp1_x
                    + 3 * (1 - t) * t ** 2 * cp2_x
                    + t ** 3 * target_x
                )
                y = (
                    (1 - t) ** 3 * start_y
                    + 3 * (1 - t) ** 2 * t * cp1_y
                    + 3 * (1 - t) * t ** 2 * cp2_y
                    + t ** 3 * target_y
                )
                page.mouse.move(x, y)
                # 接近终点时略微减速（模拟瞄准动作）
                if i > steps * 0.7:
                    time.sleep(varied_interval(12, 35) / 1000)
                else:
                    time.sleep(varied_interval(3, 15) / 1000)

            # 微调保持在目标内部；最终由 Locator 执行可点击性检查。
            for _ in range(random.randint(1, 3)):
                page.mouse.move(target_x + random.uniform(-1, 1),
                                target_y + random.uniform(-1, 1))
                human_delay(15, 70)
            human_delay(40, 180)
            locator.click(position={"x": target_x - box["x"],
                                    "y": target_y - box["y"]},
                          delay=varied_interval(35, 130), timeout=8000)
            page._pdd_mouse_position = (target_x, target_y)
            return
    except Exception:
        # bounding_box 获取失败 / 页面不在前台 → 保留可靠回退。
        locator.click()
        return

    locator.click()


def human_scroll(
    page: Page,
    delta_y: int | None = None,
    min_steps: int = 3,
    max_steps: int = 8,
) -> None:
    """
    模拟人类滚动：分多步逐渐完成，不是一次性瞬移。

    参数:
        page:      Playwright Page
        delta_y:   滚动距离（正值向下），None 时随机 200~700
        min_steps: 最小步数
        max_steps: 最大步数
    """
    if delta_y is None:
        delta_y = random.randint(200, 700)

    steps = random.randint(min_steps, max_steps)
    weights = [random.lognormvariate(0, 0.45) for _ in range(steps)]
    total_weight = sum(weights)
    moved = 0.0
    for index, weight in enumerate(weights):
        # 步幅不同，但总距离保持调用方要求，避免改变列表扫描范围。
        actual = delta_y - moved if index == steps - 1 else delta_y * weight / total_weight
        page.mouse.wheel(0, actual)
        moved += actual
        time.sleep(varied_interval(25, 160) / 1000)
        if index == steps // 2:
            business_pause(page)
    business_pause(page, reading=True)


def human_browse_scroll(
    page: Page,
    scroll_count: int | None = None,
) -> None:
    """
    模拟用户逐屏浏览：滚几屏 → 停下来阅读 → 再滚几屏。

    参数:
        page:         Playwright Page
        scroll_count: 滚动次数，None 时随机 2~5
    """
    if scroll_count is None:
        scroll_count = random.randint(2, 5)

    for _ in range(scroll_count):
        human_scroll(page)
        human_delay(800, 3000)  # 阅读停顿


def random_mouse_wander(
    page: Page,
    max_x: int = 1400,
    max_y: int = 800,
) -> None:
    """
    鼠标在页面上无目的游走（模拟用户看页面时手在鼠标上的自然晃动）。
    """
    try:
        x = random.uniform(100, max_x)
        y = random.uniform(100, max_y)
        page.mouse.move(x, y)
        human_delay(400, 1800)
    except Exception:
        pass


# ============================================================

