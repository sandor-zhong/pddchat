"""Bounded JSONL process-tree measurements; no account or page contents."""
import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import threading
import time
import uuid
import hashlib
import zipfile
import sys
import tracemalloc

_lock = threading.Lock()
_trace_lock = threading.Lock()
LOG = Path(__file__).resolve().parent / 'state' / 'logs' / 'memory.jsonl'
RUN_ID = os.environ.setdefault('PDDCHAT_DIAGNOSTIC_RUN', uuid.uuid4().hex)
_context = {}
_runtime_metrics = {}
_last_cleanup = 0.0
_started = time.monotonic()
_trace_last_snapshot = 0.0
_trace_previous_stats = {}
_TRACE_SNAPSHOT_INTERVAL = 300.0
_build = hashlib.sha256(b''.join(
    (Path(__file__).parent / name).read_bytes() for name in
    ('memory_monitor.py', 'memory_worker.py', 'main.py')
    if (Path(__file__).parent / name).exists())).hexdigest()[:16]


# ctypes 会缓存 POINTER(StructureType)。这些类型若在每次采样时动态创建，
# 缓存会持续持有新类型，诊断器本身就会缓慢增长。Windows 结构和 DLL
# 绑定全部只创建一次，后续 30 秒采样直接复用。
class _MemoryStatus(ctypes.Structure):
    _fields_ = [('length', wintypes.DWORD), ('load', wintypes.DWORD)] + [
        (name, ctypes.c_ulonglong) for name in
        ('total', 'available', 'page_total', 'page_available',
         'virtual', 'virtual_available', 'extended')
    ]


class _ProcessEntry(ctypes.Structure):
    _fields_ = [
        ('size', wintypes.DWORD), ('usage', wintypes.DWORD),
        ('pid', wintypes.DWORD), ('heap', ctypes.c_size_t),
        ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
        ('parent', wintypes.DWORD), ('priority', wintypes.LONG),
        ('flags', wintypes.DWORD), ('name', wintypes.WCHAR * 260),
    ]


class _ProcessCounters(ctypes.Structure):
    _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in
        ('peak_ws', 'ws', 'peak_paged', 'paged', 'peak_nonpaged',
         'nonpaged', 'pagefile', 'peak_pagefile', 'private')
    ]


if os.name == 'nt':
    _kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    _psapi = ctypes.WinDLL('psapi', use_last_error=True)
    _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    _kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    _kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ProcessEntry)]
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetProcessHandleCount.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
    ]
    _psapi.GetProcessMemoryInfo.argtypes = [
        wintypes.HANDLE, ctypes.POINTER(_ProcessCounters), wintypes.DWORD
    ]
else:
    _kernel32 = None
    _psapi = None


def configure(**context):
    with _lock:
        _context.update(context)


def update_runtime_metrics(**metrics):
    """更新只含数字/短状态值的运行指标，随下一次采样写入诊断包。"""
    with _lock:
        _runtime_metrics.update(metrics)


def increment_runtime_metric(name, amount=1):
    with _lock:
        _runtime_metrics[name] = int(_runtime_metrics.get(name, 0)) + amount


def start_python_heap_tracking(frames=1):
    """尽早开启 Python 堆跟踪；Qt/Chrome 原生内存不会被误算进来。"""
    with _trace_lock:
        if not tracemalloc.is_tracing():
            tracemalloc.start(frames)


def python_heap_summary(force=False):
    """返回轻量堆指标；每五分钟附带一次增长最大的源码位置。"""
    global _trace_last_snapshot, _trace_previous_stats
    if not tracemalloc.is_tracing():
        return None
    current, peak = tracemalloc.get_traced_memory()
    result = {
        'current_mb': round(current / 1048576, 2),
        'peak_mb': round(peak / 1048576, 2),
        'allocated_blocks': getattr(sys, 'getallocatedblocks', lambda: None)(),
    }
    now = time.monotonic()
    if not force and now - _trace_last_snapshot < _TRACE_SNAPSHOT_INTERVAL:
        return result
    snapshot = tracemalloc.take_snapshot()
    stats = snapshot.statistics('lineno')[:50]
    current_stats = {
        (stat.traceback[0].filename, stat.traceback[0].lineno):
        (stat.size, stat.count)
        for stat in stats
    }
    growth = []
    for (filename, lineno), (size, count) in current_stats.items():
        previous_size, previous_count = _trace_previous_stats.get(
            (filename, lineno), (0, 0)
        )
        growth.append({
            'file': Path(filename).name,
            'line': lineno,
            'current_mb': round(size / 1048576, 3),
            'delta_mb': round((size - previous_size) / 1048576, 3),
            'count': count,
            'count_delta': count - previous_count,
        })
    growth.sort(key=lambda item: item['delta_mb'], reverse=True)
    result['top_growth'] = growth[:15]
    _trace_previous_stats = current_stats
    _trace_last_snapshot = now
    return result


def system_memory():
    if os.name != 'nt':
        return {}
    value = _MemoryStatus(); value.length = ctypes.sizeof(value)
    if not _kernel32.GlobalMemoryStatusEx(ctypes.byref(value)):
        raise ctypes.WinError()
    return dict(load_percent=value.load, total_mb=value.total // 1048576,
                available_mb=value.available // 1048576)


def start_periodic(interval=30):
    stop = threading.Event()
    def loop():
        while not stop.wait(interval):
            record('periodic')
    thread = threading.Thread(target=loop, name='memory-diagnostics', daemon=True)
    thread.start()
    return stop


def is_browser_process(name: str) -> bool:
    return Path(name).name.lower() in {
        "chrome", "chrome.exe", "chromium", "chromium.exe",
        "firefox", "firefox.exe", "msedge", "msedge.exe",
    }


def _browser_windows(root_pid: int):
    if os.name != 'nt':
        return [], None
    browser_pids = {
        item['pid'] for item in snapshot(root_pid)
        if is_browser_process(item['name'])
    }
    if not browser_pids:
        return [], None
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    handles = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    user32.SetWindowPos.restype = wintypes.BOOL

    @callback_type
    def collect(hwnd, _):
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in browser_pids and user32.IsWindowVisible(hwnd):
            handles.append(hwnd)
        return True

    user32.EnumWindows(collect, 0)
    return handles, user32


def focus_browser_window(root_pid: int) -> bool:
    """Maximize and foreground a visible Chrome window below ``root_pid``.

    SW_RESTORE 会把已经最大化的窗口还原到 Chrome 配置中记住的普通
    尺寸，常见结果就是半屏。每次切换当前店铺时都使用 SW_MAXIMIZE，
    这样远程桌面分辨率变化或窗口被手动缩小后也能恢复为全屏工作区。
    """
    handles, user32 = _browser_windows(root_pid)
    if not handles:
        return False
    hwnd = handles[0]
    user32.ShowWindow(hwnd, 3)  # SW_MAXIMIZE
    brought = bool(user32.BringWindowToTop(hwnd))
    foregrounded = bool(user32.SetForegroundWindow(hwnd))
    return brought or foregrounded


def set_browser_topmost(root_pid: int, enabled: bool) -> bool:
    """Set only this shop's visible Chrome window topmost/non-topmost."""
    handles, user32 = _browser_windows(root_pid)
    if not handles:
        return False
    insert_after = ctypes.c_void_p(-1 if enabled else -2).value
    changed = False
    for hwnd in handles:
        changed = bool(user32.SetWindowPos(
            hwnd, insert_after, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040
        )) or changed
    if enabled:
        focus_browser_window(root_pid)
    return changed


def _cleanup():
    # Only our diagnostic files; never touch profiles, business logs or databases.
    global _last_cleanup
    if time.monotonic() - _last_cleanup < 300:
        return
    _last_cleanup = time.monotonic()
    files = sorted(LOG.parent.glob('memory-*.jsonl'), key=lambda p: p.stat().st_mtime)
    total = sum(p.stat().st_size for p in files)
    for path in files:
        stat = path.stat()
        if stat.st_mtime < time.time() - 14 * 86400 or total > 100 * 1024 * 1024:
            try:
                path.unlink(); total -= stat.st_size
            except OSError:
                pass


def export_bundle(destination):
    """Strict allowlist: structured memory telemetry only, never raw GUI logs."""
    record('diagnostic_export')
    with zipfile.ZipFile(destination, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('README.txt', 'Memory diagnostics only. No profiles, cookies, database or raw business logs.\n')
        for path in LOG.parent.glob('memory-*.jsonl'):
            try:
                lines = []
                with path.open(encoding='utf-8') as source:
                    for line in source:
                        try:
                            item = json.loads(line)
                        except ValueError:
                            continue  # A writer may still be appending its last line.
                        if 'result' in item:
                            item['result'] = 'confirmed' if item['result'] in (
                                'confirmed', '成功', '已提交', '已提交复议') else 'unconfirmed'
                        lines.append(json.dumps(item, ensure_ascii=False))
                archive.writestr('memory/' + path.name, '\n'.join(lines))
            except FileNotFoundError:
                pass


class ChildProcessJob:
    """Close descendants after a worker exits, including orphan browser processes."""
    def __init__(self, process):
        self.handle = None
        if os.name != 'nt':
            return
        class Basic(ctypes.Structure):
            _fields_=[('process_time',ctypes.c_longlong),('job_time',ctypes.c_longlong),
                      ('flags',wintypes.DWORD),('min_ws',ctypes.c_size_t),('max_ws',ctypes.c_size_t),
                      ('active',wintypes.DWORD),('affinity',ctypes.c_size_t),('priority',wintypes.DWORD),('scheduling',wintypes.DWORD)]
        class Extended(ctypes.Structure):
            _fields_=[('basic',Basic),('io',ctypes.c_ulonglong*6),('process_mem',ctypes.c_size_t),
                      ('job_mem',ctypes.c_size_t),('peak_process',ctypes.c_size_t),('peak_job',ctypes.c_size_t)]
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.CreateJobObjectW.argtypes=[ctypes.c_void_p,wintypes.LPCWSTR]
        kernel.CreateJobObjectW.restype=wintypes.HANDLE
        kernel.SetInformationJobObject.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD]
        kernel.AssignProcessToJobObject.argtypes=[wintypes.HANDLE,wintypes.HANDLE]
        kernel.CloseHandle.argtypes=[wintypes.HANDLE]
        self.kernel=kernel
        self.handle=kernel.CreateJobObjectW(None,None)
        info=Extended(); info.basic.flags=0x2000
        if not self.handle or not kernel.SetInformationJobObject(self.handle,9,ctypes.byref(info),ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(self.handle,int(process._handle)):
            self.close()
            raise OSError('无法建立店铺进程回收组，未开始处理订单。')

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle=None


def snapshot(root_pid=None):
    root_pid = root_pid or os.getpid()
    if os.name != 'nt':
        return []
    handle = _kernel32.CreateToolhelp32Snapshot(2,0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError()
    entries = []
    try:
        entry = _ProcessEntry(); entry.size = ctypes.sizeof(entry)
        found = _kernel32.Process32FirstW(handle,ctypes.byref(entry))
        while found:
            entries.append((entry.pid,entry.parent,entry.name,entry.threads))
            found = _kernel32.Process32NextW(handle,ctypes.byref(entry))
    finally:
        _kernel32.CloseHandle(handle)
    selected = {root_pid}
    while True:
        expanded = selected | {pid for pid,parent,_,_ in entries if parent in selected}
        if expanded == selected:
            break
        selected = expanded
    result = []
    for pid,parent,name,threads in entries:
        if pid not in selected:
            continue
        proc = _kernel32.OpenProcess(0x1000 | 0x10,False,pid)
        if not proc:
            continue
        try:
            counters = _ProcessCounters(); counters.cb = ctypes.sizeof(counters)
            if _psapi.GetProcessMemoryInfo(proc,ctypes.byref(counters),counters.cb):
                handles = wintypes.DWORD()
                handles_ok = _kernel32.GetProcessHandleCount(proc, ctypes.byref(handles))
                result.append(dict(pid=pid,parent_pid=parent,name=name,
                    threads=threads,handles=handles.value if handles_ok else None,
                    working_set_mb=round(counters.ws/1048576,2),private_mb=round(counters.private/1048576,2)))
        finally:
            _kernel32.CloseHandle(proc)
    return result


def record(event, **metadata):
    try:
        processes = snapshot()
        with _trace_lock:
            heap = python_heap_summary(
                force=event in ('gui_started', 'gui_shutdown', 'diagnostic_export')
            )
        data = dict(time=datetime.now(timezone.utc).isoformat(),pid=os.getpid(),event=event,
                    run_id=RUN_ID,build_id=_build,python_version=sys.version.split()[0],
                    uptime_seconds=round(time.monotonic()-_started,1),
                    system=system_memory(),processes=processes,**metadata)
        if heap is not None:
            data['python_heap'] = heap
        with _lock:
            data['context'] = dict(_context)
            data['runtime_metrics'] = dict(_runtime_metrics)
            LOG.parent.mkdir(parents=True,exist_ok=True)
            _cleanup()
            # Each producing process owns its own rotating file.
            path = LOG.with_name(f'memory-{RUN_ID}-{os.getpid()}.jsonl')
            if path.exists() and path.stat().st_size > 5*1024*1024:
                path.replace(path.with_suffix('.previous.jsonl'))
            with path.open('a',encoding='utf-8') as out:
                out.write(json.dumps(data,ensure_ascii=False)+'\n')
        return processes
    except Exception as error:
        # Do not copy exception text: it may contain user paths or business data.
        print('Memory diagnostic failure: ' + type(error).__name__, file=sys.stderr)
        return []
