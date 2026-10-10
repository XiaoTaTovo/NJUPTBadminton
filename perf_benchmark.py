"""Offline, alternating pre/post benchmark. Synthetic identity, real Requests preparation, no network."""
import argparse
import contextlib, io
from contextlib import ExitStack
import importlib.util
import json
import math
import statistics
import tempfile
import time
from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import patch
import requests
from requests.adapters import BaseAdapter
import api, booking, daily, scheduling
from task_runtime import TaskJournal, WaitGuard

SLOT=dict(id='synthetic-slot',name='仙林体育馆3F羽毛球馆6号场地',date='2099-01-01',start='19:00',end='20:00',available=True)
PLAN=dict(version=2,targets=[dict(id='g',date=SLOT['date'],start=SLOT['start'],end=SLOT['end'],quantity=1,courts=[SLOT['name']])],max_orders=1,max_attempts_per_target=1,target_order='round_robin')

def load(name,path):
    spec=importlib.util.spec_from_file_location(name,path);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module

def summary(values):
    s=sorted(values)
    return dict(n=len(s),median_ms=round(statistics.median(s),4),p95_ms=round(s[math.ceil(len(s)*.95)-1],4),max_ms=round(s[-1],4))

class Adapter(BaseAdapter):
    def __init__(self):self.marker=None;self.entry=None;self.payload_valid=False
    def send(self,request,**kw):
        self.entry=time.perf_counter_ns()
        from urllib.parse import parse_qs
        import hashlib
        body=parse_qs(request.body)
        ts=body['timestamp'][0]
        expected=hashlib.sha256(f"{SLOT['id']}|synthetic|{SLOT['date']}|{ts}|4pGmY6s9zX".encode()).hexdigest()
        self.payload_valid=body['fingerprint'][0]==expected and int(request.headers['Content-Length'])==len(request.body)
        response=requests.Response();response.status_code=200;response._content=json.dumps({'success':True,'data':{'order':{'orderId':'synthetic'},'detail':{'orderId':'synthetic','stadiumName':SLOT['name'],'startTime':SLOT['date']+' '+SLOT['start']+':00','endTime':SLOT['date']+' '+SLOT['end']+':00'}}}).encode()
        response.request=request;response.headers={'Date':'Sat, 10 Oct 2026 04:00:00 GMT'}
        return response
    def close(self):pass


def bench(samples=100):
    baseline=Path('private/performance-baseline-20261010')
    if not (baseline/'api.py').exists():raise RuntimeError('本机优化前源码快照缺失，不能伪造前后对比。')
    old_api=load('baseline_api',baseline/'api.py');old_booking=load('baseline_booking',baseline/'booking.py');old_daily=load('baseline_daily',baseline/'daily.py')
    results={'before':[],'after':[]};persist={'before':[],'after':[]}
    with tempfile.TemporaryDirectory() as td:
        root=Path(td);(root/'private').mkdir()
        for i in range(samples):
            for name in (('before','after') if i%2==0 else ('after','before')):
                am,bm=(old_api,old_booking) if name=='before' else (api,booking)
                am.ROOT=root
                work=root/f'{name}-{i}';work.mkdir()
                c=object.__new__(am.Client);c.claims={'studentId':'synthetic'};c.last_request=-float('inf');c.clock_time=time.time;c.timings=[]
                c.http=requests.Session();c.http.trust_env=False;c.http.proxies={};adapter=Adapter();c.http.mount('https://',adapter)
                # Execute the ACTUAL old/new daily gate and engine. Only clock/network/ACL are substituted.
                dm=old_daily if name=='before' else daily
                private=work/'private';private.mkdir()
                fire=datetime(2099,1,1,12,tzinfo=api.CN)
                class Clock:
                    offset=0.;info={}
                    current=fire-timedelta(seconds=100)
                    def now(self):return self.current
                    def timestamp(self):return self.current.timestamp()
                    def age(self):return 0
                    def check_gateway(self,event):pass
                clock=Clock();c.claims['exp']=fire.timestamp()+3600;c.slots=lambda date:[dict(SLOT)]
                config={'windows':[dict(start='19:00',end='20:00',quantity=1)],'priority':{'tier_order':[0,1,2,3],'preferred_3f_court':6},
                        'focus_target':'19:00-20:00','focus_court_number':6,'max_attempts_per_target':1,'strategy':'coverage'}
                def wait(target,*args,**kwargs):clock.current=target
                def release(*args,**kwargs):
                    clock.current=fire
                    adapter.marker=time.perf_counter_ns()
                    # Scheduler return time is the comparable origin; wake-up accuracy measured separately.
                    return {'scheduled_at':fire.isoformat(),'fired_epoch':fire.timestamp(),'fired_perf_ns':adapter.marker,'trigger_lateness_ms':0}
                class Wake:
                    def acquire(self):return True
                    def close(self):pass
                def engine(p,cl,**kw):return bm.run(p,cl,private=private,notify=lambda _:None,**kw)
                with ExitStack() as stack,contextlib.redirect_stdout(io.StringIO()):
                    stack.enter_context(patch.object(dm,'ROOT',work))
                    stack.enter_context(patch.object(am,'ROOT',work))
                    stack.enter_context(patch.object(dm,'load_settings',return_value=config))
                    stack.enter_context(patch.object(dm,'now_cn',side_effect=clock.now))
                    stack.enter_context(patch.object(dm,'Client',side_effect=lambda **kw:c))
                    stack.enter_context(patch.object(dm,'refresh_clock',return_value=clock))
                    stack.enter_context(patch.object(dm,'wait_until',side_effect=wait))
                    stack.enter_context(patch.object(dm,'wait_for_start',side_effect=release))
                    stack.enter_context(patch.object(dm,'WakeLease',Wake))
                    stack.enter_context(patch.object(dm,'run',side_effect=engine))
                    r=dm.execute(confirm=lambda _: '1')
                assert r is not None, 'daily flow stopped before benchmark submission'
                assert adapter.payload_valid and len(r['attempts'])==1
                results[name].append((adapter.entry-adapter.marker)/1e6)
                if 'intent_persist_ms' in r['attempts'][0]:persist[name].append(r['attempts'][0]['intent_persist_ms'])
                c.close()
    # Actual local scheduler wake-up experiment; not server clock accuracy.
    wakes={'sleep_only':[],'bounded_spin_0_5ms':[]}
    for i in range(samples):
        for label,spin in [('sleep_only',0),('bounded_spin_0_5ms',.5)]:
            fire=datetime.now(api.CN)+timedelta(milliseconds=12)
            info=scheduling.wait_for_start(fire,None,spin_ms=spin,defer_format=True)
            wakes[label].append(info['trigger_lateness_ms'])
    return {'benchmark':'offline_actual_daily_gate_to_requests_adapter_entry','samples_each':samples,
            'limitations':'Synthetic response, no DNS/TCP/TLS/server contention. Gate-to-adapter is not wire or server receipt time.',
            'gate_to_adapter':{k:summary(v) for k,v in results.items()},
            'intent_persist':{k:summary(v) for k,v in persist.items() if v},
            'local_wake_lateness':{k:summary(v) for k,v in wakes.items()}}

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--samples',type=int,default=100);parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not 5<=args.samples<=300:parser.error('samples must be 5..300')
    result=bench(args.samples);text=json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(text,encoding='utf-8')
    print(text)
