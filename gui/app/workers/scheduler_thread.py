"""C 页运行控制背后的 scheduler_core.Scheduler 适配器。

跟 login_worker.py 是同一个"moveToThread"模式：调用方（run_page.py）
new 一个 SchedulerWorker、moveToThread 到独立 QThread、连好信号、
start()。跟 LoginWorker 的区别是 Scheduler 本身跑的是一个可能持续很久
（甚至无限循环）的过程，需要一个能从主线程安全喊停的入口——见
request_stop() 的说明。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

import scheduler_core  # 主项目根目录已被 project_paths 加进 sys.path
import memory_monitor


class SchedulerWorker(QObject):
    event_received = pyqtSignal(object)  # scheduler_core.SchedulerEvent
    browser_focus_result = pyqtSignal(str, bool)
    error = pyqtSignal(str)  # 原始调度异常，供界面定位失败阶段
    stopped = pyqtSignal()  # run_once()/run_forever() 返回后触发（正常结束或被停止）

    def __init__(
        self,
        shops: list[str],
        extra: list[str],
        pool_size: int,
        max_consecutive_errors: int,
        response_timeout: float,
        python_executable: str | Path,
        run_forever: bool,
        rest_enabled: bool,
        rest_min_hours: float,
        rest_max_hours: float,
        rest_min_minutes: float,
        rest_max_minutes: float,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._shops = shops
        self._extra = extra
        self._pool_size = pool_size
        self._max_consecutive_errors = max_consecutive_errors
        self._response_timeout = response_timeout
        self._python_executable = python_executable
        self._run_forever = run_forever
        self._rest_enabled = rest_enabled
        self._rest_min_hours = rest_min_hours
        self._rest_max_hours = rest_max_hours
        self._rest_min_minutes = rest_min_minutes
        self._rest_max_minutes = rest_max_minutes

        self.scheduler: scheduler_core.Scheduler | None = None
        # request_stop() 可能在 start() 真正把 self.scheduler 赋值之前就被
        # 调用（用户点了"启动"之后几乎立刻又点"停止"，QThread 还没轮到
        # 执行 start()）——用这个标志记一下，start() 里构造完 Scheduler
        # 后先检查一遍，不会把这次提前到达的停止请求弄丢。
        self._stop_requested_before_start = False

    def start(self) -> None:
        try:
            self.scheduler = scheduler_core.Scheduler(
                shops=self._shops,
                extra=self._extra,
                pool_size=self._pool_size,
                max_consecutive_errors=self._max_consecutive_errors,
                response_timeout=self._response_timeout,
                python_executable=self._python_executable,
                on_event=self._on_event,
                rest_enabled=self._rest_enabled,
                rest_min_hours=self._rest_min_hours,
                rest_max_hours=self._rest_max_hours,
                rest_min_minutes=self._rest_min_minutes,
                rest_max_minutes=self._rest_max_minutes,
            )
            if self._stop_requested_before_start:
                self.scheduler.request_stop()

            if self._run_forever:
                self.scheduler.run_forever()
            else:
                self.scheduler.run_once()
        except Exception as exc:
            logging.getLogger(__name__).exception("负向申诉调度线程执行异常")
            self.error.emit(f"{type(exc).__name__}: {exc}")
        finally:
            self.stopped.emit()

    def _on_event(self, event: scheduler_core.SchedulerEvent) -> None:
        # 这个回调在 QThread（本 worker 所在线程）里同步调用——emit 信号
        # 会被 Qt 自动排队投递到接收者（通常是主线程的 RunPage）所在线程，
        # 调用方不需要自己处理跨线程切换。
        memory_monitor.increment_runtime_metric('scheduler_events_emitted')
        self.event_received.emit(event)

    def request_stop(self) -> None:
        """线程安全：可以直接从主线程调用，不需要走信号排队——
        scheduler_core.Scheduler.request_stop() 内部只是
        threading.Event.set()，本身就是线程安全操作，不触碰任何 Qt
        Widget 或非线程安全状态。"""
        if self.scheduler is not None:
            self.scheduler.request_stop()
        else:
            self._stop_requested_before_start = True

    def request_browser_focus(self, shop: str) -> None:
        # Scheduler.start() blocks its QThread event loop for the whole run, so a
        # queued slot would not execute until all shops finished. This is a
        # read-only process/window lookup; CPython's dict get is atomic, and the
        # scheduler may concurrently remove the shop, which simply returns False.
        try:
            success = bool(self.scheduler and self.scheduler.focus_browser(shop))
        except (OSError, ValueError):
            success = False
        self.browser_focus_result.emit(shop, success)

    def request_browser_topmost(self, shop: str, enabled: bool) -> None:
        try:
            if self.scheduler is not None:
                self.scheduler.set_browser_topmost(shop, enabled)
        except (OSError, ValueError):
            pass
