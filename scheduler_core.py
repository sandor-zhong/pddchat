"""多店铺逐单跨店随机调度——GUI 无关的可复用核心逻辑。

从 run_shops_interleaved.py 抽出。**这个模块永远不能 import 任何 UI 框架
（尤其是 PyQt5）**——它同时要满足两个调用方：

1. run_shops_interleaved.py：主项目 3.12 venv 里的 CLI 入口，继续原样跑；
2. gui/ 子项目：独立的旧版本 Python venv（不装 playwright/numpy/openai），
   通过 sys.path 直接 import 这个文件。能做到这一点，是因为这个模块本身
   + 它依赖的 db.py 都是纯标准库（json/sqlite3/subprocess/threading/queue/
   dataclasses），不需要装任何第三方包就能 import 成功。

调度状态变化通过 SchedulerEvent 对外广播（构造 Scheduler 时传入的
on_event 回调），而不是直接 print()——CLI 一侧提供一个把事件还原成原来
终端文案的回调（run_shops_interleaved.py 里的 _print_event），GUI 一侧
提供一个把事件转成 Qt 信号发射的回调。事件里的 line 字段是预先格式化好的
完整展示文本（可能带换行/分隔线），CLI 回调只需要 print(event.line) 就能
拿到跟重构前逐字一致的输出；GUI 既可以把 line 原样丢进日志面板，也可以用
event.type/event.shop/event.data 这些结构化字段驱动更精细的界面更新
（比如"当前第几单/共几单"的实时小表格）。

协议细节见 main.py 的 run_serve_mode() 文档字符串（stdin/stdout 上的
JSON Lines 指令：init/next/stop → ready/result/queue_empty/error）。
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import random
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import db
import memory_monitor

# 调度器自身不 import paths（避免与店铺路径固化耦合），项目根目录直接取自
# 本文件位置，与 paths.BASE_DIR 指向同一目录（沿用 run_shops.py 的写法）。
# db.py 是刻意设计成跟 paths.py/PDDCHAT_SHOP 解耦的（shop 都是显式参数传
# 入，见 db.py 顶部文档），import 它不违反"调度器不引入店铺路径固化耦合"
# 这条既有原则。
BASE_DIR = Path(__file__).resolve().parent
MAIN_PY = BASE_DIR / "main.py"
PROFILES_DIR = BASE_DIR / "state" / "profiles"

# 全局调度器锁：GUI 的 C 页和 CLI 的 run_shops_interleaved.py 都经过
# Scheduler 类，两边共享这一个文件就自动一起获得"同一时刻只能有一个调度
# 进程在跑"的保护，不用分别在两处各写一套。不做按店铺粒度的锁——这是
# 单人操作场景下够用的粗粒度保护，见 Scheduler._acquire_lock。
LOCK_FILE = BASE_DIR / "state" / "scheduler.lock"

# 打包后 GUI 用 pythonw.exe（无控制台）启动时，起一个控制台版 python.exe
# 子进程如果不显式抑制，Windows 会给它新分配一个控制台窗口、一闪而过。
# CLI 场景（run_shops_interleaved.py 本身就在控制台里跑）加这个也无害——
# 子进程的 stdout/stdin 本来就整个被接管成管道（下面 subprocess.PIPE），
# 不依赖一个真实可见的控制台窗口。CREATE_NO_WINDOW 只在 Windows 上存在，
# 且这个项目本来就是 Windows-only。
_POPEN_CREATIONFLAGS = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

# 子进程的人类可读运行日志走 stderr，协议消息独占 stdout。安装版 GUI 由
# pythonw.exe 启动，本身没有控制台，所以 ShopWorker 必须显式给 stderr
# 建管道；否则子进程继承到的 stderr 句柄可能不可用，main.py 把普通日志
# 切到 stderr 后会在真正打开浏览器之前直接退出。这里只保留尾部有限行数，
# 避免长时间运行时无上限占用内存。
_STDERR_MAX_LINES = 500
_STDERR_DIAGNOSTIC_MAX_LINES = 12
_STDERR_DIAGNOSTIC_MAX_CHARS = 4000

# 等一家店回一条协议消息的默认超时（秒）。init 要翻页采集整张表，给宽松些；
# 正常情况下不会触发，只是防止某个子进程卡死时调度器跟着死等。
DEFAULT_RESPONSE_TIMEOUT = 300.0
# --manual-captcha 时子进程可能在等真人手动过验证码，超时要放宽很多。
MANUAL_CAPTCHA_RESPONSE_TIMEOUT = 1800.0

# 同时最多打开几家店铺浏览器的默认值。
DEFAULT_MAX_CONCURRENT = 4

# 某店铺连续失败几单（比如登录失效导致每单都失败）就判定它状态异常、
# 关闭让位的默认阈值。
DEFAULT_MAX_CONSECUTIVE_ERRORS = 3

# 拟人休息：累计运行满这个区间（小时，随机取一个值）就主动歇一会儿，
# 歇够这个区间（分钟，同样随机取）再继续——避免"一个不知疲倦的机器
# 连续跑一整天"这种本身就容易被识别的规律性。默认开启，不需要用户
# 特地去打开这个开关就已经是这个行为。见 Scheduler._run_loop() 里的
# 具体触发逻辑。
DEFAULT_REST_ENABLED = True
DEFAULT_REST_MIN_HOURS = 3
DEFAULT_REST_MAX_HOURS = 4
DEFAULT_REST_MIN_MINUTES = 20
DEFAULT_REST_MAX_MINUTES = 30

# "跨天优先级"回看多少天：勾选的店铺数超过并发数（pool_size）时，某家店
# 铺可能连续好几天都排不上号（比如别的店铺积压订单多、总是优先被随机
# 抽中）。往前最多回看这么多天，数一数这家店铺"连续多少天完全没轮到
# 开工"，回看天数越多、这次排位就越靠前——直到真的被处理一次（done/
# failed 都算"处理过"，连续计数会断开）。14 天封顶只是防止历史很长时
# 这个回看查询没意义地做太多次数据库查询，不是说"14 天后就不管了"——
# 达到封顶值的店铺仍然会排在最前面，跟真实积压天数是不是恰好 14 天无关。
SKIP_PRIORITY_LOOKBACK_DAYS = 14


def _is_failure_result(result_text: str) -> bool:
    """判断一条 result 响应的文本是不是"这单失败了"。

    跟 main.py 里 run_per_order_pipeline/主流程汇总报告用的是同一套词汇
    约定：submit_single_order 失败时返回 "失败: {exc}"，
    process_and_submit_one_order 分析阶段失败时返回 "分析失败: {exc}"。
    "未找到申诉记录"/各种终态跳过（"已申诉-跳过"/"已超时-跳过"/"已驳回-
    跳过"……）不算失败，是正常的业务结果。
    """
    return result_text.startswith("失败") or result_text.startswith("分析失败")


def format_duration(seconds: float) -> str:
    """把秒数格式化为适合运行日志的紧凑中文耗时。"""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}小时{minutes}分{secs}秒"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def overall_shop_progress(
    shops: list[str],
    terminal_shops: set[str],
    initial_totals: dict[str, int],
    remaining_by_shop: dict[str, int],
) -> float:
    """按店铺等权计算不倒退的总体进度，返回 0.0～1.0。"""
    if not shops:
        return 0.0
    accumulated = 0.0
    for shop in shops:
        if shop in terminal_shops:
            accumulated += 1.0
            continue
        total = initial_totals.get(shop)
        if total is None or total <= 0:
            continue
        remaining = min(total, max(0, remaining_by_shop.get(shop, total)))
        # 预留最后 2% 给队列清空确认/平台收尾，避免剩余变成 0 后界面先
        # 显示 100%，随后浏览器还在做最后一步。
        accumulated += (total - remaining) / total * 0.98
    return min(1.0, max(0.0, accumulated / len(shops)))


def discover_shops() -> list[str]:
    """扫描 state/profiles/ 下的一级子目录，返回店铺 slug 列表。

    过滤隐藏目录，目录不存在或为空时返回空列表。CLI 默认把这份全量列表
    原样传给 Scheduler；GUI 可以先调这个函数拿到全量列表给用户勾选，
    再把勾选后的子集传给 Scheduler——discover_shops 本身不关心"这次要
    处理哪几家"，只负责"这台机器上登录过哪些店铺"。
    """
    if not PROFILES_DIR.is_dir():
        return []
    return sorted(
        d.name
        for d in PROFILES_DIR.iterdir()
        if d.is_dir() and not d.name.startswith(".")
    )


def is_pid_running(pid: int) -> bool:
    """Windows 专用：查询某个 PID 当前是不是一个还存活的进程。

    直接用 ctypes 调 OpenProcess+GetExitCodeProcess 查真实进程状态，不用
    "心跳多久没更新就当过期"这种模糊超时判断——不会因为一次验证码人工
    处理等了很久就被误判成"锁过期"，也不会在进程真崩溃后还要傻等一个
    超时窗口才能重新抢锁。项目本身就是 Windows-only（见 login.py/main.py
    里其它 Windows 专用调用），这里不做跨平台兼容。
    """
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        # 打不开句柄：同一台机器上自己起的进程正常情况下不会撞到权限问题，
        # 打不开基本就是这个 pid 已经不存在了，按"没在跑"处理。
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def read_lock_status() -> dict | None:
    """只读地看一眼锁文件内容，不做存活性校验（那是 Scheduler._acquire_lock
    抢锁时才需要做的完整判断）。给 GUI 侧"要不要弹个提示"这种轻量场景用
    （见 add_shop_dialog.py）。读不到文件/格式不对时返回 None——锁文件
    本来就只是个提示性保护，不是强一致性保证，保守地当作"没有锁"即可。
    """
    if not LOCK_FILE.is_file():
        return None
    try:
        data = json.loads(LOCK_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or "pid" not in data:
        return None
    return data


def active_operation_lock() -> dict | None:
    """返回当前仍存活的运行锁；无锁、坏锁或所属进程已退出时返回 None。"""
    existing = read_lock_status()
    try:
        pid = int((existing or {}).get("pid", -1))
    except (TypeError, ValueError):
        return None
    return existing if existing is not None and is_pid_running(pid) else None


def acquire_operation_lock(
    shops: list[str],
    operation: str,
) -> tuple[str | None, dict | None]:
    """为会打开店铺浏览器的操作抢占全局运行锁。

    批量处理、手动查询申诉结果和定时查询申诉结果都必须经过这里，避免
    两个进程同时打开同一个 ``state/profiles/<shop>`` Chrome 用户目录。
    返回 ``(lock_id, None)`` 表示成功；返回 ``(None, existing)`` 表示
    已有存活任务占用。用临时文件 + 硬链接发布完整 JSON，创建目标文件这
    一步具有排他性，避免两个入口在同一瞬间都认为自己抢锁成功。
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_id = uuid.uuid4().hex
    payload = {
        "pid": os.getpid(),
        "lock_id": lock_id,
        "operation": operation,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "shops": list(shops),
    }
    temp_path = LOCK_FILE.with_name(
        f".{LOCK_FILE.name}.{os.getpid()}.{lock_id}.tmp"
    )
    temp_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )

    try:
        for _ in range(3):
            try:
                os.link(temp_path, LOCK_FILE)
                return lock_id, None
            except FileExistsError:
                existing = read_lock_status()
                try:
                    existing_pid = int((existing or {}).get("pid", -1))
                except (TypeError, ValueError):
                    existing_pid = -1
                if existing is not None and is_pid_running(existing_pid):
                    return None, existing

                # 锁所属进程已经退出，或旧锁内容损坏：删除后重试。若此时
                # 被另一个进程抢先删除/创建，下一轮会重新读取并正确判断。
                try:
                    LOCK_FILE.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    return None, existing
        return None, read_lock_status()
    finally:
        try:
            temp_path.unlink()
        except OSError:
            pass


def release_operation_lock(lock_id: str | None) -> None:
    """仅释放由 ``lock_id`` 标识的自己的锁，绝不删除后来者的新锁。"""
    if not lock_id:
        return
    existing = read_lock_status()
    if existing is None or existing.get("lock_id") != lock_id:
        return
    try:
        LOCK_FILE.unlink()
    except OSError:
        pass


class ShopWorker:
    """一个店铺的常驻子进程：管理协议收发，并持续收集 stderr 诊断。"""

    def __init__(
        self,
        shop: str,
        extra: list[str],
        python_executable: str | Path = sys.executable,
    ) -> None:
        """python_executable：跑 main.py 用的解释器路径，默认 sys.executable
        （原来的行为——调度器和子进程用同一个解释器，CLI 场景下不变）。

        GUI 场景下调度器本身跑在一个不装 playwright/numpy/openai 的独立
        旧版本 venv 里，sys.executable 指向的是那个 venv 的 python，不能
        拿来跑 main.py（main.py 需要主项目 3.12 venv 里的那些依赖）——
        这时候 GUI 必须显式传入主项目 venv 的 python.exe 路径。
        """
        self.shop = shop
        self.remaining = 0  # 仅用于终端展示的估计值，不是判断"队列是否清空"的依据
        self.initial_total = 0
        self.processed_count = 0
        self.order_duration_total = 0.0
        self.started_monotonic = time.monotonic()
        self.consecutive_failures = 0  # 连续失败单数——判断"是不是登录失效之类的整体异常"
        self.alive = True
        self.out_queue: "queue.Queue[dict]" = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=_STDERR_MAX_LINES)
        self._stderr_lock = threading.Lock()

        cmd = [str(python_executable), str(MAIN_PY.with_name('memory_worker.py')), "--shop", shop, "--serve", *extra]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(BASE_DIR),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",  # 显式钉死，不吃系统 locale——协议消息里全是
            # 中文（"成功"/"失败: ..."等），管道两端编码只要有一处走了
            # locale 默认值（Windows 常见 cp936），中文内容会悄悄错位/
            # 乱码但不报错，调度器靠字符串前缀判断结果的逻辑会全部失灵却
            # 毫无提示。main.py --serve 那边也显式钉死了 UTF-8，两边必须
            # 保持一致。
            errors="replace",  # 原生层错误未必是 UTF-8；诊断不能因解码失败丢失
            bufsize=1,
            creationflags=_POPEN_CREATIONFLAGS,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        # stderr 必须持续排空；只设 PIPE 却不读，写满管道后会把子进程反压
        # 卡死，表面上又会变成一次 init 超时。
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        """后台线程：不断读子进程 stdout 的协议 JSON 行，丢进 out_queue。

        只做"抄网线"，不碰浏览器——浏览器操作完全在子进程里单线程执行，
        这里没有任何线程安全问题。子进程退出（管道 EOF）时把 alive 置 False。
        """
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if msg.get('type') == 'memory_status':
                    callback = getattr(self, 'diagnostic_callback', None)
                    if callback is not None:
                        callback('[内存回收] ' + str(msg.get('message','')))
                    continue
                if msg.get('type') == 'activity':
                    callback = getattr(self, 'activity_callback', None)
                    if callback is not None:
                        callback(msg)
                    continue
                self.out_queue.put(msg)
        finally:
            self.alive = False

    def _read_stderr(self) -> None:
        """持续排空并保留子进程 stderr 尾部，供异常退出时诊断。"""
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                with self._stderr_lock:
                    self._stderr_lines.append(line)
                callback = getattr(self, "diagnostic_callback", None)
                if callback is not None and line.startswith("[验证码诊断]"):
                    callback(line.strip())
        except (OSError, ValueError):
            # 进程收尾时管道可能先被关闭；已有诊断仍然可以正常读取。
            pass

    def stderr_text(self, wait_for_eof: bool = False) -> str:
        """返回目前捕获的 stderr；进程已退时可短暂等待读取线程收完尾部。"""
        if wait_for_eof and self.proc.poll() is not None:
            self._stderr_reader.join(timeout=1.0)
        with self._stderr_lock:
            return "".join(self._stderr_lines)

    def no_response_reason(self, operation: str, timeout: float) -> tuple[str, str]:
        """把“没收到协议响应”拆成超时或提前退出，并带回 stderr 摘要。"""
        exit_code = self.proc.poll()
        if exit_code is None:
            reason = f"{operation} 超时（等待 {timeout:g} 秒）"
        else:
            reason = f"子进程提前退出（退出码 {exit_code}）"

        stderr_text = self.stderr_text(wait_for_eof=exit_code is not None).strip()
        if not stderr_text:
            return reason, ""

        lines = [line for line in stderr_text.splitlines() if line.strip()]
        summary = "\n".join(lines[-_STDERR_DIAGNOSTIC_MAX_LINES:])
        truncated = len(lines) > _STDERR_DIAGNOSTIC_MAX_LINES
        if len(summary) > _STDERR_DIAGNOSTIC_MAX_CHARS:
            summary = summary[-_STDERR_DIAGNOSTIC_MAX_CHARS:]
            truncated = True
        if truncated:
            summary = "……（仅显示错误输出尾部）\n" + summary
        return reason, summary

    def send(self, cmd: dict) -> bool:
        """给子进程发一条指令。子进程已死或管道已关时返回 False。"""
        if not self.alive or self.proc.stdin is None or self.proc.stdin.closed:
            return False
        try:
            self.proc.stdin.write(json.dumps(cmd, ensure_ascii=False) + "\n")
            self.proc.stdin.flush()
            return True
        except (BrokenPipeError, OSError, ValueError):
            self.alive = False
            return False

    def wait_response(
        self,
        timeout: float,
        stop_event: threading.Event | None = None,
    ) -> dict | None:
        """阻塞等这家店回一条协议消息。子进程中途退出、超时、或者
        stop_event 被置位都返回 None。

        用短轮询代替一次性长超时，是为了子进程真的崩溃/退出时能在一两秒内
        发现（不用死等到 timeout 才反应过来），同时仍然允许合法的长耗时
        操作（比如人工手动过验证码）在超时时间内正常完成。stop_event 走
        同一条短轮询路径检查，所以哪怕正在等一个 1800 秒的人工验证码
        超时，GUI 点"停止"也能在至多 1 秒内让这次等待提前返回。
        """
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                return self.out_queue.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                if not self.alive:
                    return None
                if stop_event is not None and stop_event.is_set():
                    return None

    def stop(self, timeout: float = 10.0) -> None:
        """让子进程干净退出：发 stop，等一会儿，还不退出就强杀兜底。"""
        if self.alive:
            self.send({"cmd": "stop"})
        try:
            self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self._stderr_reader.join(timeout=1.0)

    def focus_browser(self) -> bool:
        return self.proc.poll() is None and memory_monitor.focus_browser_window(self.proc.pid)

    def set_browser_topmost(self, enabled: bool) -> bool:
        return self.proc.poll() is None and memory_monitor.set_browser_topmost(self.proc.pid, enabled)


@dataclass(frozen=True)
class SchedulerEvent:
    """一次调度器状态变化的结构化描述。

    type：事件类型（见下面各处 emit 调用时传入的字符串常量，命名即含义，
          没有单独维护一份枚举——事件种类会随着调度逻辑演进自然增减，
          用字符串常量比强类型枚举更不容易在改动时漏改）。
    shop：这个事件关联的店铺（跟店铺无关的事件，比如整体轮次开始/空闲
          等待，传 None）。
    data：事件携带的结构化数据（比如 order_no/result_text/queue_size），
          字段随 type 变化，调用方按需取用。
    line：预先格式化好的完整展示文本（可能内含换行/分隔线），忠实对应
          重构前那一行/那几行 print() 的内容——CLI 侧回调只需要
          print(event.line) 就能拿到逐字一致的终端输出，GUI 侧也可以
          原样丢进日志面板，不强求理解 type/data 的语义。
    """

    type: str
    shop: str | None
    data: dict = field(default_factory=dict)
    line: str = ""


class Scheduler:
    """多店铺逐单跨店随机调度器的核心状态机（不依赖任何 UI 框架）。

    调度规则的主体跟重构前一致（细节见 run_shops_interleaved.py 模块
    文档字符串，这里不重复）：每一轮先按 shop_daily_status 过滤出今天
    还没跑完的店铺、同时最多开 pool_size 家浏览器、谁先处理完就关掉换
    排队里的下一家补位、一家店连续失败达到阈值就判定异常关闭让位、一轮
    跑完（非 --once）自动开始下一轮。

    在此之上加了"拟人休息"：累计运行满 rest_min_hours~rest_max_hours
    小时（随机）就主动关掉所有店铺浏览器，歇 rest_min_minutes~
    rest_max_minutes 分钟（同样随机）再继续，避免"一个不知疲倦的机器
    连续跑一整天"这种规律性本身成为容易被识别的自动化特征。默认开启
    （rest_enabled=True），GUI/CLI 都不特地配置也是这个行为。见
    _run_loop() 里具体的触发/等待/恢复逻辑。
    """

    def __init__(
        self,
        shops: list[str],
        extra: list[str] | None = None,
        pool_size: int = DEFAULT_MAX_CONCURRENT,
        max_consecutive_errors: int = DEFAULT_MAX_CONSECUTIVE_ERRORS,
        response_timeout: float = DEFAULT_RESPONSE_TIMEOUT,
        python_executable: str | Path = sys.executable,
        on_event: Callable[[SchedulerEvent], None] | None = None,
        rest_enabled: bool = DEFAULT_REST_ENABLED,
        rest_min_hours: float = DEFAULT_REST_MIN_HOURS,
        rest_max_hours: float = DEFAULT_REST_MAX_HOURS,
        rest_min_minutes: float = DEFAULT_REST_MIN_MINUTES,
        rest_max_minutes: float = DEFAULT_REST_MAX_MINUTES,
    ) -> None:
        self.shops = list(shops)
        self.extra = list(extra) if extra else []
        self.pool_size = max(1, pool_size)
        self.max_consecutive_errors = max(1, max_consecutive_errors)
        self.response_timeout = response_timeout
        self.python_executable = python_executable
        self.on_event = on_event
        self.rest_enabled = rest_enabled
        self.rest_min_hours = rest_min_hours
        self.rest_max_hours = rest_max_hours
        self.rest_min_minutes = rest_min_minutes
        self.rest_max_minutes = rest_max_minutes

        # active：当前打开着浏览器、在轮转中的店铺，大小不超过 pool_size。
        self.active: dict[str, ShopWorker] = {}
        self._stop_event = threading.Event()
        self._operation_lock_id: str | None = None

    # ---- 对外控制 ----

    def request_stop(self) -> None:
        """线程安全：从任意线程调用都能让 run_once()/run_forever() 尽快
        （至多约 1 秒，见 ShopWorker.wait_response 的 stop_event 检查）
        退出循环，并触发跟 KeyboardInterrupt 时一样的收尾清理。"""
        self._stop_event.set()

    def is_stopping(self) -> bool:
        return self._stop_event.is_set()

    def focus_browser(self, shop: str) -> bool:
        worker = self.active.get(shop)
        return bool(worker and worker.focus_browser())

    def set_browser_topmost(self, shop: str, enabled: bool) -> bool:
        worker = self.active.get(shop)
        return bool(worker and worker.set_browser_topmost(enabled))

    # ---- 全局锁：防止 GUI 和手动 CLI 调度器同时跑，互相抢同一份
    # state/profiles/<shop>/ 登录态目录 ----

    def _acquire_lock(self) -> bool:
        """抢全局调度器锁。锁文件不存在，或者存在但里面的 pid 已经不在跑了
        （上次没走到 finally，比如被任务管理器强杀）→ 视为过期，直接覆盖
        写入自己的信息，拿到锁；锁文件存在且里面的 pid 还活着 → 拿不到锁。
        """
        lock_id, existing = acquire_operation_lock(self.shops, "批量处理")
        if lock_id is None:
            existing = existing or {}
            shops_text = ", ".join(existing.get("shops") or []) or "（未知）"
            operation = existing.get("operation") or "其它任务"
            self._emit(
                "lock_conflict",
                f"[调度器] 检测到已有{operation}正在运行，本次启动已取消："
                f"pid={existing.get('pid')}，从 {existing.get('started_at')} 开始在跑，"
                f"店铺列表：{shops_text}",
                pid=existing.get("pid"),
                started_at=existing.get("started_at"),
                shops=existing.get("shops"),
                operation=operation,
            )
            return False
        self._operation_lock_id = lock_id
        return True

    def _release_lock(self) -> None:
        """只释放本次 Scheduler 实例实际取得的那一把锁。"""
        release_operation_lock(self._operation_lock_id)
        self._operation_lock_id = None

    # ---- 事件广播 ----

    def _emit(self, type_: str, line: str, shop: str | None = None, **data) -> None:
        if self.on_event is not None:
            self.on_event(SchedulerEvent(type=type_, shop=shop, data=data, line=line))

    # ---- 单店铺开局 ----

    def _start_worker(self, shop: str) -> bool:
        """新开一个店铺的常驻子进程并发 init，成功入池返回 True。

        失败（发送失败/超时/init 报错/当前没有可处理订单）不会重试这家店，
        调用方会继续尝试排队里的下一家——跟原本"某店铺异常记录后跳过、
        不中断整体"的容错哲学一致。
        """
        init_started = time.monotonic()
        try:
            w = ShopWorker(shop, self.extra, python_executable=self.python_executable)
            w.diagnostic_callback = lambda line: self._emit(
                "captcha_diagnostic", f"[{shop}] {line}", shop=shop
            )
            w.activity_callback = lambda message: self._emit(
                "shop_activity",
                f"[调度器] 店铺 [{shop}] {message.get('stage') or '正在处理'}"
                + (f"，订单 {message.get('order_no')}" if message.get('order_no') else ""),
                shop=shop,
                stage=str(message.get("stage") or "正在处理"),
                order_no=str(message.get("order_no") or ""),
            )
        except OSError as exc:
            reason = f"无法启动子进程: {exc}"
            duration = time.monotonic() - init_started
            self._emit(
                "worker_init_failed",
                f"[调度器] 店铺 [{shop}] {reason}，跳过"
                f"（耗时 {format_duration(duration)}）。",
                shop=shop, reason=reason,
                duration_seconds=duration,
            )
            db.mark_shop_daily_failed(shop, reason)
            return False
        self._emit("worker_init_start", f"[调度器] → [{shop}] init（新开浏览器补位）", shop=shop)

        if not w.send({"cmd": "init"}):
            duration = time.monotonic() - init_started
            self._emit(
                "worker_init_failed",
                f"[调度器] 店铺 [{shop}] 发送 init 失败，跳过"
                f"（耗时 {format_duration(duration)}）。",
                shop=shop, reason="发送 init 指令失败",
                duration_seconds=duration,
            )
            w.stop()
            db.mark_shop_daily_failed(shop, "发送 init 指令失败")
            return False

        resp = w.wait_response(self.response_timeout, stop_event=self._stop_event)
        if resp is None:
            reason, stderr_summary = w.no_response_reason("init", self.response_timeout)
            diagnostic = f"\n[子进程错误输出]\n{stderr_summary}" if stderr_summary else ""
            duration = time.monotonic() - init_started
            self._emit(
                "worker_init_failed",
                f"[调度器] 店铺 [{shop}] {reason}，跳过"
                f"（耗时 {format_duration(duration)}）。{diagnostic}",
                shop=shop, reason=reason, stderr=stderr_summary,
                duration_seconds=duration,
            )
            w.stop()
            db.mark_shop_daily_failed(shop, reason)
            return False
        if resp.get("type") == "error":
            message = resp.get("message")
            duration = time.monotonic() - init_started
            self._emit(
                "worker_init_failed",
                f"[调度器] 店铺 [{shop}] init 出错: {message}，跳过"
                f"（耗时 {format_duration(duration)}）。",
                shop=shop, reason=str(message or ""),
                duration_seconds=duration,
            )
            w.stop()
            db.mark_shop_daily_failed(shop, str(message or ""))
            return False

        w.remaining = resp.get("queue_size", 0)
        w.initial_total = w.remaining
        w.processed_count = 0
        w.order_duration_total = 0.0
        w.started_monotonic = init_started
        init_duration = time.monotonic() - init_started
        self._emit(
            "worker_ready",
            f"[调度器] ← [{shop}] ready，待处理 {w.remaining} 单"
            f"（初始化耗时 {format_duration(init_duration)}）",
            shop=shop,
            queue_size=w.remaining,
            duration_seconds=init_duration,
        )

        if w.remaining == 0:
            empty_duration = time.monotonic() - init_started
            self._emit(
                "worker_empty_queue",
                f"[调度器] 店铺 [{shop}] 当前没有可处理订单，"
                f"不占用名额（检查耗时 {format_duration(empty_duration)}）。",
                shop=shop,
                remaining=0,
                duration_seconds=empty_duration,
            )
            w.stop()
            db.mark_shop_daily_done(shop)  # 登录、采集都成功，只是没有可处理订单——正常结束，不是失败
            return False

        self.active[shop] = w
        return True

    def _skip_priority(self, shop: str) -> int:
        """这家店铺"连续多少天完全没轮到开工"——用来决定它这一轮排队时
        应该插到多靠前。从昨天开始往回数，只要哪天状态不是 'skipped'
        （不管是 done/failed，还是那天压根没记录，比如那天没勾选这家店）
        就断开、停止计数——只关心"最近一段连续被冷落的天数"，不需要精确
        统计"历史上总共被冷落多少天"这种更复杂的东西。
        """
        streak = 0
        day = date.today()
        for _ in range(SKIP_PRIORITY_LOOKBACK_DAYS):
            day -= timedelta(days=1)
            status = (db.get_shop_daily_status(shop, run_date=day.isoformat()) or {}).get("status")
            if status != "skipped":
                break
            streak += 1
        return streak

    def _order_pending_by_priority(self, pending: list[str]) -> None:
        """原地重排 pending：连续被冷落天数越多的店铺排得越靠前，
        _fill_pool 从队首开始补位，这样"总是没轮到"的店铺下一轮会被
        优先塞进并发名额里。同等优先级（通常是大多数正常轮转的店铺，
        优先级都是 0）内部保持随机——不能因为加了优先级，就让"名字排
        在前面的店铺永远排在同分店铺的最前面"这种新的不公平取代旧的。
        Python 的 sort 是稳定排序，"先随机打乱、再按优先级降序排"正好
        实现"优先级第一，同优先级内随机"这个组合，不需要额外写归并逻辑。
        """
        random.shuffle(pending)
        pending.sort(key=self._skip_priority, reverse=True)

    def _fill_pool(self, pending: list[str]) -> None:
        """从 pending 里挨个尝试补位到并发上限，严格一家一家来（不并发）——
        补位新开浏览器这个动作，也要遵守"同一时刻只有一家在操作"。
        原地修改 pending（弹出已经尝试过的，不管成功与否）。"""
        while len(self.active) < self.pool_size and pending and not self._stop_event.is_set():
            self._start_worker(pending.pop(0))

    def _close_all_active(self) -> None:
        for shop in list(self.active):
            w = self.active.pop(shop)
            if w.alive:
                self._emit("worker_closing", f"[调度器] 关闭 [{shop}] ……", shop=shop)
                w.stop()

    def _roll_seconds(self, min_value: float, max_value: float, unit_seconds: float) -> float:
        """从 [min_value, max_value] 区间随机取一个值，乘以 unit_seconds 换算成秒。

        拟人休息要用到两处结构相同的"从一个区间随机取数、换算成秒"：
        "下次休息前还要跑多久"（区间是小时，unit_seconds=3600）和"这次要
        歇多久"（区间是分钟，unit_seconds=60），抽成一个方法避免重复写
        random.uniform + 换算这两行。min_value > max_value（用户配置反了）
        时 random.uniform 本身也能正常工作，只是区间方向反过来，不会
        报错，这里不额外加防御性校验。
        """
        return random.uniform(min_value, max_value) * unit_seconds

    # ---- 主循环 ----

    def run_once(self) -> None:
        """跑到"这次要处理的店铺都处理过一遍"（一轮）就返回，不循环。
        对应 CLI 的 --once。"""
        self._run(loop_forever=False)

    def run_forever(self) -> None:
        """无限循环，直到 request_stop() 被调用（或调用方在 on_event 里
        自己抛异常/KeyboardInterrupt 冒泡出来）。全员空闲时原地等待、
        定期重新检查，不会自己退出。"""
        self._run(loop_forever=True)

    def _run(self, loop_forever: bool) -> None:
        if not self._acquire_lock():
            # 拿不到锁：还没开任何浏览器，不需要走 _close_all_active 那套
            # 收尾清理，_acquire_lock 里已经 emit 过 lock_conflict 说明原因。
            return
        try:
            self._run_loop(loop_forever)
        except KeyboardInterrupt:
            # 只有 CLI（run_shops_interleaved.py 在一个真实终端里跑）会撞到
            # 这个分支——GUI 场景走的是 request_stop()，不依赖 SIGINT。
            # 捕获后不重新抛出：跟重构前 run_shops_interleaved.py 的
            # main() 一样，打印一句提示、让下面的 finally 正常做收尾清理，
            # 调用方（run_once()/run_forever()）视为正常返回。
            self._emit("interrupted", "\n[调度器] 收到 Ctrl+C，停止调度。")
        finally:
            self._emit("closing_start", "\n[调度器] 正在关闭所有仍存活的店铺子进程……")
            self._close_all_active()
            self._emit("closed_all", "[调度器] 已全部关闭。")
            self._release_lock()

    def _run_loop(self, loop_forever: bool) -> None:
        last_shop: str | None = None
        round_no = 0

        # 拟人休息状态：run_ref 是"距离上次休息结束（或本次运行开始）过了
        # 多久"的参照点，next_rest_after 是这次要跑够多久才休息（每次休息
        # 结束都会重新掷一次骰子，不是固定间隔，见下面 while self.active
        # 循环里的触发逻辑）。rest_enabled=False 时 next_rest_after 留 None，
        # 下面判断条件靠 and 短路，不会真的拿 None 去比较大小。
        run_ref = time.monotonic()
        next_rest_after = (
            self._roll_seconds(self.rest_min_hours, self.rest_max_hours, 3600)
            if self.rest_enabled else None
        )

        while True:
            if self._stop_event.is_set():
                break

            round_no += 1
            # 这一轮是不是因为拟人休息被提前打断（而不是真的处理完/排不上号
            # 才结束）——run_once 模式判断"整个 run 要不要就此结束"时要用，
            # 见本方法末尾的说明。每轮开头重置，只有下面休息-恢复分支会把
            # 它置 True。
            round_cut_short_by_rest = False

            self._emit(
                "round_start",
                "\n" + "=" * 70 + "\n"
                f"[调度器] 第 {round_no} 轮：同时最多 {self.pool_size} 家店铺，"
                "做完一家换一家还没处理的店铺补位\n"
                + "=" * 70,
                round_no=round_no,
            )

            # 今天（本地日期）已经跑完的店铺直接跳过，连浏览器都不开；
            # 失败过的（登录失效等）或者今天还没跑过的才是真正要处理的。
            pending = [
                s for s in self.shops
                if (db.get_shop_daily_status(s) or {}).get("status") != "done"
            ]
            skipped_done = [s for s in self.shops if s not in pending]
            if skipped_done:
                self._emit(
                    "round_skip_done",
                    f"[调度器] 今天已完成，跳过（不开浏览器）: {', '.join(skipped_done)}",
                    shops=skipped_done,
                )

            round_pending_count = len(pending)  # 本轮实际要处理的店铺数——
            # 下面 pending 会被 _fill_pool 不断 pop(0)，到本轮末尾就只剩
            # "完全没轮到开工"的店铺了，不能再用 len(pending) 表示"处理了
            # 多少家"，round_complete 那里要用这个提前存好的值。
            self._order_pending_by_priority(pending)  # 连续被冷落天数越多的店铺排越前面，同优先级内随机
            self._fill_pool(pending)

            if self._stop_event.is_set():
                break

            if not self.active:
                if len(skipped_done) == len(self.shops):
                    self._emit("round_idle", "\n[调度器] 所有店铺今天都已经完成。", all_done=True)
                else:
                    self._emit(
                        "round_idle",
                        "\n[调度器] 本轮没有店铺成功开局（可能都没有可处理订单，或者都出错了）。",
                        all_done=False,
                    )
                if not loop_forever:
                    break
                self._emit("idle_wait", "[调度器] 等待 60 秒后重新检查……", seconds=60)
                if self._stop_event.wait(60):
                    break
                continue

            # ---- 逐单随机点名：谁做完了就关掉、换排队里还没处理的店铺补位 ----
            while self.active:
                if self._stop_event.is_set():
                    break

                candidates = [s for s in self.active if s != last_shop] or list(self.active)
                shop = random.choice(candidates)
                last_shop = shop
                w = self.active[shop]

                self._emit(
                    "order_turn",
                    "\n" + ">" * 70 + "\n"
                    f"[调度器] 轮到店铺 [{shop}] 处理下一单"
                    f"（剩余约 {w.remaining} 单，当前并发 {len(self.active)}/{self.pool_size}）\n"
                    + ">" * 70,
                    shop=shop, remaining=w.remaining,
                    active_count=len(self.active), pool_size=self.pool_size,
                )

                order_started = time.monotonic()
                ok = w.send({"cmd": "next"})
                resp = (
                    w.wait_response(self.response_timeout, stop_event=self._stop_event)
                    if ok else None
                )

                if self._stop_event.is_set() and resp is None:
                    break

                if not ok or resp is None:
                    operation_duration = time.monotonic() - order_started
                    self._emit(
                        "worker_comm_lost",
                        f"[调度器] 店铺 [{shop}] 发送失败/超时/子进程已退出，"
                        f"关闭并让位（本次等待 {format_duration(operation_duration)}）。",
                        shop=shop,
                        duration_seconds=operation_duration,
                    )
                    w.stop()
                    del self.active[shop]
                    db.mark_shop_daily_failed(shop, "处理中途发送失败/超时/子进程退出")
                    self._fill_pool(pending)
                    self._stop_event.wait(random.uniform(1.0, 3.0))
                    continue

                rtype = resp.get("type")
                shop_failed_too_much = False

                if rtype == "queue_empty":
                    shop_duration = time.monotonic() - w.started_monotonic
                    average_duration = (
                        w.order_duration_total / w.processed_count
                        if w.processed_count
                        else 0.0
                    )
                    self._emit(
                        "worker_done",
                        f"[调度器] 店铺 [{shop}] 订单已处理完 ✓，"
                        f"共处理 {w.processed_count} 单，"
                        f"店铺耗时 {format_duration(shop_duration)}"
                        + (
                            f"，平均每单 {format_duration(average_duration)}"
                            if w.processed_count
                            else ""
                        )
                        + "，关闭浏览器让位。",
                        shop=shop,
                        remaining=0,
                        processed_count=w.processed_count,
                        duration_seconds=shop_duration,
                        average_order_seconds=average_duration,
                    )
                    w.stop()
                    del self.active[shop]
                    db.mark_shop_daily_done(shop)
                    self._fill_pool(pending)
                elif rtype == "result":
                    w.remaining = max(0, w.remaining - 1)
                    order_duration = time.monotonic() - order_started
                    w.processed_count += 1
                    w.order_duration_total += order_duration
                    result_text = resp.get("result", "")
                    order_no = resp.get("order_no")
                    self._emit(
                        "order_result",
                        f"[调度器] 店铺 [{shop}] 订单 {order_no}: {result_text}"
                        f"（耗时 {format_duration(order_duration)}）",
                        shop=shop,
                        order_no=order_no,
                        result_text=result_text,
                        remaining=w.remaining,
                        duration_seconds=order_duration,
                    )
                    if _is_failure_result(result_text):
                        w.consecutive_failures += 1
                        shop_failed_too_much = w.consecutive_failures >= self.max_consecutive_errors
                    else:
                        w.consecutive_failures = 0
                elif rtype == "error":
                    order_duration = time.monotonic() - order_started
                    w.processed_count += 1
                    w.order_duration_total += order_duration
                    w.remaining = max(0, w.remaining - 1)
                    order_no = resp.get("order_no", "?")
                    message = resp.get("message")
                    self._emit(
                        "order_error",
                        f"[调度器] 店铺 [{shop}] 订单 {order_no} "
                        f"出错: {message}（耗时 {format_duration(order_duration)}，"
                        "继续下一单）",
                        shop=shop,
                        order_no=order_no,
                        message=message,
                        remaining=w.remaining,
                        duration_seconds=order_duration,
                    )
                    w.consecutive_failures += 1
                    shop_failed_too_much = w.consecutive_failures >= self.max_consecutive_errors
                else:
                    self._emit(
                        "worker_unknown_response",
                        f"[调度器] 店铺 [{shop}] 收到未知响应: {resp}",
                        shop=shop, response=resp,
                    )

                if shop_failed_too_much:
                    shop_duration = time.monotonic() - w.started_monotonic
                    self._emit(
                        "worker_too_many_failures",
                        f"[调度器] 店铺 [{shop}] 连续 {w.consecutive_failures} 单失败"
                        "（可能登录失效或其他持续性异常），关闭并让位，"
                        f"本店已运行 {format_duration(shop_duration)}，下一轮会重新尝试。",
                        shop=shop, consecutive_failures=w.consecutive_failures,
                        duration_seconds=shop_duration,
                    )
                    w.stop()
                    del self.active[shop]
                    db.mark_shop_daily_failed(shop, f"连续 {w.consecutive_failures} 单失败")
                    self._fill_pool(pending)

                # 店铺之间随机停顿，避免节奏过于机械
                if self._stop_event.wait(random.uniform(1.0, 3.0)):
                    break

                # ---- 拟人休息：累计运行够久了，主动歇一会儿 ----
                # 检查点选在这里（一单处理完、下一单还没点名之前）而不是
                # 随时打断——不会有订单被拦腰截断在处理一半的状态。
                if self.rest_enabled and time.monotonic() - run_ref >= next_rest_after:
                    rest_seconds = self._roll_seconds(self.rest_min_minutes, self.rest_max_minutes, 60)
                    resume_at = datetime.now() + timedelta(seconds=rest_seconds)
                    self._emit(
                        "rest_start",
                        "\n" + "~" * 70 + "\n"
                        f"[调度器] 已连续运行约 {(time.monotonic() - run_ref) / 3600:.1f} 小时，"
                        f"模拟休息 {rest_seconds / 60:.0f} 分钟，预计 {resume_at.strftime('%H:%M')} 恢复……\n"
                        + "~" * 70,
                        rest_minutes=round(rest_seconds / 60), resume_at=resume_at.isoformat(),
                    )
                    self._close_all_active()
                    self._stop_event.wait(rest_seconds)
                    if self._stop_event.is_set():
                        # 休息期间被要求停止（手动优雅停止，或到了每日运行
                        # 结束时间）——不当"休息结束"处理，直接走外层收尾。
                        break
                    self._emit("rest_end", "[调度器] 休息结束，继续处理。")
                    run_ref = time.monotonic()
                    next_rest_after = self._roll_seconds(self.rest_min_hours, self.rest_max_hours, 3600)
                    round_cut_short_by_rest = True
                    break  # active 已经清空，跳出内层循环，回到外层重新开一轮补位

            # 走到这里、还留在 pending 里的店铺，是这一轮从头到尾都没轮到
            # 开工的（并发名额一直被别的店铺占满）——不是失败，只是没排上，
            # 单独标记成 'skipped'，供下一轮/下一天 _skip_priority 识别、
            # 优先给它们补位，避免同一批店铺被持续挤占（见 SKIP_PRIORITY_
            # LOOKBACK_DAYS 说明）。
            if pending:
                self._emit(
                    "round_skip_no_slot",
                    f"[调度器] 本轮并发名额不够，未能开工（下一轮优先补位）: {', '.join(pending)}",
                    shops=list(pending),
                )
                for shop in pending:
                    db.mark_shop_daily_skipped(shop, "并发名额不足，本轮未能开工")

            self._emit(
                "round_complete",
                f"\n[调度器] 第 {round_no} 轮：本轮 {round_pending_count} 家店铺处理完毕"
                + (f"，另有 {len(skipped_done)} 家今天已完成、本轮未处理" if skipped_done else "")
                + "。",
                round_no=round_no, processed_count=round_pending_count, skipped_count=len(skipped_done),
            )

            # not round_cut_short_by_rest：GUI 永远用 run_once（loop_forever=
            # False）——如果这一轮是被拟人休息提前打断的（不是真的处理完/
            # 排不上号），不能把它当成"这次 run 该结束了"，否则休息一结束
            # 整个批次就直接终止，"休息完继续处理"就变成一句空话。只有
            # 真正跑完/排不上号结束的轮次，round_cut_short_by_rest 才是
            # False，--once/run_once 才应该在这里退出。
            if not loop_forever and not round_cut_short_by_rest:
                self._emit("once_stop", "[调度器] --once 模式：退出。")
                break
