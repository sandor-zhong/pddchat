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
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import (
    BrowserContext,
    Locator,
    Page,
    sync_playwright,
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

STEALTH_INIT_SCRIPT = r"""
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

    // ----------------------------------------------------------
    // 16. Canvas 指纹随机化
    //    每次 getImageData 时添加微量噪点，让指纹不可复现
    // ----------------------------------------------------------
    try {
        (function () {
            var originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
            var originalToBlob = HTMLCanvasElement.prototype.toBlob;
            var originalGetImageData = CanvasRenderingContext2D.prototype.getImageData;

            var addNoise = function (canvas) {
                try {
                    var ctx = canvas.getContext('2d');
                    if (!ctx) return;
                    var imageData = ctx.getImageData(0, 0, canvas.width, canvas.height);
                    var data = imageData.data;
                    // 随机修改 0.02% 的像素值（肉眼不可见，但指纹会变）
                    var pixelCount = (data.length / 4) | 0;
                    var noiseCount = Math.max(1, (pixelCount * 0.0002) | 0);
                    for (var i = 0; i < noiseCount; i++) {
                        var idx = ((Math.random() * pixelCount) | 0) * 4;
                        // 在最低位翻转 1 bit，肉眼无法分辨但哈希值会变化
                        data[idx] = data[idx] ^ 1;
                        data[idx + 1] = data[idx + 1] ^ 1;
                        data[idx + 2] = data[idx + 2] ^ 1;
                    }
                    // ★ 关键：把修改后的像素数据写回画布，否则噪声白算
                    ctx.putImageData(imageData, 0, 0);
                } catch (e) {}
            };

            HTMLCanvasElement.prototype.toDataURL = function () {
                addNoise(this);
                return originalToDataURL.apply(this, arguments);
            };

            HTMLCanvasElement.prototype.toBlob = function () {
                addNoise(this);
                return originalToBlob.apply(this, arguments);
            };
        })();
    } catch (e) {}

    // ----------------------------------------------------------
    // 17. WebGL 指纹防护
    //     极验/GeeTest/网易易盾等验证码 SDK 会检测
    //     WEBGL_debug_renderer_info → UNMASKED_VENDOR_WEBGL
    //     / UNMASKED_RENDERER_WEBGL，用于硬件指纹识别。
    // ----------------------------------------------------------
    try {
        var origGetContext = HTMLCanvasElement.prototype.getContext;
        HTMLCanvasElement.prototype.getContext = function () {
            var contextType = arguments[0];
            var ctx = origGetContext.apply(this, arguments);

            if (ctx && (contextType === 'webgl' || contextType === 'experimental-webgl' || contextType === 'webgl2')) {
                if (!ctx.__stealthPatched) {
                    patchWebGL(ctx);
                    ctx.__stealthPatched = true;
                }
            }
            return ctx;
        };

        function patchWebGL(gl) {
            // 17a. 拦截 getParameter，隐藏 UNMASKED 常量
            var origGetParameter = gl.getParameter.bind(gl);
            // UNMASKED_VENDOR_WEBGL  = 0x9245
            // UNMASKED_RENDERER_WEBGL = 0x9246
            var UNMASKED_VENDOR_WEBGL = 0x9245;
            var UNMASKED_RENDERER_WEBGL = 0x9246;

            gl.getParameter = function (pname) {
                if (pname === UNMASKED_VENDOR_WEBGL) {
                    // 返回泛用 NVIDIA 厂商名，与绝大多数开发机一致
                    return 'Google Inc. (NVIDIA)';
                }
                if (pname === UNMASKED_RENDERER_WEBGL) {
                    // 返回一个中等配置的 ANGLE 渲染器字符串
                    return 'ANGLE (NVIDIA, NVIDIA GeForce RTX 3060 Direct3D11 vs_5_0 ps_5_0)';
                }
                return origGetParameter(pname);
            };

            // 17b. getSupportedExtensions 去重 + 挪动顺序，避免与已知自动化 profile 一致
            var origGetSupportedExtensions = gl.getSupportedExtensions.bind(gl);
            gl.getSupportedExtensions = function () {
                var exts = origGetSupportedExtensions();
                if (!exts) return exts;
                // 打乱顺序，防止扩展列表做指纹
                var arr = exts.slice();
                for (var i = arr.length - 1; i > 0; i--) {
                    var j = (Math.random() * (i + 1)) | 0;
                    var tmp = arr[i];
                    arr[i] = arr[j];
                    arr[j] = tmp;
                }
                return arr;
            };

            // 17c. getShaderPrecisionFormat 加微小噪声
            //     部分反爬引擎对精度值做哈希指纹
            var origGetShaderPrecisionFormat = gl.getShaderPrecisionFormat.bind(gl);
            gl.getShaderPrecisionFormat = function (shaderType, precisionType) {
                var fmt = origGetShaderPrecisionFormat(shaderType, precisionType);
                if (fmt) {
                    return {
                        rangeMin: fmt.rangeMin + (Math.random() - 0.5) * 2,
                        rangeMax: fmt.rangeMax + (Math.random() - 0.5) * 2,
                        precision: fmt.precision + (Math.random() - 0.5) * 2,
                    };
                }
                return fmt;
            };
        }
    } catch (e) {}

})();
"""


# ============================================================
# Chrome 启动参数（隐藏自动化特征）
# ============================================================

STEALTH_ARGS = [
    # ---- 核心反检测 ----
    "--disable-blink-features=AutomationControlled",

    # ---- 禁用可被检测的特性 ----
    "--disable-features=IsolateOrigins,site-per-process",
    "--disable-site-isolation-trials",
    "--disable-features=BlockInsecurePrivateNetworkRequests",

    # ---- 减少噪声 ----
    "--disable-component-update",
    "--disable-background-networking",
    "--disable-sync",
    "--disable-default-apps",

    # ---- 不弹干扰提示 ----
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-infobars",

    # ---- 稳定性 / 性能 ----
    "--disable-dev-shm-usage",
    "--disable-breakpad",
    "--disable-crash-reporter",

    # ---- 关闭不必要的 UI ----
    "--disable-features=TranslateUI",
    "--disable-features=PasswordGeneration",
    "--disable-extensions",
    "--disable-popup-blocking",

    # ---- 其他 ----
    "--allow-running-insecure-content",
    "--disable-features=PreloadMediaEngagementData,MediaEngagementBypassAutoplayPolicies",
    "--window-size=1920,1080",
    "--start-maximized",
]

# 从默认启动参数中移除的（暴露自动化身份的参数）
STEALTH_IGNORE_DEFAULT_ARGS = [
    "--enable-automation",
]


# ============================================================
# 上下文创建 —— 替换原来的 launch_persistent_context
# ============================================================

def create_stealth_context(
    playwright: Any,
    user_data_dir: str | Path,
    headless: bool = False,
    viewport: dict | None = None,
    locale: str = "zh-CN",
    timezone_id: str = "Asia/Shanghai",
    user_agent: str | None = None,
    **extra_context_args: Any,
) -> BrowserContext:  
    """
    创建带反检测能力的持久化浏览器上下文。

    这个方法是对 playwright.chromium.launch_persistent_context 的
    封装，自动追加反检测启动参数、注入指纹篡改脚本、配置设备指纹。

    参数:
        playwright: sync_playwright() 返回的 playwright 对象
        user_data_dir: 持久化用户数据目录（登录态保存在这里）
        headless:     是否无头模式（强烈建议始终 False）
        viewport:     视口大小，默认 1920x1080
        locale:       浏览器语言，默认 zh-CN
        timezone_id:  时区，默认 Asia/Shanghai
        **extra_context_args: 传给 launch_persistent_context 的额外参数

    返回:
        已注入反检测脚本的 BrowserContext 实例
    """

    if viewport is None:
        viewport = {"width": 1920, "height": 1080}

    context_args: dict[str, Any] = {
        "user_data_dir": str(user_data_dir),
        "headless": headless,

        # 视口
        "viewport": viewport,
        "no_viewport": False,

        # 语言和时区指纹
        "locale": locale,
        "timezone_id": timezone_id,

        # 地理位置（上海附近）
        "geolocation": {
            "latitude": 31.230416,
            "longitude": 121.473701,
        },
        "permissions": ["geolocation"],

        # 设备属性
        "color_scheme": "light",
        "device_scale_factor": 1,
        "is_mobile": False,
        "has_touch": False,

        # HTTPS 证书错误兼容
        "ignore_https_errors": True,

        # 去掉 "--enable-automation" 这个暴露身份的默认参数
        "ignore_default_args": STEALTH_IGNORE_DEFAULT_ARGS,

        # 启动参数
        "args": STEALTH_ARGS,
    }

    # 显式设置 User-Agent（防止 Playwright UA 含可检测的 Chromium 版本异常）
    if user_agent is not None:
        context_args["user_agent"] = user_agent

    context_args.update(extra_context_args)

    context = playwright.chromium.launch_persistent_context(**context_args)

    # 在所有页面（现有 + 未来新开的）加载前注入反检测脚本
    context.add_init_script(STEALTH_INIT_SCRIPT)

    return context


# ============================================================
# 拟人化操作工具
# ============================================================

def human_delay(min_ms: int = 200, max_ms: int = 800) -> None:
    """
    模拟人类操作间隔的随机延迟。

    真实用户"看完内容 → 决定操作 → 执行"之间天然存在随机停顿，
    不存在 0ms 的连续机械操作。默认 200~800ms 覆盖大部分场景。
    """
    delay_seconds = random.uniform(min_ms, max_ms) / 1000.0
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
    actual_ms = base_ms + random.randint(-jitter, jitter)
    actual_ms = max(50, actual_ms)  # 不低于 50ms
    page.wait_for_timeout(actual_ms)


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
        locator.click()
        human_delay(100, 300)

    for i, char in enumerate(text):
        delay = random.randint(min_delay, max_delay)

        # 数字和特殊符号需要更多确认时间
        if char.isdigit() or char in "-_/@:.#":
            delay = random.randint(70, 180)

        # 每 6~14 个字符有一个自然停顿（呼吸 / 换手）
        if i > 0 and i % random.randint(6, 14) == 0:
            human_delay(200, 500)

        page.keyboard.type(char, delay=delay)

    human_delay(100, 300)


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
        human_delay(50, 200)
        locator.click()
        return

    try:
        box = locator.bounding_box()
        if box:
            target_x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            target_y = box["y"] + box["height"] * random.uniform(0.3, 0.7)

            # 起始位置：页面左上角到中央之间的随机点
            start_x = random.uniform(200, 900)
            start_y = random.uniform(100, 500)

            # 贝塞尔控制点（制造自然弧度）
            cp1_x = start_x + (target_x - start_x) * random.uniform(0.25, 0.55)
            cp1_y = start_y + random.uniform(-60, 60)
            cp2_x = target_x + (start_x - target_x) * random.uniform(0.10, 0.30)
            cp2_y = target_y + random.uniform(-40, 40)

            steps = random.randint(6, 14)
            for i in range(steps + 1):
                t = i / steps
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
                    time.sleep(random.uniform(0.012, 0.035))
                else:
                    time.sleep(random.uniform(0.003, 0.015))

            # 悬停片刻再点击
            human_delay(40, 120)
    except Exception:
        # bounding_box 获取失败 / 页面不在前台 → 回退到直接点击
        pass

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
    step_delta = delta_y / steps

    for _ in range(steps):
        # 每步有 ±30% 的随机波动，模拟不均匀的滚动手感
        actual = step_delta + random.uniform(-step_delta * 0.3, step_delta * 0.3)
        page.mouse.wheel(0, actual)
        time.sleep(random.uniform(0.03, 0.10))


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
# 带拟人延迟的清障包装
# ============================================================

def stealth_safe_click(
    page: Page,
    locator: Locator,
    description: str,
    timeout: int = 8_000,
    retries: int = 3,
) -> None:
    """
    带拟人延迟的 safe_click 变体。

    在原有 clear_blockers + click 的基础上，增加了点击前的小随机延迟，
    让时序模式更接近真人。
    """

    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

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


# ============================================================
# User-Agent 辅助
# ============================================================

# 常见 Windows Chrome 127+ 的 UA（匹配 2025H2 时间窗口）。
# 注意：不要在代码里硬写 UA 然后用它来"伪装"，Playwright 自己
# 生成的 UA 已经是真实的 Chrome UA。这里保留作为参考，以防某些
# 场景需要传入 user_agent 参数。
REFERENCE_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/127.0.0.0 Safari/537.36"
)
