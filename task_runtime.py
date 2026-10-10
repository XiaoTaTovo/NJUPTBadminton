"""Private task lifecycle, guarded waits, and read-only diagnostics (no API calls)."""
import ctypes
import json
import os
import time
import uuid
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import psutil
from api import ROOT, CN, now_cn, SafeError
from booking import atomic
from safe_errors import MESSAGES, safe_details

PHASES={'credentials','initial_calibration','refresh_wait','refresh_calibration','prefetch_wait',
        'prepare_slots','ready_wait','booking_engine'}
TERMINAL={'complete','partial_or_exhausted','stopped','unknown','cancelled','blocked'}


def read_json(path):
    try: return json.loads(path.read_text(encoding='utf-8'))
    except (OSError,ValueError): return None


def process_matches(state, root=ROOT):
    """PID alone is insufficient: validate creation time and exact project script."""
    from launcher import owned_kind
    try:
        p=psutil.Process(state['pid'])
        return abs(p.create_time()-state['process_created'])<.01 and owned_kind(p.cmdline(),p.cwd(),root)=='app'
    except (psutil.Error,KeyError,TypeError,ValueError): return False


@contextmanager
def task_lock(private, name="task.lock"):
    import msvcrt
    private.mkdir(exist_ok=True)
    with (private/name).open('a+b') as f:
        f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
        try: msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError: raise SafeError(code='TASK_ALREADY_RUNNING') from None
        try: yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


class TaskJournal:
    def __init__(self, private, mode, fire, windows):
        self.private=Path(private)
        self.task_id=uuid.uuid4().hex
        self.clock=None
        self.started=time.monotonic(); self.phase_started=self.started
        self.sequence=0; self.last_heartbeat=self.started; self.pending=[]
        self.data={'task_id':self.task_id,'pid':os.getpid(),'process_created':psutil.Process().create_time(),
                   'mode':mode,'scheduled_at':fire.isoformat(),'created_at':now_cn().isoformat(),
                   'phase':'credentials','state':'running','next_action':'credentials',
                   'windows':[{k:w[k] for k in ('start','end','quantity')} for w in windows],
                   'post_count':0,'slots_get_count':0,'types_get_count':0,'telemetry_incomplete':False}
        previous=read_json(self.private/'current-task.json')
        if previous:
            if process_matches(previous):
                if previous.get('state') not in TERMINAL: raise SafeError(code='TASK_ALREADY_RUNNING')
            elif previous.get('state') not in TERMINAL and previous.get('phase') in ('ready_wait','booking_engine'):
                raise SafeError(code='PREVIOUS_TASK_UNKNOWN')
            if previous.get('state')=='unknown': raise SafeError(code='PREVIOUS_TASK_UNKNOWN')
        self.event('started')

    def persist(self):
        self.data['heartbeat_at']=now_cn().isoformat()
        atomic(self.private/f'task-{self.task_id}.json',self.data)
        atomic(self.private/'current-task.json',self.data)

    def event(self, event_type, details=None, *, defer=False):
        self.sequence+=1
        mono=time.monotonic()
        event={'task_id':self.task_id,'sequence':self.sequence,'phase':self.data['phase'],
               'event':event_type,'local_time':now_cn().isoformat(),
               'reference_time':self.clock.now().isoformat() if self.clock else None,
               'elapsed_ms':round((mono-self.started)*1000,3),
               'phase_elapsed_ms':round((mono-self.phase_started)*1000,3),
               'state':self.data['state'],'next_action':self.data['next_action'],
               'details':safe_details(details)}
        self.pending.append(event)
        if defer: return
        try:
            with (self.private/f'events-{self.task_id}.jsonl').open('a',encoding='utf-8') as f:
                for item in self.pending: f.write(json.dumps(item,ensure_ascii=False)+'\n')
                f.flush(); os.fsync(f.fileno())
            self.pending.clear()
            self.persist()
        except OSError: raise SafeError(code='LOG_WRITE_FAILED') from None

    def transition(self, phase, *, defer=False):
        assert phase in PHASES
        self.event('completed',defer=defer)
        self.data.update(phase=phase,next_action=phase)
        self.phase_started=time.monotonic()
        self.event('started',defer=defer)
        if not defer: set_title('预约任务：'+phase)

    def heartbeat(self, fire):
        if self.clock and (fire-self.clock.now()).total_seconds()<=10: return
        if time.monotonic()-self.last_heartbeat>=30:
            self.event('heartbeat'); self.last_heartbeat=time.monotonic()

    def finish(self, state, *, exc=None, client=None, result=None):
        self.data['state']=state
        self.data['next_action']='manual_order_check' if state=='unknown' else 'none'
        self.data['ended_at']=now_cn().isoformat()
        if client is not None:
            events=getattr(client,'timings',[])
            for endpoint,key in [('booking','post_count'),('slots','slots_get_count'),('types','types_get_count')]:
                self.data[key]=sum(e.get('endpoint')==endpoint for e in events)
            self.data['telemetry_incomplete']=bool(getattr(client,'telemetry_incomplete',False))
        if result:
            self.data.update(run_id=result.get('run_id'),success_count=sum(a['state']=='success' for a in result.get('attempts',[])),
                             credited_count=len(result.get('credited_slots',[])),
                             unknown_count=sum(a['state'] in ('unknown','submitting') for a in result.get('attempts',[])))
        if exc:
            code=exc.code if isinstance(exc,SafeError) else 'USER_CANCELLED' if isinstance(exc,(KeyboardInterrupt,EOFError)) else 'INTERNAL_ERROR'
            self.data.update(error_code=code,safe_message=MESSAGES[code],error_details=safe_details(getattr(exc,'details',{})))
            # Never include locals, source lines, raw exception messages or external filenames.
            frames=[]
            for f in traceback.extract_tb(exc.__traceback__):
                path=Path(f.filename)
                if path.parent.resolve()==ROOT.resolve():frames.append({'file':path.name,'function':f.name,'line':f.lineno})
            self.data['error_location']=frames
        self.event('failed' if exc or state in ('unknown','blocked') else 'completed',self.data.get('error_details'))


def set_title(text):
    try: ctypes.windll.kernel32.SetConsoleTitleW(text)
    except (AttributeError,OSError): pass


class WakeLease:
    def acquire(self):
        try: self.active=bool(ctypes.windll.kernel32.SetThreadExecutionState(0x80000001))
        except (AttributeError,OSError): self.active=False
        return self.active
    def close(self):
        if getattr(self,'active',False):
            try: ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
            except (AttributeError,OSError): pass
            finally: self.active=False


class WaitGuard:
    def __init__(self, *, strict, observer=None, monotonic=None, wall=None):
        self.mono=monotonic or time.monotonic; self.wall=wall or time.time
        self.last_mono=self.mono(); self.last_wall=self.wall()
        self.strict=strict; self.observer=observer or (lambda *a:None)
    def check(self, *, check_gap=True):
        m=self.mono(); w=self.wall()
        gap=m-self.last_mono; jump=abs((w-self.last_wall)-gap)
        self.last_mono=m; self.last_wall=w
        if (check_gap and gap>5) or jump>2:
            details={'gap_seconds':gap,'jump_seconds':jump}
            if self.strict: raise SafeError(code='WAIT_DISCONTINUITY',details=details)
            self.observer('wait_discontinuity_observed',details)


def wait_until(target, clock, journal, fire, *, strict=True, sleep=None, monotonic=None, wall=None, guard=None):
    sleep=sleep or time.sleep
    guard=guard or WaitGuard(strict=strict,observer=journal.event,monotonic=monotonic,wall=wall)
    while (remaining:=(target-clock.now()).total_seconds())>0:
        sleep(min(1,remaining)); guard.check(); journal.heartbeat(fire)
    guard.check()


def diagnose(private=None, root=ROOT):
    """Read only: no directory creation, ACL changes, locks, network, or subprocesses."""
    private=Path(private or root/'private')
    state=read_json(private/'current-task.json')
    if not state:
        print('没有新格式任务记录；不能据此认定预约成功或失败。')
        failures=sorted(private.glob('launch-failure-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
        if failures:
            old=read_json(failures[0]) or {}
            phase=old.get('phase')
            print('历史失败阶段：',phase if phase in PHASES else '未分类', '；旧日志缺少具体原因，不能补推。')
        return None
    alive=process_matches(state,root)
    try: stale=(now_cn()-datetime.fromisoformat(state['heartbeat_at'])).total_seconds()>90
    except (KeyError,ValueError,TypeError): stale=True
    status=state.get('state')
    print('任务：',state.get('task_id'),'；状态：',status if status in TERMINAL or status=='running' else '无法确认')
    print('阶段：',state.get('phase') if state.get('phase') in PHASES else '未知')
    print('进程身份匹配：',alive,'；心跳陈旧：',stale)
    print('最后心跳：',state.get('heartbeat_at'),'；下一动作：',state.get('next_action') if state.get('next_action') in PHASES|{'none','manual_order_check'} else '未知')
    if status not in TERMINAL and (stale or not alive): print('状态陈旧／无法确认；禁止把它当成仍在等待或已经成功。')
    if state.get('error_code') in MESSAGES: print('停止原因：',state['error_code'],MESSAGES[state['error_code']])
    counts={'slots':0,'booking':0,'types':0}; incomplete=state.get('telemetry_incomplete',False)
    try:
        with (private/'request-events.jsonl').open(encoding='utf-8') as f:
            for line in f:
                try: e=json.loads(line)
                except ValueError: incomplete=True;continue
                if e.get('task_id')==state['task_id'] and e.get('endpoint') in counts:counts[e['endpoint']]+=1
    except FileNotFoundError: pass
    except OSError: incomplete=True
    print('记录到的场次GET：',counts['slots'],'；类型GET：',counts['types'],'；预约POST：',counts['booking'])
    successes=credited=unknown=0
    for path in private.glob('run-*.json'):
        run=read_json(path)
        if not run:
            incomplete=True; continue
        if run.get('context',{}).get('task_id')!=state['task_id']: continue
        successes+=sum(a.get('state')=='success' for a in run.get('attempts',[]))
        credited+=len(run.get('credited_slots',[]))
        unknown+=sum(a.get('state') in ('unknown','submitting') for a in run.get('attempts',[]))
    print('本轮新增成功：',successes,'；历史计入：',credited,'；未知/未完成意图：',unknown)
    if unknown: print('存在未确认订单意图；不应重新提交，请先人工核对。')
    if not counts['booking']: print('没有记录到预约请求；日志缺失或进程异常时，不证明服务器绝对未收到。')
    if incomplete: print('诊断不完整，请结合本地订单记录及小程序人工核对。')
    return state


def show_last_status(private):
    state=read_json(Path(private)/'current-task.json')
    if state and state.get('state') in TERMINAL:
        print('【最近任务】',state['state'],MESSAGES.get(state.get('error_code'),'结果请通过高级菜单11查看；支付状态以小程序为准。'))


if __name__=='__main__':
    import argparse
    parser=argparse.ArgumentParser(description='只读任务诊断，不调用预约接口')
    parser.add_argument('command',choices=['diagnose'])
    parser.parse_args()
    diagnose()
