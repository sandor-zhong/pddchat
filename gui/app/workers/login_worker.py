"""B 页"新增店铺/重新登录"背后的登录子进程适配器。

调用方（add_shop_dialog.py）负责把这个对象 moveToThread 到一个独立
QThread 上再调用 start()——本对象自己不建线程，只是把"跟 --login-only
子进程通信"这件事包成三个好懂的信号：waiting / succeeded / failed，
调用方连好信号就行，不需要理解底层 JSON Lines 协议。
"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

from ..services.app_logging import logger
from .base_process_worker import ProcessLineIO


def _truncate_for_label(text: str, max_lines: int = 6, max_chars: int = 500) -> str:
    """把可能几 KB 长的完整 stderr（比如一份 traceback）压成适合直接
    放进 GUI 一个 QLabel 的摘要。

    取尾部而不是头部：Python traceback 最后几行（真正的异常类型+消息）
    通常信息量最高，中间的调用栈对运营用户没有意义。完整原文不会因为
    这里截断而丢失——调用方在截断之前已经把完整文本写进 gui.log，需要
    完整堆栈的开发者可以去那边看。
    """
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    tail = lines[-max_lines:]
    snippet = "\n".join(tail)
    truncated = len(lines) > max_lines
    if len(snippet) > max_chars:
        snippet = snippet[-max_chars:]
        truncated = True
    prefix = "……（已截断，完整内容见 gui.log）\n" if truncated else ""
    return prefix + snippet


class LoginWorker(QObject):
    """跑 `<pddchat_python> main.py --shop <shop> --login-only`，
    等待登录检测结果。"""

    waiting = pyqtSignal()  # 子进程已启动，浏览器窗口应该已经弹出
    succeeded = pyqtSignal()
    failed = pyqtSignal(str)
    finished = pyqtSignal()  # 子进程已完全退出，调用方此时才能安全清理线程

    def __init__(
        self,
        pddchat_python: str | Path,
        main_py: str | Path,
        pddchat_root: str | Path,
        shop: str,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = str(pddchat_python)
        self._main_py = str(main_py)
        self._root = str(pddchat_root)
        self._shop = shop
        self._io: ProcessLineIO | None = None
        self._got_result = False

    def start(self) -> None:
        cmd = [self._python, self._main_py, "--shop", self._shop, "--login-only"]
        self._io = ProcessLineIO(cmd, cwd=self._root, parent=self)
        self._io.line_received.connect(self._on_line)
        self._io.process_exited.connect(self._on_exit)
        self._io.start()
        self.waiting.emit()

    def _on_line(self, msg: dict) -> None:
        if msg.get("type") != "login_result":
            return
        self._got_result = True
        if msg.get("success"):
            self.succeeded.emit()
        else:
            self.failed.emit(str(msg.get("message") or "登录未成功（原因未知）"))
        # 收到结果后主动让子进程关闭——不能立刻强杀，要走 close 指令让它
        # 干净关闭 Playwright context（cookie/localStorage 落盘依赖这个）。
        if self._io is not None:
            self._io.send({"cmd": "close"})

    def _on_exit(self, exit_code: int) -> None:
        if not self._got_result:
            message = f"登录子进程异常退出（退出码 {exit_code}），未收到登录结果。"
            stderr_text = self._io.stderr_text().strip() if self._io is not None else ""
            if stderr_text:
                # 完整内容写日志文件（不截断），GUI 标签上只放尾部摘要——
                # 运营人员遇到问题时至少能看到一行有意义的提示，需要
                # 完整排查信息时开发者可以直接要 gui/state/logs/gui.log。
                logger.error(
                    "login-only 子进程异常退出（退出码 %d），完整 stderr：\n%s",
                    exit_code, stderr_text,
                )
                message += "\n" + _truncate_for_label(stderr_text)
            self.failed.emit(message)
        self.finished.emit()

    def cancel(self) -> None:
        """人工点"取消"：发 close 让子进程自己关浏览器退出，不强杀——
        跟收到结果后的收尾走同一条路径，_on_exit 会在还没等到结果时
        补一条 failed，调用方不需要特殊处理"取消"和"失败"两种情况。"""
        if self._io is not None:
            self._io.send({"cmd": "close"})
            self._io.stop(timeout=5.0)
