"""Offline full daily-flow benchmark; synthetic success responses, no network.

Cold mode starts a fresh interpreter per sample. Waiting before calibration and
prefetch is time-compressed; the final idle uses the actual scheduler. This is
not a measurement of server latency or proof of multi-hour idle performance.
"""
import argparse
import contextlib
import copy
import importlib.util
import hashlib
import io
import json
import math
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import requests
from requests.adapters import BaseAdapter
import api
import booking
import daily
import scheduling

ROOT = Path(__file__).resolve().parent
BASELINE = 'bfc875f'


def load_baseline(folder):
    modules=[]
    for name in ('api','booking','daily'):
        result=subprocess.run(['git','show',f'{BASELINE}:{name}.py'],cwd=ROOT,
                              capture_output=True,check=True,timeout=10)
        path=folder/f'{name}.py';path.write_bytes(result.stdout)
        spec=importlib.util.spec_from_file_location('oct10_'+name,path)
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        modules.append(module)
    return modules


def sample(variant='after', idle_seconds=.2):
    with tempfile.TemporaryDirectory(prefix='booking-offline-') as td:
        root=Path(td);private=root/'private';private.mkdir()
        am,bm,dm=load_baseline(root) if variant=='before' else (api,booking,daily)
        fire=datetime(2099,1,1,12,tzinfo=api.CN)
        slots=[dict(id=f'synthetic-{h}',name='仙林体育馆3F羽毛球馆6号场地',
                    date='2099-01-01',start=f'{h}:00',end=f'{h+1}:00',available=False)
               for h in (20,21)]
        by_id={s['id']:s for s in slots}
        class Clock:
            offset=0.;info={}
            def set(self,date):
                self.anchor=time.monotonic();self.epoch=date.timestamp()
            def timestamp(self):return self.epoch+time.monotonic()-self.anchor
            def now(self):return datetime.fromtimestamp(self.timestamp(),api.CN)
            def age(self):return time.monotonic()-self.anchor
            def check_gateway(self,event):pass
        clock=Clock();clock.set(fire-timedelta(hours=1))
        class Adapter(BaseAdapter):
            def __init__(self):self.starts=[];self.ends=[]
            def send(self,request,**kwargs):
                assert kwargs['verify'] is True
                assert request.url.startswith(api.BASE+'/venue/user/booking/')
                self.starts.append(time.perf_counter_ns())
                trace=getattr(request,'_booking_trace',None)
                if trace is not None:trace['transport_enter_ns']=self.starts[-1]
                slot=by_id[request.url.rsplit('/',1)[-1]]
                r=requests.Response();r.status_code=200;r.headers={};r.request=request
                r._content=json.dumps({'success':True,'data':{'order':{'orderId':'synthetic'},
                    'detail':{'orderId':'synthetic','stadiumName':slot['name'],
                              'startTime':slot['date']+' '+slot['start']+':00',
                              'endTime':slot['date']+' '+slot['end']+':00'}}}).encode()
                self.ends.append(time.perf_counter_ns())
                if trace is not None:trace['response_headers_ns']=self.ends[-1]
                return r
            def close(self):pass
        c=object.__new__(am.Client)
        c.claims={'studentId':'synthetic','exp':fire.timestamp()+3600}
        c.http=requests.Session();c.http.trust_env=False;c.http.proxies={}
        adapter=Adapter();c.http.mount('https://',adapter)
        c.last_request=-float('inf');c.min_request_interval=1.
        c.clock_time=clock.timestamp;c.timings=[];c.telemetry_incomplete=False
        c.slots=lambda _:copy.deepcopy(slots)
        config=copy.deepcopy(daily.DEFAULT)
        config['max_attempts_per_target']=1
        class Wake:
            def acquire(self):return True
            def close(self):pass
        release={}
        def gate(fire,warmup,**kw):
            clock.set(fire-timedelta(seconds=idle_seconds))
            info=scheduling.wait_for_start(fire,warmup,**kw)
            release['ns']=info.get('fired_perf_ns',time.perf_counter_ns())
            return info
        def engine(p,client,**kw):
            return bm.run(p,client,private=private,notify=lambda _:None,**kw)
        with contextlib.ExitStack() as stack,contextlib.redirect_stdout(io.StringIO()):
            for module in (am,dm):stack.enter_context(patch.object(module,'ROOT',root))
            stack.enter_context(patch.object(dm,'Client',side_effect=lambda **_:c))
            stack.enter_context(patch.object(dm,'load_settings',return_value=config))
            stack.enter_context(patch.object(dm,'now_cn',side_effect=clock.now))
            stack.enter_context(patch.object(dm,'calibrate',return_value=clock))
            stack.enter_context(patch.object(dm,'refresh_clock',return_value=clock))
            stack.enter_context(patch.object(dm,'wait_until',side_effect=lambda target,*a,**k:clock.set(target)))
            stack.enter_context(patch.object(dm,'wait_for_start',side_effect=gate))
            stack.enter_context(patch.object(dm,'WakeLease',Wake))
            stack.enter_context(patch.object(dm,'run',side_effect=engine))
            # Network adapters cannot escape this in-memory adapter even if code regresses.
            with patch('socket.socket.connect',side_effect=AssertionError('offline benchmark network blocked')):
                result=dm.execute(confirm=lambda _:'1')
        assert result and result['state']=='complete' and len(adapter.starts)==2
        attempts=result['attempts'];first=attempts[0];second=attempts[1]
        reference_start=first['request_timing'].get('http_call_started_ns',adapter.starts[0])
        earliest=max(reference_start+1_000_000_000,adapter.ends[0])
        return dict(variant=variant, real_idle_seconds=idle_seconds,
                    source_hashes={m.__name__:hashlib.sha256(Path(m.__file__).read_bytes()).hexdigest() for m in (am,bm,dm)},
                    first_gate_to_adapter_ms=(adapter.starts[0]-release['ns'])/1e6,
                    first_to_second_ms=(adapter.starts[1]-adapter.starts[0])/1e6,
                    second_excess_over_interval_ms=max(0,(adapter.starts[1]-earliest)/1e6),
                    classification_to_candidate_ms=(second['candidate_ready_mono_ns']-first['classification_completed_mono_ns'])/1e6,
                    first_request=first['request_timing'],
                    result_persist_ms=first.get('result_io',{}).get('total_ms'),
                    success_count=2, network_calls=0)


def summarize(samples):
    out={}
    for key in ('first_gate_to_adapter_ms','classification_to_candidate_ms','first_to_second_ms','second_excess_over_interval_ms'):
        values=sorted(s[key] for s in samples)
        out[key]=dict(median=statistics.median(values),p95=values[math.ceil(.95*len(values))-1],maximum=max(values))
    return out


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--samples',type=int,default=5)
    p.add_argument('--idle-seconds',type=float,default=1.)
    p.add_argument('--compare',action='store_true',help='Compare local git commit bfc875f; no fetch')
    p.add_argument('--output',type=Path)
    p.add_argument('--child',choices=['before','after'],help=argparse.SUPPRESS)
    args=p.parse_args()
    if not 1<=args.samples<=100 or not .01<=args.idle_seconds<=3600:p.error('invalid bounded sample/idle range')
    if args.child:
        print(json.dumps(sample(args.child,args.idle_seconds)));return
    variants=('before','after') if args.compare else ('after',)
    raw={f'{v}_{m}':[] for v in variants for m in ('cold','warm')}
    for mode in ('cold','warm'):
        for i in range(args.samples):
            for variant in (variants if i%2==0 else tuple(reversed(variants))):
                if mode=='cold':
                    child=subprocess.run([sys.executable,str(Path(__file__).resolve()),'--child',variant,
                        '--idle-seconds',str(args.idle_seconds)],cwd=ROOT,capture_output=True,
                        text=True,check=True,timeout=args.idle_seconds+45)
                    value=json.loads(child.stdout)
                else:value=sample(variant,args.idle_seconds)
                raw[f'{variant}_{mode}'].append(value)
    groups={key:dict(n=len(samples),summary=summarize(samples),samples=samples) for key,samples in raw.items()}
    report=dict(kind='offline_full_daily_two_successes',baseline=BASELINE if args.compare else None,
                actual_interval_seconds=1,groups=groups,
                limitations='Synthetic immediate responses; early one-hour wait is compressed; only final idle is real. No server/network latency or success-rate claim.')
    text=json.dumps(report,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(text,encoding='utf-8')
    print(text)


if __name__=='__main__':main()
