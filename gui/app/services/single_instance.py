"""GUI 应用本身的单实例保护——避免同事不小心把桌面图标双击了两次、或者
上一次窗口还没完全退出又点了一次，导致同时开两个窗口互相冲突（两边都能
弹批量任务、都能同时改 disabled_shops.json/auto_schedule.json，行为不
可预期）。

跟 scheduler_core.LOCK_FILE 是两把不同的锁，保护的对象不一样：那把锁
保护"同一时刻只能有一个批量调度在跑"（GUI 的 C 页和 CLI 的
run_shops_interleaved.py 共享一把）；这把锁保护"同一时刻只能有一个 GUI
窗口进程在跑"——即使两边都没点"启动批量处理"，同时开两个 GUI 窗口本身
就已经是要避免的状态，所以要在 app/main.py 的 main() 一开始、还没创建
MainWindow 之前就检查，不是等用户点某个按钮才检查。两把锁分开存两个
文件（这把锁是 state/gui_app.lock，不是 scheduler.lock），因为两件事
的生命周期完全不同，混在一个文件里会让"批量任务在跑"和"GUI 窗口开着"
这两件事互相污染判断。

存活性校验直接复用 scheduler_core.is_pid_running()（同一套"读锁文件里的
pid，用 OpenProcess 查真进程是否还活着"逻辑，不新造一遍）——锁文件存在
但里面的 pid 已经不在跑了（上次异常退出没走到 release()，比如被任务管理器
强杀），视为过期，自动收回。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

# 延后到这里才 import project_paths/scheduler_core：跟 app/main.py 里
# `from .main_window import MainWindow` 是同一个顾虑——只有 PROJECT_PATHS.
# validate() 已经确认过 pddchat_root 配置没问题，import scheduler_core
# 才不会因为 config.json 配错、sys.path 指到一个不存在的目录而在这里
# ModuleNotFoundError 崩溃（比"配置有误"那条友好提示还早崩，体验最差）。
# 这里仍然显式 import 一次 project_paths（哪怕 app/main.py 早就 import
# 过了）只是防御性地不依赖调用方的 import 顺序——跟 add_shop_dialog.py
# 顶部的同款注释是同一个思路。
from .project_paths import PROJECT_PATHS  # noqa: F401  (import 时机很关键，见上)

import scheduler_core  # noqa: E402

LOCK_FILE = scheduler_core.BASE_DIR / "state" / "gui_app.lock"


def try_acquire() -> bool:
    """尝试拿到"GUI 只能开一个"这把锁。

    拿到锁返回 True——调用方必须在进程退出前调用 release()（用
    try/finally 包住 QApplication.exec_() 那一段，覆盖正常退出和中途
    抛异常两种情况；真正的进程被强杀/崩溃这种连 finally 都不走的场景，
    交给下一次启动时的存活性校验自动收回，见模块文档）。

    锁文件不存在，或者存在但里面的 pid 已经不在跑了，都视为没有别的
    实例在跑，直接覆盖写入自己的信息、拿到锁。锁文件存在且里面的 pid
    还活着——说明真的已经有一个 GUI 实例在跑，返回 False，调用方应该
    弹提示后直接退出，不要创建 MainWindow。
    """
    existing = _read_status()
    if existing is not None and scheduler_core.is_pid_running(existing.get("pid", -1)):
        return False
    _write(pid=os.getpid())
    return True


def release() -> None:
    """释放锁——只在锁文件里的 pid 仍然是自己时才删，避免误删别的进程
    刚好抢到的新锁（正常退出流程下不会并发到这个地步，多一层判断没坏处，
    跟 scheduler_core.Scheduler._release_lock 是同一个思路）。"""
    existing = _read_status()
    if existing is not None and existing.get("pid") == os.getpid():
        try:
            LOCK_FILE.unlink()
        except OSError:
            pass


def _read_status() -> dict | None:
    if not LOCK_FILE.is_file():
        return None
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "pid" not in data:
        return None
    return data


def _write(pid: int) -> None:
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "pid": pid,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    LOCK_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
