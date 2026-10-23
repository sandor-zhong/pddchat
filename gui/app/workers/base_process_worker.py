"""通用的"起子进程 + 后台线程读 JSON Lines 协议 + emit Qt 信号"骨架。

跟 scheduler_core.ShopWorker 是同一个通信模式（发指令写 stdin、后台线程
读 stdout 塞队列/直接 emit、短轮询等响应）——但这里刻意**不 import
scheduler_core**，也不感知任何具体协议语义（是登录检测还是逐单调度）。
这一层只负责"跟一个子进程用 JSON Lines 双向通信"这件事本身，
login_worker.py（B 页新增店铺）、scheduler_thread.py（C 页运行控制）
各自在这一层之上定义自己的协议解读。
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

import memory_monitor

# 打包后 GUI 用 pythonw.exe（无控制台）启动，这时候起一个控制台版
# python.exe 子进程如果不显式抑制，Windows 会给它新分配一个控制台窗口、
# 一闪而过——开发时用 `uv run python -m app.main`（本身就带控制台）
# 看不出这个问题，只有打包后才会暴露。CREATE_NO_WINDOW 只在 Windows 上
# 存在，且这个项目本来就是 Windows-only。
_POPEN_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 子进程 stderr 攒到内存里、供异常退出时诊断用的行数上限——正常情况下
# 这里应该完全是空的（main.py 那边的协议消息全走 stdout，不写 stderr），
# 只有子进程本身崩溃/打印 traceback 才会有内容。给个上限纯粹是防御性的，
# 避免极端情况下无限增长 GUI 进程内存。
_STDERR_MAX_LINES = 500


class ProcessLineIO(QObject):
    """管理一个子进程的启动/通信/关闭。

    line_received 信号在【子进程 stdout 读取线程】上发射，不是构造这个
    对象所在的线程——依赖 Qt 的跨线程信号槽自动排队机制，槽函数会在
    "接收者对象所在的线程"（也就是这个 ProcessLineIO 对象被 moveToThread
    到的线程，默认是创建它的线程）执行，调用方不需要自己处理这层线程
    切换，只要保证是在目标线程里构造这个对象即可。

    stderr 不是这一层的协议通道（协议全在 stdout 上），只是持续排空、
    攒起来供子进程异常退出时诊断用——见 stderr_text()。之前这里没有
    设 stderr=，子进程（GUI 本身是 pythonw.exe 无控制台启动）的 stderr
    继承下来的句柄基本没法用，子进程崩溃时打印的 traceback 会直接
    丢失，只能在 GUI 侧看到一个语焉不详的退出码——加上这个之后至少能把
    完整错误文本带回来。
    """

    line_received = pyqtSignal(dict)
    process_exited = pyqtSignal(int)

    def __init__(
        self,
        cmd: list[str],
        cwd: str | Path,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._cmd = [str(c) for c in cmd]
        if '--serve' in self._cmd and len(self._cmd) > 1 and Path(self._cmd[1]).name == 'main.py':
            self._cmd[1] = str(Path(self._cmd[1]).with_name('memory_worker.py'))
        self._cwd = str(cwd)
        self._proc: subprocess.Popen | None = None
        self._reader_thread: threading.Thread | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=_STDERR_MAX_LINES)
        self._stderr_lock = threading.Lock()

    def start(self) -> None:
        self._proc = subprocess.Popen(
            self._cmd,
            cwd=self._cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",  # 必须跟主项目 main.py 那边显式钉的 UTF-8 一致，
            # 否则 Windows 默认 locale（cp936）会让中文协议内容悄悄错位/
            # 乱码，json.loads 可能"成功"解析出错误内容却不报错。
            errors="replace",  # 只影响解码失败时的兜底行为（用 U+FFFD 占位
            # 而不是抛 UnicodeDecodeError）——stdout 是我们自己的协议通道，
            # 理论上不会触发；stderr 是子进程崩溃时的诊断通道，内容不受
            # 我们控制，一次原生层错误/运行时启动阶段本身出问题都可能
            # 不是干净的 UTF-8，解码失败不能让读线程直接死掉、反而丢了
            # 本来要抓的诊断信息。
            bufsize=1,
            creationflags=_POPEN_CREATIONFLAGS,
        )
        self._reader_thread = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_thread.start()

    def _read_stdout(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get('type') == 'memory_status':
                    from ..services.app_logging import logger
                    logger.info('[内存回收] %s', msg.get('message',''))
                    continue
                self.line_received.emit(msg)
        finally:
            exit_code = self._proc.wait() if self._proc is not None else -1
            self.process_exited.emit(exit_code)

    def _read_stderr(self) -> None:
        """后台线程：持续排空子进程 stderr，只攒起来，不 emit 任何信号。

        必须要有这个线程在读——加了 stderr=subprocess.PIPE 之后，如果
        没人读这个管道，子进程写多了会被 Windows 管道缓冲区（通常只有
        几十 KB）反压卡住，表现成子进程"卡死不退出"，比不加 stderr=PIPE
        之前还糟（之前至少能正常退出，只是错误信息丢失）。
        """
        assert self._proc is not None and self._proc.stderr is not None
        try:
            for line in self._proc.stderr:
                with self._stderr_lock:
                    self._stderr_lines.append(line)
        except Exception:
            pass

    def stderr_text(self) -> str:
        """子进程异常退出、且没有通过协议给出任何结果时，调用方可以用
        这个拿到目前为止攒到的 stderr 诊断文本（正常退出/正常协议交互
        的情况下就是空字符串）。"""
        with self._stderr_lock:
            return "".join(self._stderr_lines)

    def send(self, obj: dict) -> bool:
        if self._proc is None or self._proc.stdin is None or self._proc.stdin.closed:
            return False
        try:
            self._proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            return False

    def focus_browser(self) -> bool:
        """Locate the current browser descendant; broker PID stays stable across recycling."""
        return bool(
            self._proc is not None
            and self._proc.poll() is None
            and memory_monitor.focus_browser_window(self._proc.pid)
        )

    def set_browser_topmost(self, enabled: bool) -> bool:
        return bool(
            self._proc is not None
            and self._proc.poll() is None
            and memory_monitor.set_browser_topmost(self._proc.pid, enabled)
        )

    def stop(self, timeout: float = 10.0) -> None:
        """等子进程退出，超时强杀兜底。不主动发任何指令——是否要先发
        个"请关闭"指令由调用方（更清楚协议语义）决定，这里只负责收尾。"""
        if self._proc is None:
            return
        try:
            self._proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
