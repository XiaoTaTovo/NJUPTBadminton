"""Synthetic-only P0/P1 acceptance: recovery, I/O boundaries, long waits and timing."""
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import api
import booking
import durable_attempts as durable
import task_runtime
from test_p0_speed import Fake, client, failure, plan, rows


def run(tmp, c, **kw):
    return booking.run(plan(rows()), c, tmp, lambda _:None, prepared=rows(), **kw)


def test_full_report_only_before_and_after_submissions(tmp_path):
    c=Fake(rows(),[{'state':'success'}]*2)
    real=booking.atomic
    writes=[]
    def save(path,obj):
        if path.name.startswith('run-'):
            writes.append(len(c.calls))
            assert len(c.calls)!=1
        return real(path,obj)
    with patch('booking.atomic',side_effect=save):
        r=run(tmp_path,c)
    assert r['state']=='complete' and writes[-1]==2
    assert all(x in (0,2) for x in writes)


def test_slow_notification_cannot_delay_second_post(tmp_path):
    release=threading.Event();entered=threading.Event()
    def notify(_):
        entered.set();release.wait(3)
    c=Fake(rows(),[{'state':'success'}]*2)
    try:
        r=booking.run(plan(rows()),c,tmp_path,notify,prepared=rows())
        assert entered.is_set() and not release.is_set()
        assert len(c.calls)==2 and r['notifications']['pending']
    finally: release.set()


def test_broken_notification_not_business_failure(tmp_path):
    def broken(_): raise RuntimeError('TOKEN-DO-NOT-LOG')
    r=booking.run(plan(rows()),Fake(rows(),[{'state':'success'}]*2),tmp_path,broken,prepared=rows())
    assert r['state']=='complete' and r['notifications']['failed']
    assert 'TOKEN-DO-NOT-LOG' not in ''.join(p.read_text(encoding='utf-8') for p in tmp_path.glob('*.json'))


def test_final_report_failure_still_recovers_successes(tmp_path):
    c=Fake(rows(),[{'state':'success'}]*2);real=booking.atomic
    def save(path,obj):
        if path.name.startswith('run-') and len(c.calls)==2: raise OSError('synthetic report fault')
        return real(path,obj)
    with patch('booking.atomic',side_effect=save): r=run(tmp_path,c)
    assert r['state']=='complete' and r['telemetry_incomplete']
    assert len(booking.existing_keys(tmp_path))==2
    c2=Fake(rows(),[{'state':'success'}]*2)
    r2=run(tmp_path,c2)
    assert r2['state']=='complete' and not c2.calls


@pytest.mark.parametrize('state',['success','unknown','blocked','sold_out','rejected'])
def test_minimal_result_state_and_redaction(tmp_path,state):
    c=Fake(rows(),[dict(state=state,definitive_rejection=True,order_id='SYNTHETIC-ORDER-SECRET')]*2)
    r=run(tmp_path,c)
    evidence=durable.recover(tmp_path)
    assert next(iter(evidence.values()))['state']==state
    raw=''.join(p.read_text(encoding='utf-8') for p in tmp_path.glob('result-*.json'))
    assert 'SYNTHETIC-ORDER-SECRET' not in raw
    assert not any(k in raw for k in ('token','fingerprint','studentId','headers'))


@pytest.mark.parametrize('corruption',['bad_json','missing_result','mismatch','orphan'])
def test_corrupted_or_missing_evidence_blocks_restart(tmp_path,corruption):
    run(tmp_path,Fake(rows(),[{'state':'success'}]*2))
    p=next(tmp_path.glob('result-*.json'))
    if corruption=='bad_json': p.write_text('{',encoding='utf-8')
    elif corruption=='missing_result': p.unlink()
    elif corruption=='mismatch':
        d=json.loads(p.read_text(encoding='utf-8'));d['slot']['id']='wrong'
        p.write_text(json.dumps(d),encoding='utf-8')
    else:
        p.with_name(p.name.replace('result-','intent-')).unlink()
    with pytest.raises(api.SafeError): booking.existing_keys(tmp_path)


@pytest.mark.parametrize('fault_stage',['write','fsync','replace'])
def test_critical_result_io_failure_stops_after_one(tmp_path,fault_stage):
    c=Fake(rows(),[{'state':'success'}]*2)
    real=durable.write_record
    def save(path,obj):
        if path.name.startswith('result-'):
            if fault_stage=='write': raise OSError('fault')
            target='durable_attempts.os.fsync' if fault_stage=='fsync' else 'durable_attempts.os.replace'
            with patch(target,side_effect=OSError('fault')): return real(path,obj)
        return real(path,obj)
    with patch('durable_attempts.write_record',side_effect=save),pytest.raises(api.SafeError): run(tmp_path,c)
    assert len(c.calls)==1
    with pytest.raises(api.SafeError): booking.existing_keys(tmp_path)


@pytest.mark.parametrize('edge',['intent','before_result','result','second_intent'])
def test_hard_process_exit_recovery(tmp_path,edge):
    # Child forcibly exits with no finally handlers: no live adapter or credentials.
    script=r'''
import os,sys
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'tests'))
from test_p0_speed import Fake,rows,plan
import booking,durable_attempts as d
folder=Path(sys.argv[1]);edge=sys.argv[2];real=d.write_record
def save(path,obj):
    if edge=='before_result' and path.name.startswith('result-'):os._exit(73)
    out=real(path,obj)
    if edge=='intent' and path.name.startswith('intent-'):os._exit(73)
    if edge=='result' and path.name.startswith('result-'):os._exit(73)
    if edge=='second_intent' and path.name.startswith('intent-') and path.stem.endswith('-002'):os._exit(73)
    return out
d.write_record=save
booking.run(plan(rows()),Fake(rows(),[dict(state='success')]*2),folder,lambda _:None,prepared=rows())
'''
    child=subprocess.run([sys.executable,'-c',script,str(tmp_path),edge],capture_output=True,timeout=15)
    assert child.returncode==73
    if edge=='result':
        assert len(booking.existing_keys(tmp_path))==1
        c=Fake(rows(),[{'state':'success'}]);run(tmp_path,c)
        assert [x[0] for x in c.calls]==['20-6']
    else:
        with pytest.raises(api.SafeError): booking.existing_keys(tmp_path)


def test_minimal_io_and_selection_timings(tmp_path):
    r=run(tmp_path,Fake(rows(),[{'state':'success'}]*2))
    for a in r['attempts']:
        for key in ('intent_io','result_io'):
            assert set(('serialization_ms','write_ms','flush_ms','fsync_ms','replace_ms','thread_cpu_ms'))<=a[key].keys()
            assert a[key]['bytes']<2048 and a[key]['total_ms']>=0
        assert a['candidate_selection_ms']>=0 and a['task_update_ms']>=0
    assert r['attempts'][1]['previous_classification_to_candidate_ms']>=0


def test_warmup_synthetic_no_send_no_limiter_changes():
    c,adapter=client();before=c.last_request
    with patch.object(c.http,'send',side_effect=AssertionError('warmup sent network')):
        c.warmup_local()
    assert c.last_request==before and not c.timings and not adapter.calls
    c.close()


def test_first_guard_delay_not_mislabelled_signature(tmp_path):
    c,adapter=client();(tmp_path/'private').mkdir()
    send=c.prepare_submit(rows()[0]);c.submission_guard=lambda:time.sleep(.03)
    with patch('api.ROOT',tmp_path): send()
    t=c.timings[-1]
    assert t['timing_version']==3 and t['submission_guard_ms']>=25
    for k in ('timestamp_ms','signature_ms','body_encode_ms','request_update_ms','prepare_thread_cpu_ms'):
        assert t[k]>=0
    assert t['signature_ms']<t['submission_guard_ms']
    c.close()


def test_all_requests_prepared_before_gate_and_no_report_io_between_posts(tmp_path):
    c,adapter=client();(tmp_path/'private').mkdir();c.min_request_interval=0
    prep=c.http.prepare_request;gate_passed=False
    def prepare(req):
        assert not gate_passed
        return prep(req)
    def gate():
        nonlocal gate_passed
        gate_passed=True
        return {}
    # Adapter always explicitly rejects, so every configured candidate is tried.
    c.slots=lambda _:[]
    with patch('api.ROOT',tmp_path),patch.object(c.http,'prepare_request',side_effect=prepare):
        r=run(tmp_path/'private',c,before_first_submit=gate)
    assert len(adapter.calls)==8
    assert len((tmp_path/'private/request-events.jsonl').read_text().splitlines())==8
    assert r['state']=='partial_or_exhausted'
    c.close()


def test_buffered_telemetry_failure_does_not_erase_durable_success(tmp_path):
    c=Fake(rows(),[{'state':'success'}]*2)
    c.defer_telemetry=False
    class Buffered(Fake):
        def flush_telemetry(self): self.telemetry_incomplete=True
    c=Buffered(rows(),[{'state':'success'}]*2)
    r=run(tmp_path,c)
    assert r['state']=='complete' and r['telemetry_incomplete']
    assert len(booking.existing_keys(tmp_path))==2


def test_task_phase_callback_is_deferred(tmp_path):
    from datetime import datetime
    j=task_runtime.TaskJournal(tmp_path,'scheduled',datetime(2099,1,1,12,tzinfo=api.CN),[])
    j.transition('ready_wait')
    with patch.object(j,'persist',side_effect=AssertionError('critical path task write')):
        j.transition('booking_engine',defer=True)
        j.event('submission_observed',defer=True)
    assert len(j.pending)==3
    j.finish('complete')
    assert not j.pending


def test_diagnostic_uses_minimal_records_without_final_report(tmp_path,capsys):
    from datetime import datetime
    j=task_runtime.TaskJournal(tmp_path,'scheduled',datetime(2099,1,1,12,tzinfo=api.CN),[])
    r=run(tmp_path,Fake(rows(),[{'state':'success'}]*2),context={'task_id':j.task_id})
    (tmp_path/f"run-{r['run_id']}.json").unlink()
    task_runtime.diagnose(tmp_path)
    out=capsys.readouterr().out
    assert '本轮新增成功： 2' in out and '未知/未完成意图： 0' in out


def test_torn_noncritical_report_recovers_from_minimal_results(tmp_path):
    r=run(tmp_path,Fake(rows(),[{'state':'success'}]*2))
    (tmp_path/f"run-{r['run_id']}.json").write_text('{',encoding='utf-8')
    assert len(booking.existing_keys(tmp_path))==2


@pytest.mark.parametrize('kind',['intent','result'])
def test_non_object_recovery_record_reports_structured_error(tmp_path,kind):
    run(tmp_path,Fake(rows(),[{'state':'success'}]*2))
    next(tmp_path.glob(f'{kind}-*.json')).write_text('[]',encoding='utf-8')
    with pytest.raises(api.SafeError) as exc:
        booking.existing_keys(tmp_path)
    assert exc.value.code=='RECOVERY_RECORD_INVALID'


def test_expensive_payload_cannot_cross_boundary_retry_deadline():
    c,adapter=client();clock=[.5];c.submit_not_after=1.
    def payload():
        clock[0]=1.2
        return {'timestamp':'1'}
    with patch('api.time.monotonic',side_effect=lambda:clock[0]),pytest.raises(api.SafeError) as exc:
        c.request('POST','/venue/user/booking/synthetic',data_factory=payload)
    assert exc.value.code=='BOUNDARY_RETRY_EXPIRED' and not adapter.calls
    c.close()


def test_recovery_query_invalidates_prepared_request_cache(tmp_path):
    class Prepared(Fake):
        def __init__(self):
            super().__init__(rows(),[failure('sold_out')]*8+[{'state':'success'}]*2)
            self.prepared_ids=[]
        def prepare_submit(self,slot):
            snapshot=dict(slot);self.prepared_ids.append(snapshot['id'])
            return lambda:self.submit(snapshot)
        def warmup_local(self):pass
        def slots(self,date):
            self.reads+=1
            return [dict(s,id='fresh-'+s['id']) for s in rows() if s['id'] in ('19-6','20-6')]
    c=Prepared();r=run(tmp_path,c,before_first_submit=lambda:{})
    assert c.reads==1
    assert 'fresh-19-6' in c.prepared_ids and 'fresh-20-6' in c.prepared_ids
    # Old IDs are never reused after fresh inventory replaces them.
    fresh_index=next(i for i,(sid,_) in enumerate(c.calls) if sid.startswith('fresh-'))
    assert all(sid.startswith('fresh-') for sid,_ in c.calls[fresh_index:])


def test_full_daily_pipeline_with_real_wait_and_two_mocked_successes():
    from perf_cold_start import sample
    r=sample('after',.03)
    assert r['success_count']==2 and r['network_calls']==0
    assert r['first_to_second_ms']>=999
    assert r['first_request']['timing_version']==3


def test_simulated_hour_wait_with_real_engine_and_recovery(tmp_path):
    from datetime import datetime,timedelta
    from task_runtime import WaitGuard,wait_until
    elapsed=[0.]
    start=datetime(2099,1,1,11,tzinfo=api.CN)
    class Clock:
        def now(self):return start+timedelta(seconds=elapsed[0])
    class Journal:
        def heartbeat(self,fire):pass
        def event(self,*a):pass
    guard=WaitGuard(strict=True,monotonic=lambda:elapsed[0],wall=lambda:start.timestamp()+elapsed[0])
    def gate():
        wait_until(start+timedelta(hours=1),Clock(),Journal(),start+timedelta(hours=1),
                   guard=guard,sleep=lambda n:elapsed.__setitem__(0,elapsed[0]+n))
        return {}
    c=Fake(rows(),[{'state':'success'}]*2)
    r=run(tmp_path,c,before_first_submit=gate)
    assert elapsed[0]==3600 and r['state']=='complete'
    assert len(booking.existing_keys(tmp_path))==2
