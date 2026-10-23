"""最多三个独立无头检测进程；只在进程退出后释放并发名额。"""
import json
import subprocess
import sys

from PyQt5.QtCore import QObject, QProcess, QTimer, pyqtSignal

from ..services.project_paths import PROJECT_PATHS


class BatchLoginCheck(QObject):
    result = pyqtSignal(str, object, str)
    progress = pyqtSignal(int, int)
    finished = pyqtSignal()

    def __init__(self, shops, parent=None):
        super().__init__(parent)
        self.pending = list(shops)
        self.total = len(shops)
        self.done = 0
        self.active = {}
        self.stopped = False

    def start(self):
        while self.pending and len(self.active) < 3 and not self.stopped:
            shop = self.pending.pop(0)
            process = QProcess(self)
            timer = QTimer(process)
            timer.setSingleShot(True)
            self.active[process] = (shop, timer)
            process.setWorkingDirectory(str(PROJECT_PATHS.root))
            process.finished.connect(lambda *_, p=process: self._complete(p))
            process.errorOccurred.connect(lambda error, p=process: self._error(p, error))
            timer.timeout.connect(lambda p=process: self._terminate(p))
            process.start(str(PROJECT_PATHS.python), [str(PROJECT_PATHS.main_py), "--shop", shop, "--check-login"])
            timer.start(90_000)
        if not self.active:
            self.finished.emit()

    def _error(self, process, error):
        if error == QProcess.FailedToStart:
            self._complete(process)

    def _complete(self, process):
        if process not in self.active:
            return
        shop, timer = self.active.pop(process)
        timer.stop()
        success = None
        message = "检测未完成：进程启动失败、异常退出或超时"
        for line in bytes(process.readAllStandardOutput()).decode("utf-8", "replace").splitlines():
            try:
                payload = json.loads(line)
                if payload.get("type") == "login_check_result":
                    success, message = payload.get("success"), payload.get("message", "")
            except (ValueError, AttributeError):
                pass
        if self.stopped:
            success, message = None, "检测已停止"
        self.result.emit(shop, success, message)
        self.done += 1
        self.progress.emit(self.done, self.total)
        process.deleteLater()
        self.start()

    def _terminate(self, process):
        pid = int(process.processId())
        if pid and sys.platform == "win32":
            try:
                subprocess.Popen(["taskkill", "/PID", str(pid), "/T", "/F"],
                                 creationflags=subprocess.CREATE_NO_WINDOW,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                process.kill()
        else:
            process.kill()

    def stop(self):
        self.stopped = True
        self.pending.clear()
        for process in list(self.active):
            self._terminate(process)
