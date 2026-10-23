"""Local protocol fixture: never launches browsers or accesses real orders."""
import json
import os
import sys

for stream in (sys.stdin,sys.stdout,sys.stderr):
    stream.reconfigure(encoding='utf-8')
cursor=0
for line in sys.stdin:
    command=json.loads(line)
    if command['cmd']=='stop': break
    if command['cmd']=='init':
        cursor=command.get('resume',{}).get('cursor',0)
        print(json.dumps({'type':'ready','queue_size':6-cursor}),flush=True)
    elif command['cmd']=='next':
        cursor+=1
        print(json.dumps({'type':'result','order_no':str(cursor),'pid':os.getpid(),
                          'result':os.getenv('TEST_ORDER_RESULT','已提交'),
                          '_resume':{'cursor':cursor} if cursor<6 else None}),flush=True)
