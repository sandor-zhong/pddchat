"""Stable protocol broker: restart the entire order worker only after safe results."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import hashlib

from memory_monitor import record, ChildProcessJob, configure, start_periodic, is_browser_process


def broker(arguments, input_stream=None, output_stream=None, command=None):
    source, sink = input_stream or sys.stdin, output_stream or sys.stdout
    events = queue.Queue()
    stopped = threading.Event()
    every = max(1,int(os.getenv('PDDCHAT_RECYCLE_ORDERS','25')))
    python_limit = max(1,int(os.getenv('PDDCHAT_PYTHON_LIMIT_MB','768')))
    browser_limit = max(1,int(os.getenv('PDDCHAT_BROWSER_LIMIT_MB','1536')))
    child = None
    job = None
    generation = 0
    checkpoint = None
    recycling = False
    restoring = False
    pending_next = False
    completed = 0
    order_started = None
    shop = arguments[arguments.index('--shop')+1] if '--shop' in arguments else ''
    configure(role='broker', shop_id=hashlib.sha256(shop.encode()).hexdigest()[:16],
              mode='aftersale' if any('aftersale' in a for a in arguments) else
                   'reconsideration' if any('reconsider' in a for a in arguments) else 'appeal',
              recycle_orders=every, python_limit_mb=python_limit, browser_limit_mb=browser_limit)
    def output(msg):
        sink.write(json.dumps(msg,ensure_ascii=False)+'\n'); sink.flush()
    def read_commands():
        try:
            for line in source:
                msg = json.loads(line)
                if msg.get('cmd') == 'stop': stopped.set()
                events.put(('command',0,msg))
        finally:
            stopped.set(); events.put(('command',0,{'cmd':'stop'}))
    def send(msg):
        nonlocal order_started
        if child is not None and child.poll() is None and not child.stdin.closed:
            if msg.get('cmd') == 'next':
                order_started=time.monotonic()
                configure(phase='processing_order')
                record('order_started',worker_pid=child.pid,order_sequence=completed+1)
            child.stdin.write(json.dumps(msg,ensure_ascii=False)+'\n'); child.stdin.flush()
    def launch():
        nonlocal child,generation,job
        generation += 1
        child = subprocess.Popen(command or [sys.executable,str(Path(__file__).with_name('main.py')),*arguments],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
            encoding='utf-8',errors='replace',bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            job=ChildProcessJob(child)
        except OSError:
            # No init has been sent; there is no in-flight order to interrupt.
            child.terminate(); child.wait()
            for pipe in (child.stdin,child.stdout,child.stderr): pipe.close()
            raise
        proc, gen = child,generation
        def read_output():
            try:
                for line in proc.stdout:
                    try: msg=json.loads(line)
                    except ValueError: continue
                    events.put(('message',gen,msg))
            finally:
                code=proc.wait()
                proc.stdout.close()
                events.put(('exit',gen,code))
        def read_errors():
            try:
                for line in proc.stderr:
                    sys.stderr.write(line); sys.stderr.flush()
            finally: proc.stderr.close()
        threading.Thread(target=read_output,daemon=True).start()
        threading.Thread(target=read_errors,daemon=True).start()
        record('worker_started',worker_pid=proc.pid,generation=gen)
    def resume():
        nonlocal restoring,recycling,completed
        launch(); restoring=True; recycling=False; completed=0
        output({'type':'memory_status','message':f'新店铺进程 {child.pid} 已启动，正在恢复剩余队列'})
        send({'cmd':'init','resume':checkpoint})
    launch()
    sampling_stop = start_periodic()
    threading.Thread(target=read_commands,daemon=True).start()
    try:
        while True:
            kind,gen,msg=events.get()
            if kind=='command':
                if stopped.is_set() and msg.get('cmd') != 'stop':
                    continue
                if msg.get('cmd')=='stop':
                    if child is None: break
                    send({'cmd':'stop'}); child.stdin.close()
                elif msg.get('cmd')=='next' and (recycling or restoring or child is None):
                    pending_next=True
                    if child is None and not stopped.is_set(): resume()
                else: send(msg)
            elif gen != generation:
                continue
            elif kind=='exit':
                if child.stdin and not child.stdin.closed: child.stdin.close()
                if job is not None:
                    job.close(); job=None
                child=None
                configure(phase='worker_exited')
                record('worker_exited',exit_code=msg,generation=gen)
                if stopped.is_set(): break
                if recycling and msg==0:
                    if pending_next: resume()
                else:
                    return msg or 0
            else:
                state=msg.pop('_resume',None)
                if msg.get('type') in ('ready','reconsider_ready','aftersale_ready'):
                    configure(phase='ready')
                    record('worker_ready',worker_pid=child.pid,generation=gen)
                if restoring and msg.get('type') in ('ready','reconsider_ready','aftersale_ready'):
                    restoring=False
                    if pending_next and not stopped.is_set():
                        pending_next=False; send({'cmd':'next'})
                    continue
                if msg.get('type') in ('result','reconsider_order_result','aftersale_order_result'):
                    completed+=1
                    configure(phase='order_finished')
                    samples=record('order_finished',worker_pid=child.pid,completed=completed,
                                   result='confirmed' if msg.get('result') in ('已提交','已提交复议','成功') else 'unconfirmed',
                                   duration_seconds=round(time.monotonic()-order_started,2) if order_started else None,
                                   pages=msg.get('page_count'))
                    py_mb=sum(p['private_mb'] for p in samples if p['pid']!=os.getpid() and 'python' in p['name'].lower())
                    browser_mb=sum(p['private_mb'] for p in samples if is_browser_process(p['name']))
                    safe=msg.get('result') in ('已提交','已提交复议','成功')
                    if state and safe and not stopped.is_set() and (completed>=every or py_mb>=python_limit or browser_mb>=browser_limit):
                        checkpoint=state; recycling=True
                        record('recycle_requested',worker_pid=child.pid,completed=completed,python_mb=py_mb,browser_mb=browser_mb)
                        output({'type':'memory_status','message':f'本单已确认，回收进程 {child.pid}；已处理 {completed} 单，Python {py_mb:.0f} MB，浏览器 {browser_mb:.0f} MB'})
                        send({'cmd':'stop'}); child.stdin.close()
                output(msg)
    finally:
        sampling_stop.set()
        if child is not None:
            try:
                send({'cmd':'stop'})
                if not child.stdin.closed: child.stdin.close()
                child.wait(timeout=60)
            except (OSError,ValueError,subprocess.TimeoutExpired):
                # Unexpected broker shutdown: allow graceful cleanup first.
                # Closing the job below also removes any remaining descendants.
                record('shutdown_wait_incomplete',worker_pid=child.pid)
        if job is not None:
            job.close()
    return 0


if __name__=='__main__':
    for stream in (sys.stdin,sys.stdout,sys.stderr): stream.reconfigure(encoding='utf-8')
    sys.exit(broker(sys.argv[1:]))
