"""发起复议的多店铺浏览器池：浏览器常驻，订单严格串行轮转。"""

from __future__ import annotations

import random
from dataclasses import dataclass
from pathlib import Path

from PyQt5.QtCore import QObject, QTimer, pyqtSignal, pyqtSlot

from ..services.app_logging import logger
from .base_process_worker import ProcessLineIO


@dataclass
class _ShopRun:
    io: ProcessLineIO
    phase: str = "initializing"  # initializing / idle / busy / stopping / closing
    remaining: int = 0
    processed: int = 0
    submitted: int = 0
    pending_confirmation: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    finish_emitted: bool = False


class ReconsiderationWorker(QObject):
    """最多保持 ``pool_size`` 家浏览器打开，但一次只驱动一家处理一单。"""

    _cancel_requested = pyqtSignal()
    _focus_requested = pyqtSignal(str)
    _topmost_requested = pyqtSignal(str, bool)
    browser_focus_result = pyqtSignal(str, bool)
    shop_activity = pyqtSignal(str, str, str)
    shop_started = pyqtSignal(str)
    shop_progress = pyqtSignal(str, str, int, int)
    shop_order_result = pyqtSignal(str, str, str, int)
    shop_finished = pyqtSignal(str, bool, int, int, int, int, str)
    all_finished = pyqtSignal()

    def __init__(
        self,
        pddchat_python: str | Path,
        main_py: str | Path,
        pddchat_root: str | Path,
        shops: list[str],
        max_orders: int = 20,
        pool_size: int = 4,
        max_consecutive_failures: int = 3,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = str(pddchat_python)
        self._main_py = str(main_py)
        self._root = str(pddchat_root)
        self._pending_shops = list(shops)
        self._max_orders = max(1, max_orders)
        self._pool_size = max(1, pool_size)
        self._max_consecutive_failures = max(1, max_consecutive_failures)

        self._states: dict[str, _ShopRun] = {}
        self._shop_by_io: dict[ProcessLineIO, str] = {}
        self._initializing_shop: str | None = None
        self._busy_shop: str | None = None
        self._last_shop: str | None = None
        self._cancelled = False
        self._finished = False
        self._cancel_requested.connect(self._cancel_in_worker_thread)
        self._focus_requested.connect(self._focus_in_worker_thread)
        self._topmost_requested.connect(self._topmost_in_worker_thread)

    def start(self) -> None:
        if self._cancelled:
            self._emit_all_finished_once()
            return
        self._fill_pool_or_dispatch()

    def _start_shop(self, shop: str) -> None:
        command = [
            self._python,
            self._main_py,
            "--shop",
            shop,
            "--reconsider",
            "--serve",
            "--confirm",
            "--max-orders",
            str(self._max_orders),
        ]
        io = ProcessLineIO(command, cwd=self._root, parent=self)
        state = _ShopRun(io=io)
        self._states[shop] = state
        self._shop_by_io[io] = shop
        self._initializing_shop = shop
        io.line_received.connect(self._on_line)
        io.process_exited.connect(self._on_exit)
        try:
            io.start()
        except Exception as exc:
            self._initializing_shop = None
            self._states.pop(shop, None)
            self._shop_by_io.pop(io, None)
            self.shop_finished.emit(shop, False, 0, 0, 0, 0, str(exc))
            QTimer.singleShot(0, self._fill_pool_or_dispatch)
            return
        self.shop_started.emit(shop)
        if not io.send({"cmd": "init"}):
            self._finish_shop(shop, False, "发送复议 init 指令失败。")

    def _fill_pool_or_dispatch(self) -> None:
        if self._finished:
            return
        if self._cancelled:
            if not self._states:
                self._emit_all_finished_once()
            return
        # 初始化（打开浏览器、登录、扫描）和订单处理都属于页面操作；任何
        # 时刻只能有一个在进行，不能为了填满池子与正在提交的店铺并发。
        if self._initializing_shop is not None or self._busy_shop is not None:
            return

        if len(self._states) < self._pool_size and self._pending_shops:
            self._start_shop(self._pending_shops.pop(0))
            return

        idle = [
            shop
            for shop, state in self._states.items()
            if state.phase == "idle" and state.remaining > 0
        ]
        if idle:
            self._dispatch_next()
            return

        if not self._states and not self._pending_shops:
            self._emit_all_finished_once()

    def _dispatch_next(self) -> None:
        if (
            self._cancelled
            or self._busy_shop is not None
            or self._initializing_shop is not None
        ):
            return
        candidates = [
            shop
            for shop, state in self._states.items()
            if state.phase == "idle" and state.remaining > 0
        ]
        if not candidates:
            self._fill_pool_or_dispatch()
            return
        alternatives = [shop for shop in candidates if shop != self._last_shop]
        shop = random.choice(alternatives or candidates)
        state = self._states[shop]
        if not state.io.send({"cmd": "next"}):
            self._finish_shop(shop, False, "发送下一单指令失败。")
            return
        state.phase = "busy"
        self._busy_shop = shop
        self._last_shop = shop

    @pyqtSlot(dict)
    def _on_line(self, message: dict) -> None:
        io = self.sender()
        shop = self._shop_by_io.get(io)
        if shop is None or shop not in self._states:
            return
        state = self._states[shop]
        message_type = message.get("type")

        if message_type == "activity":
            self.shop_activity.emit(
                shop,
                str(message.get("stage") or "正在处理"),
                str(message.get("order_no") or ""),
            )
            return

        if message_type == "reconsider_progress":
            self.shop_progress.emit(
                shop,
                str(message.get("stage") or ""),
                int(message.get("current", 0)),
                int(message.get("total", 0)),
            )
            return

        if message_type == "reconsider_ready":
            if self._initializing_shop == shop:
                self._initializing_shop = None
            state.remaining = max(0, int(message.get("queue_size", 0)))
            state.phase = "idle"
            if state.remaining == 0:
                self._finish_shop(shop, True, "没有待复议订单。")
            else:
                self.shop_progress.emit(shop, "等待轮转", 0, state.remaining)
                self._fill_pool_or_dispatch()
            return

        if message_type == "reconsider_order_result":
            if self._busy_shop == shop:
                self._busy_shop = None
            state.phase = "idle"
            state.processed += 1
            state.remaining = max(0, int(message.get("remaining", 0)))
            order_no = str(message.get("order_no") or "")
            result = str(message.get("result") or "")
            if result == "已提交复议":
                state.submitted += 1
                state.consecutive_failures = 0
            elif result == "复议结果待确认":
                state.pending_confirmation += 1
                state.consecutive_failures = 0
            elif result.startswith("失败："):
                state.failed += 1
                state.consecutive_failures += 1
            else:
                state.consecutive_failures = 0
            self.shop_order_result.emit(shop, order_no, result, state.remaining)

            if self._cancelled or bool(message.get("stopped")):
                self._finish_shop(shop, True, "已按要求停止。")
            elif state.consecutive_failures >= self._max_consecutive_failures:
                self._finish_shop(
                    shop,
                    False,
                    f"连续 {state.consecutive_failures} 条复议处理失败，已关闭该店铺。",
                )
            elif state.remaining == 0:
                self._finish_shop(shop, True, "")
            else:
                # 店铺之间随机停顿，并在下一次选择时优先排除刚处理过的店铺。
                self.shop_progress.emit(shop, "切换等待", 0, state.remaining)
                QTimer.singleShot(
                    random.randint(1000, 3000),
                    self._fill_pool_or_dispatch,
                )
            return

        if message_type == "reconsider_queue_empty":
            if self._busy_shop == shop:
                self._busy_shop = None
            state.remaining = 0
            self._finish_shop(shop, True, "")
            return

        if message_type == "reconsider_stopped":
            if self._busy_shop == shop:
                self._busy_shop = None
            if self._initializing_shop == shop:
                self._initializing_shop = None
            self._finish_shop(shop, self._cancelled, "已按要求停止。")
            return

        if message_type == "reconsider_error":
            if self._busy_shop == shop:
                self._busy_shop = None
            if self._initializing_shop == shop:
                self._initializing_shop = None
            self._finish_shop(
                shop,
                False,
                str(message.get("message") or "复议子进程返回未知错误。"),
            )

    def _finish_shop(self, shop: str, success: bool, message: str) -> None:
        state = self._states.get(shop)
        if state is None:
            return
        if self._busy_shop == shop:
            self._busy_shop = None
        if self._initializing_shop == shop:
            self._initializing_shop = None
        state.phase = "closing"
        if not state.finish_emitted:
            state.finish_emitted = True
            self.shop_finished.emit(
                shop,
                success,
                state.processed,
                state.submitted,
                state.pending_confirmation,
                state.failed,
                message,
            )
        if not state.io.send({"cmd": "stop"}):
            # 子进程可能已经在协议错误后自行退出；真正移出池子统一等 exit。
            logger.debug("[%s] 复议子进程已无法接收 stop，等待退出信号。", shop)

    @pyqtSlot(int)
    def _on_exit(self, exit_code: int) -> None:
        io = self.sender()
        shop = self._shop_by_io.pop(io, None)
        if shop is None:
            return
        state = self._states.pop(shop, None)
        if state is None:
            return
        if self._busy_shop == shop:
            self._busy_shop = None
        if self._initializing_shop == shop:
            self._initializing_shop = None

        if not state.finish_emitted:
            if state.phase == "stopping" and self._cancelled:
                self.shop_finished.emit(
                    shop,
                    True,
                    state.processed,
                    state.submitted,
                    state.pending_confirmation,
                    state.failed,
                    "已按要求停止。",
                )
            else:
                stderr = state.io.stderr_text().strip()
                message = f"复议子进程异常退出（退出码 {exit_code}），未正常完成。"
                if stderr:
                    logger.error("reconsider 子进程异常退出：\n%s", stderr)
                    message += "\n" + stderr[-500:]
                self.shop_finished.emit(
                    shop,
                    False,
                    state.processed,
                    state.submitted,
                    state.pending_confirmation,
                    state.failed,
                    message,
                )
        QTimer.singleShot(0, self._fill_pool_or_dispatch)

    def cancel(self) -> None:
        """线程安全地请求停止；具体状态变更回到 worker 所在线程执行。"""
        self._cancel_requested.emit()

    def request_browser_focus(self, shop: str) -> None:
        self._focus_requested.emit(shop)

    def request_browser_topmost(self, shop: str, enabled: bool) -> None:
        self._topmost_requested.emit(shop, enabled)

    @pyqtSlot(str)
    def _focus_in_worker_thread(self, shop: str) -> None:
        state = self._states.get(shop)
        self.browser_focus_result.emit(shop, bool(state and state.io.focus_browser()))

    @pyqtSlot(str, bool)
    def _topmost_in_worker_thread(self, shop: str, enabled: bool) -> None:
        state = self._states.get(shop)
        if state:
            state.io.set_browser_topmost(enabled)

    @pyqtSlot()
    def _cancel_in_worker_thread(self) -> None:
        """停止排队并通知全部常驻店铺；正在提交者仍走子进程安全边界。"""
        if self._cancelled:
            return
        self._cancelled = True
        self._pending_shops.clear()
        if not self._states:
            self._emit_all_finished_once()
            return
        for shop, state in list(self._states.items()):
            if state.phase not in {"closing", "stopping"}:
                state.phase = "stopping"
            if not state.io.send({"cmd": "stop"}):
                logger.warning("[%s] 复议停止指令发送失败，子进程可能已经退出。", shop)

    def _emit_all_finished_once(self) -> None:
        if self._finished:
            return
        self._finished = True
        self.all_finished.emit()
