"""No-network regression suite for long waits, clock failure and private diagnostics."""
import json
import subprocess
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from pathlib import Path
import pytest
import clock_sync
import daily
import task_runtime as runtime
import booking
from api import SafeError, CN, Client
from safe_errors import safe_details

FIRE=datetime(2030,1,1,12,tzinfo=CN)

class Ticks:
    def __init__(self):self.value=0
    def mono(self):return self.value
    def sleep(self,n):self.value+=n

@pytest.mark.parametrize('stdout,code',[
 ('nonsense token=SECRET','CLOCK_SAMPLES_INSUFFICIENT'),
 ('12:00:00, +1.0s\n12:00:01, +1.5s\n','CLOCK_SAMPLE_SPREAD_HIGH'),
 ('12:00:00, +65.0s\n12:00:01, +65.0s\n','CLOCK_OFFSET_OUT_OF_RANGE'),
])
def test_clock_failure_classification_and_no_raw_output(stdout,code):
    events=[]
    with pytest.raises(SafeError) as err:
        clock_sync.calibrate(runner=lambda *a,**k:SimpleNamespace(stdout=stdout,returncode=1),
                             observer=lambda *a:events.append(a))
    assert err.value.code==code
    assert len(events)==4
    assert 'SECRET' not in json.dumps(events)


def test_timeout_then_other_source_success():
    def runner(cmd,**kw):
        if 'windows' in cmd[2]:raise subprocess.TimeoutExpired(cmd,kw['timeout'])
        return SimpleNamespace(stdout='12:00:00, +0.010s\n12:00:01, +0.011s\n',returncode=0)
    events=[]
    c=clock_sync.calibrate(runner=runner,observer=lambda *a:events.append(a))
    assert c.info['sources']==['time.cloudflare.com']
    assert events[1][1]['last_error_code']=='CLOCK_COMMAND_TIMEOUT'


def test_refresh_retry_then_success():
    t=Ticks(); calls=[]; sentinel=object()
    def sample(**kw):
        calls.append(kw['round_number'])
        if len(calls)==1:raise SafeError(code='CLOCK_COMMAND_TIMEOUT')
        return sentinel
    assert clock_sync.refresh_clock(80,calibrator=sample,monotonic=t.mono,sleep=t.sleep) is sentinel
    assert calls==[1,2] and t.value==5


def test_refresh_three_failures_stop():
    t=Ticks();calls=[]
    def sample(**kw):
        calls.append(kw['round_number']);raise SafeError(code='CLOCK_SAMPLES_INSUFFICIENT')
    with pytest.raises(SafeError) as e:
        clock_sync.refresh_clock(80,calibrator=sample,monotonic=t.mono,sleep=t.sleep)
    assert e.value.code=='CLOCK_REFRESH_EXHAUSTED'
    assert e.value.details['last_error_code']=='CLOCK_SAMPLES_INSUFFICIENT'
    assert calls==[1,2,3]


def test_command_deadline_caps_timeout_and_no_second_source():
    t=Ticks();timeouts=[]
    def runner(cmd,**kw):
        timeouts.append(kw['timeout']);t.sleep(kw['timeout'])
        raise subprocess.TimeoutExpired(cmd,kw['timeout'])
    with pytest.raises(SafeError) as e:
        clock_sync.calibrate(deadline=1.25,runner=runner,monotonic=t.mono)
    assert timeouts==[1.25] and e.value.code=='CLOCK_COMMAND_TIMEOUT'


@pytest.mark.parametrize('strict',[True,False])
@pytest.mark.parametrize('gap,jump',[(6,0),(1,3)])
def test_wait_discontinuity(strict,gap,jump):
    t=Ticks();w=[100];events=[]
    guard=runtime.WaitGuard(strict=strict,monotonic=t.mono,wall=lambda:w[0],observer=lambda *a:events.append(a))
    t.value=gap;w[0]+=gap+jump
    if strict:
        with pytest.raises(SafeError) as e:guard.check()
        assert e.value.code=='WAIT_DISCONTINUITY'
    else:
        guard.check();assert len(events)==1


def test_task_events_filter_secrets_and_keep_failure(tmp_path):
    journal=runtime.TaskJournal(tmp_path,'scheduled',FIRE,[])
    journal.transition('refresh_calibration')
    err=SafeError('token=SECRET cookie=SECRET',code='CLOCK_SAMPLES_INSUFFICIENT',
                  details={'source':'SECRET','token':'SECRET','round':2,'last_error_code':'SECRET'})
    journal.finish('stopped',exc=err)
    raw=''.join(p.read_text(encoding='utf-8') for p in tmp_path.iterdir())
    assert 'SECRET' not in raw
    current=json.loads((tmp_path/'current-task.json').read_text(encoding='utf-8'))
    assert current['error_code']=='CLOCK_SAMPLES_INSUFFICIENT'
    assert current['phase']=='refresh_calibration'
    assert current['error_details']=={'round':2}


def test_failed_durable_task_prevents_client(tmp_path):
    s=dict(windows=[dict(start='20:00',end='21:00',quantity=1)],priority=daily.DEFAULT_PRIORITY)
    with patch('daily.load_settings',return_value=s),patch('daily.ROOT',tmp_path),patch('daily.now_cn',return_value=FIRE-timedelta(hours=1)),patch('task_runtime.atomic',side_effect=OSError('SECRET')),patch('daily.Client') as client:
        assert daily.execute(confirm=lambda _: '1') is None
    client.assert_not_called()


def test_deferred_transition_does_not_write_until_response(tmp_path):
    journal=runtime.TaskJournal(tmp_path,'scheduled',FIRE,[])
    journal.transition('ready_wait')
    with patch('task_runtime.atomic') as save:
        journal.transition('booking_engine',defer=True)
        save.assert_not_called()
        journal.event('submission_observed')
        assert save.call_count==2
    rows=[json.loads(x) for x in (tmp_path/f'events-{journal.task_id}.jsonl').read_text(encoding='utf-8').splitlines()]
    assert [(x['phase'],x['event']) for x in rows[-3:]]==[('ready_wait','completed'),('booking_engine','started'),('booking_engine','submission_observed')]


def test_heartbeat_suppressed_in_last_ten_seconds(tmp_path):
    j=runtime.TaskJournal(tmp_path,'scheduled',FIRE,[])
    j.clock=SimpleNamespace(now=lambda:FIRE-timedelta(seconds=9))
    j.last_heartbeat=-float('inf')
    with patch.object(j,'event') as event:j.heartbeat(FIRE)
    event.assert_not_called()


def test_diagnostic_read_only_and_stale(tmp_path,capsys):
    j=runtime.TaskJournal(tmp_path,'scheduled',FIRE,[])
    j.data['pid']=99999999;j.persist()
    before={p.name:p.read_bytes() for p in tmp_path.iterdir()}
    with patch('api.Client',side_effect=AssertionError('network forbidden')),patch('clock_sync.calibrate',side_effect=AssertionError('clock forbidden')):
        runtime.diagnose(tmp_path)
    assert '无法确认' in capsys.readouterr().out
    assert before=={p.name:p.read_bytes() for p in tmp_path.iterdir()}


def test_unresolved_task_cannot_be_restarted(tmp_path):
    j=runtime.TaskJournal(tmp_path,'scheduled',FIRE,[])
    j.data.update(pid=99999999,phase='booking_engine');j.persist()
    with pytest.raises(SafeError) as err:runtime.TaskJournal(tmp_path,'scheduled',FIRE,[])
    assert err.value.code=='PREVIOUS_TASK_UNKNOWN'


def test_request_logging_failure_stops_after_first_post(tmp_path):
    row=dict(id='slot',name='A',date='2030-01-01',start='19:00',end='20:00',available=True)
    plan=dict(version=2,targets=[dict(id='g',date=row['date'],start=row['start'],end=row['end'],courts=['A'],quantity=1)],max_orders=1)
    class C:
        timings=[];telemetry_incomplete=False
        def submit(self,s):
            self.telemetry_incomplete=True
            return dict(state='success',reason='accepted')
    result=booking.run(plan,C(),tmp_path,lambda _:None,prepared=[row])
    assert result['state']=='unknown'
    assert len(result['attempts'])==1 and result['attempts'][0]['state']=='success'


def test_auth_vs_expiry_failure():
    c=object.__new__(Client);c.request=MagicMock(return_value=SimpleNamespace(status_code=401))
    with pytest.raises(SafeError) as e:c.get_list('/dummy')
    assert e.value.code=='AUTH_REJECTED'


def test_late_start_rejected_before_client():
    s=dict(windows=[dict(start='20:00',end='21:00',quantity=1)],priority=daily.DEFAULT_PRIORITY)
    with patch('daily.load_settings',return_value=s),patch('daily.now_cn',return_value=FIRE-timedelta(seconds=70)),patch('daily.Client') as client:
        assert daily.execute(confirm=lambda _: '1') is None
    client.assert_not_called()


@pytest.mark.parametrize('fail_refresh,cancel',[(False,False),(True,False),(False,True)])
def test_one_hour_early_end_to_end_without_network(tmp_path,fail_refresh,cancel):
    t=Ticks();base=FIRE-timedelta(hours=1)
    class Clock:
        offset=0;info={}
        def now(self):return base+timedelta(seconds=t.value)
        def timestamp(self):return self.now().timestamp()
        def age(self):return 0
        def check_gateway(self,event):pass
    clock=Clock();calls=[]
    class C:
        claims={'exp':FIRE.timestamp()+3600}
        def __init__(self,**kw):self.timings=[]
        def slots(self,date):
            calls.append(('slots',clock.now()))
            return [dict(id='s',name='仙林体育馆3楼6号场',date='2030-01-01',start='20:00',end='21:00',available=False)]
        def close(self):pass
    def sample(**kw):
        calls.append(('calibrate',clock.now()))
        if kw.get('round_number') and fail_refresh:raise SafeError(code='CLOCK_COMMAND_TIMEOUT')
        return clock
    def wait(target,clock,journal,fire,**kw):
        if cancel:raise KeyboardInterrupt()
        runtime.wait_until(target,clock,journal,fire,sleep=t.sleep,monotonic=t.mono,wall=lambda:base.timestamp()+t.value,**kw)
    def refresh(deadline,**kw):
        return clock_sync.refresh_clock(deadline,monotonic=t.mono,sleep=t.sleep,**kw)
    def engine(plan,c,**kw):
        calls.append(('run',clock.now()))
        assert not kw['prepared'][0]['observed_available']
        assert kw['context']['task_id']
        kw['before_first_submit']()
        calls.append(('post_simulated',clock.now()))
        return dict(state='complete',attempts=[],credited_slots=[])
    def gate(fire,warmup,**kw):
        from scheduling import wait_for_start
        return wait_for_start(fire,warmup,monotonic=t.mono,sleep=t.sleep,**kw)
    s=dict(windows=[dict(start='20:00',end='21:00',quantity=1)],priority=daily.DEFAULT_PRIORITY)
    with patch('daily.load_settings',return_value=s),patch('daily.ROOT',tmp_path),patch('daily.now_cn',side_effect=clock.now),patch('daily.Client',C),patch('daily.calibrate',side_effect=sample),patch('daily.refresh_clock',side_effect=refresh),patch('daily.time.monotonic',side_effect=t.mono),patch('daily.WakeLease'),patch('daily.WaitGuard',side_effect=lambda **kw:runtime.WaitGuard(monotonic=t.mono,wall=lambda:base.timestamp()+t.value,**kw)),patch('daily.wait_until',side_effect=wait),patch('daily.wait_for_start',side_effect=gate),patch('daily.run',side_effect=engine):
        daily.execute(confirm=lambda _: '1')
    current=json.loads((tmp_path/'private/current-task.json').read_text(encoding='utf-8'))
    if cancel:
        assert current['state']=='cancelled' and current['error_code']=='USER_CANCELLED'
    elif fail_refresh:
        assert current['error_code']=='CLOCK_REFRESH_EXHAUSTED'
        assert [k for k,_ in calls]==['calibrate']*4
    else:
        assert current['state']=='complete'
        assert [k for k,_ in calls]==['calibrate','calibrate','slots','run','post_simulated']
        assert calls[1][1]==FIRE-timedelta(seconds=150)
        assert calls[2][1]==FIRE-timedelta(seconds=60)
        assert calls[-1][1]==FIRE
    if cancel or fail_refresh:assert not any(k in ('slots','post_simulated') for k,_ in calls)


def test_prepared_submit_has_no_identity_parse_after_gate(tmp_path):
    from api import Client
    c=object.__new__(Client);c.claims={'studentId':'synthetic'};c.clock_time=lambda:1000.0;c.last_request=-float('inf');c.min_request_interval=0;c.timings=[];c.telemetry_incomplete=False
    response=SimpleNamespace(status_code=200,json=lambda:{'success':False,'errCode':7070,'errMsg':'场地已被预约'},headers={},content=b'')
    c.http=MagicMock();c.http.request.return_value=response
    slot={'id':'s','name':'A','date':'2030-01-01','start':'19:00','end':'20:00'}
    send=c.prepare_submit(slot)
    assert callable(send)
    (tmp_path/'private').mkdir()
    with patch('api.ROOT',tmp_path):result=send()
    assert result['state']=='sold_out'
    assert c.http.request.call_count==1


def test_log_has_segmented_request_timing(tmp_path):
    from api import Client
    c=object.__new__(Client);c.last_request=-float('inf');c.min_request_interval=0;c.clock_time=lambda:1000.0;c.timings=[];c.telemetry_incomplete=False;c.http=MagicMock()
    c.http.request.return_value=SimpleNamespace(status_code=200,headers={'Date':'Sat, 10 Oct 2026 04:00:00 GMT'},content=b'')
    (tmp_path/'private').mkdir()
    with patch('api.ROOT',tmp_path):
        c.request('GET','/venue/user/types')
    event=c.timings[0]
    assert event['http_call_started_ns'] <= event['response_headers_ns'] <= event['response_body_done_ns']
    assert event['limiter_finished_ns'] <= event['signature_ready_ns'] <= event['http_call_started_ns']
    assert event['limiter_wait_ms'] >= 0
