"""C 页"查询申诉结果"按钮背后的适配器——依次为每家勾选的店铺起一个
一次性子进程：`main.py --shop <shop> --check-appeal-results --since-days N`
（跟 login_worker.py 同款 ProcessLineIO 骨架）。

不用 scheduler_core.Scheduler（run_page.py 主流程"启动"按钮用的那一套）：
那套 init/next/stop 协议是为"同一批店铺并发跑、每个店铺内部逐单交织"
设计的，这个功能里每家店铺只是"开一次浏览器、翻几页、关掉"的一次性
动作，没有"下一单"这个概念，套 Scheduler 的协议纯属过度设计。

店铺之间严格顺序执行（同一时刻只有一个子进程在跑）——不仅是因为不需要
并发，也刻意避免这个 worker 自己在多个店铺间产生并发冲突（同一家店铺的
Chrome 用户目录同一时刻只能被一个 Playwright 进程打开）。
"""

from __future__ import annotations

from pathlib import Path

from PyQt5.QtCore import QObject, pyqtSignal

from ..services.app_logging import logger
from .base_process_worker import ProcessLineIO
from result_query_settings import normalize_since


class AppealResultCheckWorker(QObject):
    shop_started = pyqtSignal(str)                  # shop
    shop_progress = pyqtSignal(str, int, int, int)   # shop, page, checked, matched
    shop_skipped = pyqtSignal(str, str)             # shop, reason
    shop_finished = pyqtSignal(str, bool, int, str)  # shop, success, updated_count, message
    all_finished = pyqtSignal()                      # 全部店铺处理完毕（不管每家成功与否）

    def __init__(
        self,
        pddchat_python: str | Path,
        main_py: str | Path,
        pddchat_root: str | Path,
        shops: list[str],
        since_days: int = 3,
        parent: QObject | None = None,
        *,
        submitted_since: str | None = None,
    ) -> None:
        super().__init__(parent)
        self._python = str(pddchat_python)
        self._main_py = str(main_py)
        self._root = str(pddchat_root)
        self._since_days = since_days
        self._submitted_since = normalize_since(submitted_since)
        self._queue: list[str] = list(shops)
        self._io: ProcessLineIO | None = None
        self._current_shop: str | None = None
        self._got_result = False
        self._cancelled = False

    def start(self) -> None:
        self._run_next()

    def _run_next(self) -> None:
        if self._cancelled or not self._queue:
            self.all_finished.emit()
            return

        shop = self._queue.pop(0)
        self._current_shop = shop
        self._got_result = False

        cmd = [
            self._python, self._main_py,
            "--shop", shop, "--check-appeal-results",
            "--since-days", str(self._since_days),
            "--result-submitted-since", self._submitted_since or "all",
        ]
        self._io = ProcessLineIO(cmd, cwd=self._root, parent=self)
        self._io.line_received.connect(self._on_line)
        self._io.process_exited.connect(self._on_exit)
        self._io.start()
        self.shop_started.emit(shop)

    def _on_line(self, msg: dict) -> None:
        shop = self._current_shop
        if shop is None:
            return
        msg_type = msg.get("type")

        if msg_type == "progress":
            self.shop_progress.emit(
                shop, int(msg.get("page", 0)),
                int(msg.get("checked", 0)), int(msg.get("matched", 0)),
            )
        elif msg_type == "check_result":
            self._got_result = True
            if msg.get("success") and msg.get("skipped"):
                self.shop_skipped.emit(shop, str(msg.get("message") or "没有符合条件的待查订单。"))
            self.shop_finished.emit(
                shop, bool(msg.get("success")),
                int(msg.get("updated_count", 0)),
                str(msg.get("message") or ""),
            )

    def _on_exit(self, exit_code: int) -> None:
        shop = self._current_shop
        if shop is not None and not self._got_result:
            stderr_text = self._io.stderr_text().strip() if self._io is not None else ""
            message = f"子进程异常退出（退出码 {exit_code}），未收到结果。"
            if stderr_text:
                logger.error(
                    "check-appeal-results 子进程异常退出（退出码 %d），完整 stderr：\n%s",
                    exit_code, stderr_text,
                )
                message += "\n" + stderr_text[-500:]
            self.shop_finished.emit(shop, False, 0, message)

        self._current_shop = None
        self._run_next()

    def cancel(self) -> None:
        """不再继续排队里剩下的店铺；当前正在跑的这一个不强杀（避免在
        数据库写到一半时被打断），等它自然结束再停——跟 LoginWorker.cancel()
        的"发 close 不强杀"是同一个考虑，只是这里的协议本身没有 close
        指令（--check-appeal-results 是同步跑完自己退出的一次性子进程，
        见 main.py 的 run_check_appeal_results_mode 文档），没有指令可发，
        只能等它自己结束。
        """
        self._cancelled = True
