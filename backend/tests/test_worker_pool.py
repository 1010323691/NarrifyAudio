"""Real subprocess checks for the dedicated pool supervisor."""
import json
import os
import sys
import threading
import time

from backend import worker_pool


def test_supervisor_preserves_lane_counts_when_one_child_crashes(monkeypatch,tmp_path):
    script=r'''
import json,os,signal,sys,time
from pathlib import Path
root,lane,slot=Path(sys.argv[1]),sys.argv[2],sys.argv[3]
p=root/(lane+'-'+slot+'.json')
old=json.loads(p.read_text()) if p.exists() else []
p.write_text(json.dumps(old+[os.getpid()]))
if lane=='mechanical' and slot=='0' and len(old)==0:
    raise SystemExit(42)
for signum in (signal.SIGINT,signal.SIGTERM):
    signal.signal(signum,lambda *_:sys.exit(0))
while True:time.sleep(.1)
'''
    def command(lane,slot,interval,pool_id):
        return [sys.executable,'-c',script,str(tmp_path),lane,str(slot)]
    monkeypatch.setattr(worker_pool,'worker_command',command)
    stop=threading.Event()
    errors=[]
    def run():
        try:
            worker_pool.supervise({'mechanical':4,'model':4},interval=.1,stop=stop,pool_id='test')
        except BaseException as exc:
            errors.append(exc)
    thread=threading.Thread(target=run)
    thread.start()
    try:
        deadline=time.monotonic()+10
        while time.monotonic()<deadline:
            files=list(tmp_path.glob('*.json'))
            try:
                ready=len(files)==8 and len(json.loads((tmp_path/'mechanical-0.json').read_text()))==2
            except (FileNotFoundError,json.JSONDecodeError):
                ready=False
            if ready:break
            time.sleep(.05)
        assert ready,'supervisor did not restore the failed dedicated slot'
        records={p.stem:json.loads(p.read_text()) for p in files}
        assert all(len(pids)==(2 if key=='mechanical-0' else 1) for key,pids in records.items())
        assert {key.split('-')[0] for key in records}=={'mechanical','model'}
    finally:
        stop.set();thread.join(timeout=20)
    assert not thread.is_alive() and not errors
    if os.name=='posix':
        for pids in records.values():
            for pid in pids:
                try:
                    os.kill(pid,0)
                except ProcessLookupError:
                    continue
                raise AssertionError(f'child left running: {pid}')


def test_pool_commands_always_select_a_dedicated_lane():
    for lane in ('mechanical','model'):
        command=worker_pool.worker_command(lane,0,.1,'pool')
        assert command[command.index('--task-lane')+1]==lane
