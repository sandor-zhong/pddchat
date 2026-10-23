import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import unittest


class BrokerTests(unittest.TestCase):
    def run_sequence(self,result='已提交',count=6):
        fixture=Path(__file__).parent/'fixtures'/'memory_worker_child.py'
        code=('import memory_worker; memory_worker.record=lambda *a,**k: []; '
              f'raise SystemExit(memory_worker.broker([],command=[{sys.executable!r},{str(fixture)!r}]))')
        proc=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE,text=True,encoding='utf-8',
                              env=dict(os.environ,PYTHONIOENCODING='utf-8',PDDCHAT_RECYCLE_ORDERS='2',TEST_ORDER_RESULT=result))
        messages=queue.Queue()
        def read():
            for line in proc.stdout:
                message=json.loads(line)
                if message.get('type') != 'memory_status':
                    messages.put(message)
        thread=threading.Thread(target=read,daemon=True); thread.start()
        def send(msg):
            proc.stdin.write(json.dumps(msg)+'\n'); proc.stdin.flush()
        try:
            send({'cmd':'init'})
            self.assertEqual(messages.get(timeout=10)['type'],'ready')
            results=[]
            for _ in range(count):
                send({'cmd':'next'})
                msg=messages.get(timeout=10)
                self.assertEqual(msg['type'],'result')
                self.assertNotIn('_resume',msg)
                results.append(msg)
            send({'cmd':'stop'}); proc.stdin.close()
            proc.wait(timeout=10)
            self.assertEqual(proc.returncode,0,proc.stderr.read())
            self.assertEqual([r['order_no'] for r in results],[str(i+1) for i in range(count)])
            return results
        finally:
            if proc.poll() is None:
                proc.kill(); proc.wait()
            if not proc.stdin.closed: proc.stdin.close()
            thread.join(timeout=2)
            proc.stdout.close(); proc.stderr.close()

    def test_restarts_worker_and_preserves_exact_order_cursor(self):
        results=self.run_sequence()
        self.assertEqual(len({r['pid'] for r in results}),3)

    def test_pending_confirmation_never_triggers_restart(self):
        results=self.run_sequence('复议结果待确认')
        self.assertEqual(len({r['pid'] for r in results}),1)

    def test_stop_at_recycle_boundary_never_starts_next_order(self):
        results=self.run_sequence(count=2)
        self.assertEqual(len(results),2)
