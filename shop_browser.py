"""打开已有店铺供人工查看，保留浏览器直到用户关闭窗口。"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path


def run_shop_browser(playwright, output, profile_dir: Path, shop: str) -> None:
    import browser_settings
    import scheduler_core
    from paths import START_URL
    from stealth import create_stealth_context

    def emit(kind: str, message: str = "") -> None:
        output.write(json.dumps({"type": kind, "message": message}, ensure_ascii=False) + "\n")
        output.flush()

    if not browser_settings.profile_directory(profile_dir).is_dir():
        emit("shop_browser_error", "该店铺没有当前浏览器的登录目录，请先在店铺管理中登录。")
        return

    lock_id, _conflict = scheduler_core.acquire_operation_lock([shop], "manual_browser")
    if lock_id is None:
        emit("shop_browser_error", "其它任务正在使用店铺浏览器，请结束任务后再打开店铺。")
        return

    closing = threading.Event()

    def read_commands() -> None:
        try:
            for line in sys.stdin:
                try:
                    command = json.loads(line)
                except (ValueError, TypeError):
                    continue
                if isinstance(command, dict) and command.get("cmd") == "close":
                    break
        finally:
            # GUI 退出或管道关闭时也清理浏览器，释放登录目录和运行锁。
            closing.set()

    context = None
    try:
        threading.Thread(target=read_commands, daemon=True).start()
        if closing.is_set():
            return
        context = create_stealth_context(
            playwright, user_data_dir=str(profile_dir), timeout=30_000,
        )
        context.on("close", lambda *_args: closing.set())
        if closing.is_set():
            return
        page = context.pages[0] if context.pages else context.new_page()
        message = "浏览器已打开，可手动查看订单；按 F12 或右键“检查”查看页面元素。"
        try:
            page.goto(START_URL, wait_until="domcontentloaded", timeout=30_000)
        except Exception as exc:
            # 网络超时仍保留窗口，用户可以刷新或输入其它后台地址。
            if not closing.is_set():
                message = f"浏览器已打开，但初始页面加载未完成，可手动刷新：{exc}"
        if not closing.is_set():
            emit("shop_browser_ready", message)
        while not closing.is_set():
            pages = [item for item in context.pages if not item.is_closed()]
            if not pages:
                break
            try:
                # 持续处理 Playwright 事件；关闭初始页后，新开的订单标签仍可用。
                pages[0].wait_for_timeout(250)
            except Exception:
                if closing.is_set() or pages[0].is_closed():
                    continue
                raise
    except Exception as exc:
        emit("shop_browser_error", f"打开店铺失败：{exc}")
    finally:
        try:
            if context is not None:
                context.close()
        finally:
            scheduler_core.release_operation_lock(lock_id)
            emit("shop_browser_closed", "浏览器已关闭，登录状态已保存。")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shop", required=True, help="已保存登录状态的店铺名称")
    args = parser.parse_args()

    import os
    # 必须先设置店铺再 import paths，避免意外打开默认店铺目录。
    os.environ["PDDCHAT_SHOP"] = args.shop
    from paths import PROFILE_DIR, SHOP_ID
    from playwright.sync_api import sync_playwright

    sys.stdout.reconfigure(encoding="utf-8")
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    output = sys.stdout
    sys.stdout = sys.stderr
    with sync_playwright() as playwright:
        run_shop_browser(playwright, output, PROFILE_DIR, SHOP_ID)


if __name__ == "__main__":
    main()
