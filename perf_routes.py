"""Bounded unauthenticated read-only route measurement. Never reads session.json or sends POST."""
import argparse,json,time,statistics
from datetime import datetime
from pathlib import Path
import requests
from transport_metrics import MeasuredAdapter

URL='https://wechat.njupt.edu.cn/'

def measure(samples=5,interval=3):
    sessions={};rows=[];failures={'clash':0,'direct':0}
    for route in failures:
        s=requests.Session();s.trust_env=False
        s.proxies={'https':'http://127.0.0.1:7897'} if route=='clash' else {}
        s.mount('https://',MeasuredAdapter(max_retries=0));sessions[route]=s
    try:
        stop=False
        for i in range(samples):
            for route in ('clash','direct') if i%2==0 else ('direct','clash'):
                if failures[route]>=2:continue
                if rows:time.sleep(interval)
                trace={};s=sessions[route];p=s.prepare_request(requests.Request('GET',URL));p._booking_trace=trace
                start=time.perf_counter_ns();r=None
                row={'route':route,'sample':i+1,'http_status':None,'error_type':None}
                try:
                    r=s.send(p,verify=True,timeout=(3,5),allow_redirects=False,stream=True,proxies=s.proxies)
                    row['http_status']=r.status_code
                    if r.status_code==429:stop=True
                    size=0
                    for chunk in r.iter_content(16384):
                        size+=len(chunk)
                        if size>1048576:row['body_truncated']=True;break
                except requests.RequestException as exc:
                    row['error_type']=type(exc).__name__;failures[route]+=1
                finally:
                    end=time.perf_counter_ns()
                    if r is not None:r.close()
                    row.update(total_ms=round((end-start)/1e6,3),
                               headers_ms=round((trace['response_headers_ns']-start)/1e6,3) if trace.get('response_headers_ns') else None,
                               connection_reuse_observed=trace.get('connection_reuse_observed'))
                    rows.append(row)
                if stop:break
            if stop:break
    finally:
        for s in sessions.values():s.close()
    summaries=summarize(rows)
    return {'measured_at_local':datetime.now().isoformat(),'endpoint':'public_root_no_auth','samples_requested':samples,
            'interval_seconds':interval,'tls_verified':True,'summaries':summaries,'rows':rows,
            'limitations':'Public root only; no credentials; not booking latency or noon congestion. Direct and proxy may share upstream policy. DNS/TCP/TLS stages not separately measured.'}

def summarize(rows):
    summaries={}
    for route in ('clash','direct'):
        rr=[r for r in rows if r['route']==route]
        usable=[r['total_ms'] for r in rr if not r['error_type'] and 200<=r.get('http_status',0)<400]
        replies=[r['total_ms'] for r in rr if not r['error_type']]
        summaries[route]={'http_responses':len(replies),'usable_responses':len(usable),
                          'transport_errors':sum(bool(r['error_type']) for r in rr),
                          'http_error_responses':sum(r.get('http_status') is not None and r['http_status']>=400 for r in rr),
                          'usable_median_ms':round(statistics.median(usable),3) if usable else None,
                          'all_response_median_ms':round(statistics.median(replies),3) if replies else None}
    return summaries

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--network',action='store_true',help='Explicitly authorize at most five GETs per route');parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if not args.network:parser.error('Pass --network to perform bounded unauthenticated GET measurements')
    data=measure();text=json.dumps(data,ensure_ascii=False,indent=2)
    if args.output:args.output.write_text(text,encoding='utf-8')
    print(text)
