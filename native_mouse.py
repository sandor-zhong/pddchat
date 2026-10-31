"""Route synchronous Playwright pointer actions through the Windows cursor.

Only contexts explicitly enabled with install_system_mouse are affected. DOM
inspection stays in Playwright; no browser mouse input is used as a fallback.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from functools import wraps

from playwright.sync_api import Locator, Mouse, Page, expect

_installed = False
_pages = {}
_driver = None


class WindowsMouse:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("系统鼠标模式需要 Windows 桌面。")
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        # Use physical coordinates on mixed-DPI desktops, including worker threads.
        self.user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateMutexW.argtypes = [
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        ]
        self.kernel.CreateMutexW.restype = wintypes.HANDLE
        self.kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        self.kernel.GetCurrentThreadId.restype = wintypes.DWORD
        self.user.GetForegroundWindow.restype = wintypes.HWND
        self.user.IsWindowVisible.argtypes = [wintypes.HWND]
        self.user.GetWindowTextW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self.user.GetClientRect.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.RECT),
        ]
        self.user.ClientToScreen.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.POINT),
        ]
        self.user.SetForegroundWindow.argtypes = [wintypes.HWND]
        self.user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.AttachThreadInput.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.BOOL,
        ]
        self.user.BringWindowToTop.argtypes = [wintypes.HWND]
        self.user.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user.IsZoomed.argtypes = [wintypes.HWND]
        self.user.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
        self.user.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        self.mutex = self.kernel.CreateMutexW(None, False, "Local\\PddchatSystemMouse")
        if not self.mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        self.drag_page = None
        self.drag_owner = None
        self.buttons = set()
        self.windows = {}
        self.positions = {}

    def foreground(self, hwnd):
        # SW_MAXIMIZE: restoring (SW_RESTORE) would shrink the window on every
        # pointer action, even when Chromium was launched with --start-maximized.
        if not self.user.IsZoomed(hwnd):
            self.user.ShowWindow(hwnd, 3)
        active = self.user.GetForegroundWindow()
        current_thread = self.kernel.GetCurrentThreadId()
        active_thread = (
            self.user.GetWindowThreadProcessId(active, None) if active else 0
        )
        attached = (
            active_thread
            and active_thread != current_thread
            and self.user.AttachThreadInput(current_thread, active_thread, True)
        )
        try:
            self.user.BringWindowToTop(hwnd)
            self.user.SetForegroundWindow(hwnd)
        finally:
            if attached:
                self.user.AttachThreadInput(current_thread, active_thread, False)
        for _ in range(10):
            if self.user.GetForegroundWindow() == hwnd:
                return
            time.sleep(0.02)
        raise RuntimeError("浏览器无法切换到前台，已停止系统鼠标操作。")

    @contextmanager
    def locked(self):
        result = self.kernel.WaitForSingleObject(self.mutex, 30000)
        if result not in (0, 0x80):
            raise RuntimeError("等待系统鼠标超时，请结束其它正在使用鼠标的任务。")
        try:
            yield
        finally:
            self.kernel.ReleaseMutex(self.mutex)

    def window(self, page):
        page.bring_to_front()
        cached = self.windows.get(page._impl_obj)
        if cached and self.user.IsWindowVisible(cached):
            self.foreground(cached)
            return cached
        token = "pdd-mouse-" + uuid.uuid4().hex
        title = page.title()
        found = []
        callback_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
        )

        @callback_type
        def visit(hwnd, _):
            value = ctypes.create_unicode_buffer(4096)
            self.user.GetWindowTextW(hwnd, value, len(value))
            if self.user.IsWindowVisible(hwnd) and token in value.value:
                found.append(hwnd)
            return True

        self.user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        try:
            page.evaluate("title => { document.title = title; }", token)
            for _ in range(20):
                self.user.EnumWindows(visit, 0)
                if found:
                    break
                page.wait_for_timeout(25)
        finally:
            page.evaluate("title => { document.title = title; }", title)
        if len(set(found)) != 1:
            raise RuntimeError("无法唯一识别浏览器窗口，已停止系统鼠标操作。")
        hwnd = found[0]
        self.foreground(hwnd)
        self.windows[page._impl_obj] = hwnd
        return hwnd

    def point(self, page, x, y):
        self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if page.viewport_size is not None:
            raise RuntimeError(
                "系统鼠标模式需要 no_viewport=True，以便坐标跟随真实窗口。"
            )
        hwnd = self.window(page)
        metrics = page.evaluate(
            "() => ({w: innerWidth, h: innerHeight, dpr: devicePixelRatio})"
        )
        if not 0 <= x < metrics["w"] or not 0 <= y < metrics["h"]:
            raise RuntimeError("鼠标目标位于浏览器可视区域之外。")
        rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
        if not self.user.GetClientRect(
            hwnd, ctypes.byref(rect)
        ) or not self.user.ClientToScreen(hwnd, ctypes.byref(origin)):
            raise ctypes.WinError(ctypes.get_last_error())
        # Chromium/Firefox client areas include their toolbar above the viewport.
        scale = metrics["dpr"]
        return round(origin.x + x * scale), round(
            origin.y + rect.bottom - metrics["h"] * scale + y * scale
        )

    def move(self, page, x, y, *, steps=1):
        with self.locked():
            if self.drag_page is not None and self.drag_page is not page._impl_obj:
                raise RuntimeError("拖拽过程中不能切换页面。")
            end_x, end_y = self.point(page, x, y)
            start = wintypes.POINT()
            self.user.GetCursorPos(ctypes.byref(start))
            for i in range(1, max(1, steps) + 1):
                ratio = i / max(1, steps)
                if not self.user.SetCursorPos(
                    round(start.x + (end_x - start.x) * ratio),
                    round(start.y + (end_y - start.y) * ratio),
                ):
                    raise ctypes.WinError(ctypes.get_last_error())
                if steps > 1:
                    time.sleep(0.008)
            self.positions[page._impl_obj] = (x, y)

    def down(self, page, *, button="left", click_count=1):
        flags = {"left": 0x2, "right": 0x8, "middle": 0x20}
        flag = flags[button]
        if self.drag_page is None:
            result = self.kernel.WaitForSingleObject(self.mutex, 30000)
            if result not in (0, 0x80):
                raise RuntimeError("等待系统鼠标超时。")
            self.drag_page = page._impl_obj
            self.drag_owner = threading.get_ident()
        try:
            self.window(page)
            position = self.positions.get(page._impl_obj)
            if position is not None:
                self.move(page, *position)
            self.user.mouse_event(flag, 0, 0, 0, 0)
            self.buttons.add(button)
        except BaseException:
            self.release()
            raise

    def release(self):
        if self.drag_owner != threading.get_ident():
            return
        for button in self.buttons:
            self.user.mouse_event(
                {"left": 0x4, "right": 0x10, "middle": 0x40}[button], 0, 0, 0, 0
            )
        self.buttons.clear()
        if self.drag_page is not None:
            self.drag_page = None
            self.drag_owner = None
            self.kernel.ReleaseMutex(self.mutex)

    def up(self, page, *, button="left", click_count=1):
        self.release()

    def click(self, page, x, y, *, button="left", click_count=1, delay=0):
        with self.locked():
            self.move(page, x, y, steps=8)
            for i in range(click_count):
                try:
                    self.down(page, button=button)
                    time.sleep((delay or 50) / 1000)
                finally:
                    self.release()
                if i + 1 < click_count:
                    time.sleep(0.06)

    def dblclick(self, page, x, y, **kwargs):
        self.click(page, x, y, click_count=2, **kwargs)

    def wheel(self, page, delta_x, delta_y):
        with self.locked():
            self.window(page)
            size = page.evaluate("() => ({w: innerWidth, h: innerHeight})")
            left, top = self.point(page, 0, 0)
            right, bottom = self.point(page, size["w"] - 1, size["h"] - 1)
            cursor = wintypes.POINT()
            self.user.GetCursorPos(ctypes.byref(cursor))
            if not (left <= cursor.x <= right and top <= cursor.y <= bottom):
                self.move(page, size["w"] / 2, size["h"] / 2)
            if delta_y:
                self.user.mouse_event(0x800, 0, 0, int(-delta_y) & 0xFFFFFFFF, 0)
            if delta_x:
                self.user.mouse_event(0x1000, 0, 0, int(delta_x) & 0xFFFFFFFF, 0)


def _enabled(page):
    return getattr(page.context, "_system_mouse_enabled", False) is True


def _locator_action(locator, action, **options):
    page = locator.page
    with _driver.locked():
        # Maximizing changes layout; locate the target only after the window has
        # reached its required state, so the first click uses the new coordinates.
        _driver.window(page)
        timeout = options.pop("timeout", None)
        locator.wait_for(state="visible", timeout=timeout)
        if action != "hover":
            expect(locator).to_be_enabled(timeout=timeout)
        locator.scroll_into_view_if_needed(timeout=timeout)
        box = locator.bounding_box(timeout=timeout)
        if not box:
            raise RuntimeError("无法获取系统鼠标目标的位置。")
        position = options.pop("position", None) or {
            "x": box["width"] / 2,
            "y": box["height"] / 2,
        }
        x, y = box["x"] + position["x"], box["y"] + position["y"]
        # Check the actual hit target, including overlays, before moving the OS cursor.
        if not options.pop("force", False) and not locator.evaluate(
            "(el, p) => { const r=el.getBoundingClientRect(); const hit=el.ownerDocument.elementFromPoint(r.x+p.x,r.y+p.y); return hit===el || el.contains(hit); }",
            position,
        ):
            raise RuntimeError("目标被其它元素遮挡，已停止系统鼠标点击。")
        if options.pop("trial", False):
            return
        modifiers = options.pop("modifiers", None) or []
        if modifiers:
            raise RuntimeError("系统鼠标暂不支持组合键点击。")
        options.pop("no_wait_after", None)
        if action == "hover":
            _driver.move(page, x, y, steps=8)
        else:
            getattr(_driver, action)(page, x, y, **options)


def install_system_mouse(context):
    """Enable native pointer input for current and future pages of a context."""
    global _driver, _installed
    if _driver is None:
        _driver = WindowsMouse()
    if not _installed:
        for name in ("move", "down", "up", "click", "dblclick", "wheel"):
            original = getattr(Mouse, name)

            def make_mouse(action, fallback):
                @wraps(fallback)
                def wrapped(mouse, *args, **kwargs):
                    page = _pages.get(mouse._impl_obj)
                    if page is None:
                        return fallback(mouse, *args, **kwargs)
                    try:
                        return getattr(_driver, action)(page, *args, **kwargs)
                    except BaseException:
                        _driver.release()
                        raise

                return wrapped

            setattr(Mouse, name, make_mouse(name, original))
        for name in ("click", "dblclick", "hover"):
            original = getattr(Locator, name)

            def make_locator(action, fallback):
                @wraps(fallback)
                def wrapped(locator, **kwargs):
                    if not _enabled(locator.page):
                        return fallback(locator, **kwargs)
                    return _locator_action(locator, action, **kwargs)

                return wrapped

            setattr(Locator, name, make_locator(name, original))
            original_page = getattr(Page, name)

            def make_page(action, fallback):
                @wraps(fallback)
                def wrapped(page, selector, **kwargs):
                    if not _enabled(page):
                        return fallback(page, selector, **kwargs)
                    kwargs.pop("strict", None)
                    return _locator_action(page.locator(selector), action, **kwargs)

                return wrapped

            setattr(Page, name, make_page(name, original_page))
        _installed = True
    context._system_mouse_enabled = True

    def attach(page):
        _pages[page.mouse._impl_obj] = page

        def detach(*_):
            _pages.pop(page.mouse._impl_obj, None)
            _driver.windows.pop(page._impl_obj, None)
            _driver.positions.pop(page._impl_obj, None)
            if _driver.drag_page is page._impl_obj:
                _driver.release()

        page.on("close", detach)
        page.on("crash", detach)

    for page in context.pages:
        attach(page)
        if isinstance(page, Page):
            with _driver.locked():
                _driver.window(page)
    context.on("page", attach)
