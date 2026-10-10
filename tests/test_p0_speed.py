"""P0 invariants plus real Requests fast-path contract tests. No live booking requests."""
import copy, hashlib, json, time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs
import pytest, requests
from requests.adapters import BaseAdapter
import api, booking, daily
from api import SafeError, classify_rejection

DATE='2099-01-01'
def rows(counts=(4,4)):
    return [dict(id=f'{h}-{n}',date=DATE,start=f'{h}:00',end=f'{h+1}:00',name=f'仙林体育馆3F羽毛球馆{n}号场地',available=True)
            for h,nums in zip((19,20),([6,1,2,3][:counts[0]],[6,1,2,3][:counts[1]])) for n in nums]
def settings():
    return dict(windows=[dict(start='19:00',end='20:00',quantity=1),dict(start='20:00',end='21:00',quantity=1)],priority=copy.deepcopy(daily.DEFAULT_PRIORITY),focus_target='19:00-20:00',focus_court_number=6,max_attempts_per_target=30)
def plan(rr):return daily.build_plan(settings(),rr,DATE)[0]
def failure(category='server_rejected_unknown'):
    return dict(state='sold_out' if category=='sold_out' else 'rejected',definitive_rejection=True,category=category,
                boundary_retry_eligible=category=='not_open_yet',err_code=7070)

class Fake:
    def __init__(self,rr,outcomes,delays=None):
        self.rr=rr;self.outcomes=list(outcomes);self.delays=list(delays or []);self.calls=[];self.reads=0;self.timings=[]
        self.t=100.;self.last_request=-float('inf');self.min_request_interval=1.
    def slots(self,date):self.reads+=1;return [s for s in self.rr if s['date']==date]
    def submit(self,s):
        self.t=max(self.t,self.last_request+1);self.last_request=self.t;self.calls.append((s['id'],self.t))
        self.t+=self.delays.pop(0) if self.delays else .02
        return self.outcomes.pop(0) if self.outcomes else failure('sold_out')

def run_fake(tmp,rr,outcomes,*,strategy='coverage',refresh=None,delays=None):
    p=plan(rr);p['strategy']=strategy;c=Fake(refresh if refresh is not None else rr,outcomes,delays)
    with patch('booking.time.monotonic',side_effect=lambda:c.t):
        r=booking.run(p,c,tmp,lambda _:None,prepared=rr,before_first_submit=lambda:{})
    return c,r

@pytest.mark.parametrize('text,category,eligible',[
 ('请在规定时间预约，预约时间12:00-22:00','time_window_hint',False),
 ('不在预约时间范围','outside_booking_window',False),
 ('尚未到预约时间','not_open_yet',True),
 ('预约时间已经结束','time_window_hint',False),
 ('场地已经被预定','sold_out',False),
 ('场地已被其他人预定，请稍后重试','sold_out',False),
 ('已有未支付订单，尚未开放','unpaid_order_limit',False),
 ('认证失效，尚未开放','login_expired',False),
 ('请求过于频繁，尚未开放','rate_limit',False),
])
def test_semantics(text,category,eligible):
    r=classify_rejection(7070,text)
    assert (r['category'],r['boundary_retry_eligible'],r['classifier_version'])==(category,eligible,2)


def test_true_round_robin_when_first_rank_missing(tmp_path):
    rr=rows();rr=[s for s in rr if s['id']!='19-6']
    p=plan(rows());c=Fake(rr,[failure(),failure(),dict(state='success'),dict(state='success')])
    r=booking.run(p,c,tmp_path,lambda _:None,prepared=rr,before_first_submit=lambda:{})
    assert [k for k,_ in c.calls]==['19-1','20-6','19-2','20-1']
    assert r['state']=='complete'


def test_focus_court_overrides_generic_preference_and_focus_target_first(tmp_path):
    rr=rows();s=settings();s.update(focus_target='20:00-21:00',focus_court_number=3)
    p,_=daily.build_plan(s,rr,DATE);c=Fake(rr,[dict(state='success')]*2)
    booking.run(p,c,tmp_path,lambda _:None,prepared=rr,before_first_submit=lambda:{})
    assert [k for k,_ in c.calls]==['20-3','19-6']


@pytest.mark.parametrize('strategy,expected',[
 ('coverage',['19-6','20-6','19-6']),('focus',['19-6','19-6','20-6'])])
def test_one_boundary_retry_with_strategy(tmp_path,strategy,expected):
    c,r=run_fake(tmp_path,rows(),[failure('not_open_yet'),dict(state='success'),dict(state='success')],strategy=strategy)
    assert [k for k,_ in c.calls]==expected
    assert sum(a['is_boundary_retry'] for a in r['attempts'])==1
    assert r['state']=='complete'


def test_repeated_not_open_never_loops_same_court(tmp_path):
    c,r=run_fake(tmp_path,rows(),[failure('not_open_yet')]*3+[dict(state='success')]*2)
    assert [k for k,_ in c.calls]==['19-6','20-6','19-6','20-1','19-1']
    assert sum(a['is_boundary_retry'] for a in r['attempts'])==1


def test_slow_second_response_expires_pending_boundary_retry(tmp_path):
    c,r=run_fake(tmp_path,rows(),[failure('not_open_yet'),failure(),dict(state='success'),dict(state='success')],delays=[.02,3.8])
    assert [k for k,_ in c.calls]==['19-6','20-6','19-1','20-1']
    assert c.calls[2][1]>=c.calls[1][1]+3.8
    assert not any(a['is_boundary_retry'] for a in r['attempts'])


def test_no_candidate_refresh_only_once_no_poll(tmp_path):
    rr=rows((1,1));c,r=run_fake(tmp_path,rr,[failure(),failure()],refresh=[dict(s,available=False) for s in rr])
    assert len(c.calls)==2 and c.reads==1 and r['recovery_reads']==1
    assert r['exit_reason']=='no_candidates_refresh_budget_exhausted'


def test_three_unknown_rejections_not_refresh_trigger(tmp_path):
    rr=rows((4,0));p=plan(rr+[rows()[4]]);p['targets']=p['targets'][:1];p['max_orders']=1
    c=Fake(rr,[failure()]*3+[dict(state='success')]);r=booking.run(p,c,tmp_path,lambda _:None,prepared=rr,before_first_submit=lambda:{})
    assert r['state']=='complete' and c.reads==0


def test_three_soldouts_refresh_once_not_reenable_success(tmp_path):
    rr=rows();p=plan(rr);c=Fake(rr,[dict(state='success')]+[failure('sold_out')]*3+[dict(state='success')])
    r=booking.run(p,c,tmp_path,lambda _:None,prepared=rr,before_first_submit=lambda:{})
    assert r['state']=='complete' and c.reads==1
    assert [k for k,_ in c.calls]==['19-6','20-6','20-1','20-2','20-3']


@pytest.mark.parametrize('outcome',[dict(state='unknown',reason='timeout'),dict(state='blocked',reason='rate_limit')])
def test_unknown_or_blocked_holds_slot_and_stops_all(tmp_path,outcome):
    c,r=run_fake(tmp_path,rows(),[outcome])
    assert len(c.calls)==1 and c.reads==0 and r['state']==outcome['state']
    if outcome['state']=='unknown':
        with pytest.raises(SafeError):booking.run(plan(rows()),c,tmp_path,lambda _:None,prepared=rows())


def test_hard_cap_rejects_config_and_two_previous_reservations(tmp_path):
    p=plan(rows());p['max_orders']=3
    with pytest.raises(SafeError):booking.validate(p)
    rr=rows();c,r=run_fake(tmp_path,rr,[dict(state='success')]*2)
    unrelated=[dict(s,id='another-'+s['id'],start='21:00',end='22:00') for s in rr[:2]]
    p2={'version':2,'targets':[dict(id='new',date=DATE,start='21:00',end='22:00',quantity=1,courts=[s['name'] for s in unrelated])],'max_orders':1}
    c2=Fake(unrelated,[dict(state='success')]);r2=booking.run(p2,c2,tmp_path,lambda _:None,prepared=unrelated)
    assert r2['state']=='blocked' and not c2.calls


def test_intent_failure_no_post(tmp_path):
    rr=rows();c=Fake(rr,[dict(state='success')]);real=booking.atomic
    def atomic(path,obj):
        if any(a['state']=='submitting' for a in obj.get('attempts',[])):raise OSError('synthetic failure')
        return real(path,obj)
    with patch('booking.atomic',side_effect=atomic),pytest.raises(OSError):booking.run(plan(rr),c,tmp_path,lambda _:None,prepared=rr)
    assert not c.calls


class InspectAdapter(BaseAdapter):
    def __init__(self):self.calls=[]
    def send(self,p,**kwargs):
        assert kwargs['verify'] is True and kwargs['timeout']==(5,8)
        self.calls.append((p.body,dict(p.headers),kwargs))
        trace=p._booking_trace;trace['transport_enter_ns']=time.perf_counter_ns();trace['response_headers_ns']=time.perf_counter_ns()
        r=requests.Response();r.status_code=200;r._content=b'{"success":false,"errCode":7070,"errMsg":"rejected"}';r.headers={};r.request=p
        return r
    def close(self):pass


def client():
    c=object.__new__(api.Client);c.claims={'studentId':'synthetic'};c.http=requests.Session();c.http.trust_env=False;c.http.proxies={}
    adapter=InspectAdapter();c.http.mount('https://',adapter);c.last_request=-float('inf');c.min_request_interval=1;c.timings=[];c.clock_time=lambda:1000.;c.telemetry_incomplete=False
    return c,adapter


def test_prepared_request_exact_signature_no_session_preparation_after_gate(tmp_path):
    c,adapter=client();sl=rows()[0];send=c.prepare_submit(sl);c.clock_time=lambda:1001.23
    (tmp_path/'private').mkdir()
    with patch('api.ROOT',tmp_path),patch.object(c.http,'prepare_request',side_effect=AssertionError('late preparation')):
        r=send()
    body=parse_qs(adapter.calls[0][0]);ts=body['timestamp'][0]
    assert ts=='1001230'
    assert body['fingerprint'][0]==hashlib.sha256(f"{sl['id']}|synthetic|{sl['date']}|{ts}|4pGmY6s9zX".encode()).hexdigest()
    assert len(adapter.calls[0][0])==int(adapter.calls[0][1]['Content-Length'])
    assert r['state']=='rejected'
    assert 'synthetic' not in (tmp_path/'private/request-events.jsonl').read_text(encoding='utf-8')
    assert c.timings[0]['signature_ready_ns']<=c.timings[0]['http_call_started_ns']<=c.timings[0]['transport_enter_ns']
    c.close()


def test_boundary_expiry_checked_after_limiter(tmp_path):
    c,adapter=client();send=c.prepare_submit(rows()[0]);t=[.5];c.last_request=0;c.submit_not_after=1.1
    def sleep(_):t[0]=1.2
    with patch('api.time.monotonic',side_effect=lambda:t[0]),patch('api.time.sleep',side_effect=sleep):r=send()
    assert r['state']=='not_submitted' and not adapter.calls
    c.close()


def test_separate_limiter_and_http_measurements(tmp_path):
    c,adapter=client();send=c.prepare_submit(rows()[0]);c.last_request=time.monotonic();c.min_request_interval=.02
    (tmp_path/'private').mkdir()
    with patch('api.ROOT',tmp_path):send()
    e=c.timings[0]
    assert e['limiter_wait_ms']>=10 and e['http_total_ms']<e['limiter_wait_ms']
    assert e['limiter_finished_ns']<=e['signature_ready_ns']<=e['http_call_started_ns']<=e['response_headers_ns']<=e['response_body_done_ns']
    c.close()

def test_route_summary_never_ranks_502_as_success():
    from perf_routes import summarize
    s=summarize([dict(route='clash',total_ms=1,error_type=None,http_status=502),dict(route='direct',total_ms=2,error_type=None,http_status=502)])
    assert s['clash']['usable_responses']==s['direct']['usable_responses']==0
    assert s['clash']['usable_median_ms'] is None


def test_adapter_connection_reuse_observation_without_identifiers():
    from transport_metrics import MeasuredAdapter
    from requests.adapters import HTTPAdapter
    adapter=MeasuredAdapter();conn=SimpleNamespace(sock=object());raw=SimpleNamespace(_connection=conn)
    request=requests.Request('GET','https://example.invalid/').prepare();request._booking_trace={}
    with patch.object(HTTPAdapter,'send',return_value=SimpleNamespace(raw=raw)):
        adapter.send(request);assert request._booking_trace['connection_reuse_observed'] is False
        request._booking_trace={};adapter.send(request);assert request._booking_trace['connection_reuse_observed'] is True
    assert all(not isinstance(v,str) for v in request._booking_trace.values())
    adapter.close()


def test_after_send_durable_failure_stops_and_preserves_unknown_intent(tmp_path):
    rr=rows();c=Fake(rr,[dict(state='success')]);real=booking.atomic
    def save(path,obj):
        if any(a['state']=='success' for a in obj.get('attempts',[])):raise OSError('disk failure after response')
        return real(path,obj)
    with patch('booking.atomic',side_effect=save),pytest.raises(OSError):booking.run(plan(rr),c,tmp_path,lambda _:None,prepared=rr)
    assert len(c.calls)==1
    with pytest.raises(SafeError):booking.existing_keys(tmp_path)


def test_focus_ui_only_updates_configuration(tmp_path):
    from unittest.mock import MagicMock
    s=settings();dest=tmp_path/'settings.json'
    with patch('daily.load_settings',return_value=s),patch('daily.SETTINGS',dest),patch('builtins.input',side_effect=['3','2','3','2']),patch('api.Client') as live:
        daily.change_priority()
    saved=json.loads(dest.read_text(encoding='utf-8'))
    assert saved['focus_target']=='20:00-21:00' and saved['focus_court_number']==3 and saved['strategy']=='focus'
    live.assert_not_called()


def test_custom_unknown_rejection_allows_alternative_but_never_same_retry(tmp_path):
    rr=rows();c,r=run_fake(tmp_path,rr,[failure('time_window_hint'),dict(state='success'),dict(state='success')])
    assert [k for k,_ in c.calls]==['19-6','20-6','19-1']
    assert c.reads==0 and not any(a['is_boundary_retry'] for a in r['attempts'])


def test_refreshed_rows_cannot_expand_user_candidates(tmp_path):
    rr=rows((1,1));extra=dict(rr[0],id='forbidden',name='outside-config')
    c,r=run_fake(tmp_path,rr,[failure(),failure()],refresh=[extra])
    assert len(c.calls)==2 and all(k!='forbidden' for k,_ in c.calls)
    assert c.reads==1


def test_invalid_focus_and_over_two_daily_settings_fail():
    s=settings();s['focus_target']='18:00-19:00'
    with pytest.raises(SafeError):daily.validate_settings(s)
    s=settings();s['windows'][0]['quantity']=2
    with pytest.raises(SafeError):daily.validate_settings(s)


def test_slow_single_response_serial_second_not_started_early(tmp_path):
    c,r=run_fake(tmp_path,rows(),[dict(state='success')]*2,delays=[3.8,.02])
    assert c.calls[1][1]>=c.calls[0][1]+3.8
    assert r['state']=='complete' and c.reads==0

def test_get_connection_is_observed_before_first_post():
    from transport_metrics import MeasuredAdapter
    from requests.adapters import HTTPAdapter
    adapter=MeasuredAdapter();conn=SimpleNamespace(sock=object());raw=SimpleNamespace(_connection=conn)
    first=requests.Request('GET','https://example.invalid/').prepare()
    second=requests.Request('POST','https://example.invalid/').prepare();second._booking_trace={}
    with patch.object(HTTPAdapter,'send',return_value=SimpleNamespace(raw=raw)):
        adapter.send(first);adapter.send(second)
    assert second._booking_trace['connection_reuse_observed'] is True
    adapter.close()
