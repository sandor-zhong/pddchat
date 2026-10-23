"""通用的"定时轮询数据库、结果通过信号送回主线程"骨架。

不在主线程同步查数据库——本地 SQLite 单次查询通常很快，但调度器的多个
店铺子进程可能正在（WAL 模式下）并发写同一个数据库文件，偶尔的锁等待
不该卡住界面。用 QThreadPool + 一次性 QRunnable：定时器每次触发丢一个
任务进线程池，跑完把结果通过信号送回主线程，不需要常驻线程、也不需要
自己管理线程生命周期。
"""

from __future__ import annotations

from typing import Callable

from PyQt5.QtCore import QObject, QRunnable, QThreadPool, QTimer, pyqtSignal


class _PollSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)


class _PollTask(QRunnable):
    """在线程池的工作线程里跑一次 fetch()，绝不能碰任何 Qt Widget——
    fetch 只应该做纯数据查询，返回值必须是能安全跨线程传递的普通对象
    （dict/list/dataclass 之类），不能是 Widget 引用。"""

    def __init__(self, fetch: Callable[[], object], signals: _PollSignals) -> None:
        super().__init__()
        self._fetch = fetch
        self._signals = signals

    def run(self) -> None:
        try:
            data = self._fetch()
        except Exception as exc:
            # 查询失败也要通过信号上报，不能让工作线程带着未捕获异常退出
            # （那样 _busy 标志永远不会被清掉，poller 会卡死不再触发）。
            self._signals.error.emit(str(exc))
            return
        self._signals.result.emit(data)


class DbPoller(QObject):
    """定时在线程池里跑 fetch()，通过 data_ready 信号把结果送回主线程。

    start()/stop() 控制定时器；上一次任务还没跑完时不会重复丢新任务
    （用 _busy 标志防抖），避免查询偶尔变慢时任务在线程池里堆积。
    """

    data_ready = pyqtSignal(object)
    fetch_error = pyqtSignal(str)

    def __init__(
        self,
        fetch: Callable[[], object],
        interval_ms: int = 4000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._fetch = fetch
        self._pool = QThreadPool.globalInstance()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)
        self._busy = False

    def start(self, immediate: bool = True) -> None:
        if immediate:
            self._tick()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def _tick(self) -> None:
        if self._busy:
            return
        self._busy = True
        signals = _PollSignals()
        signals.result.connect(self._on_result)
        signals.error.connect(self._on_error)
        self._pool.start(_PollTask(self._fetch, signals))

    def _on_result(self, data: object) -> None:
        self._busy = False
        self.data_ready.emit(data)

    def _on_error(self, message: str) -> None:
        self._busy = False
        self.fetch_error.emit(message)
