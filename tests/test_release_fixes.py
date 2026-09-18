from datetime import datetime,timedelta
from pathlib import Path
from unittest.mock import MagicMock,patch
import json
import pytest
import clock_sync
import daily
import booking
from api import Client, CN, SafeError, classify_rejection
from preferences import court_number,tier
from scheduling import wait_for_start


def test_ntp_localized_output():
    assert clock_sync.parse_offsets('12:05:01, +04.7165282s\n12:05:04, -00,0312s\n12:05:06, 错误: 0x800705B4\n')==[4.7165282,-.0312]


def test_ntp_accepts_measured_drift_without_changing_os_clock():
    sample=MagicMock(stdout='12:05:01, +04.7165282s\n12:05:04, +04.7112162s\n')
    with patch('clock_sync.subprocess.run',return_value=sample) as run:
        c=clock_sync.calibrate()
    assert c.offset==pytest.approx(4.7138722)
    assert '/stripchart' in run.call_args.args[0]
    assert '/resync' not in run.call_args.args[0]
    assert c.info['confidence']=='medium'


@pytest.mark.parametrize('stdout',['error\n','12:00:00, +65.0s\n12:00:01, +65.0s\n','12:00:00, +4.0s\n12:00:01, +4.5s\n'])
def test_bad_ntp_never_silently_falls_back_to_local(stdout):
    with patch('clock_sync.subprocess.run',return_value=MagicMock(stdout=stdout)):
        with pytest.raises(SafeError):clock_sync.calibrate()


def test_reference_clock_stays_monotonic_after_wall_clock_change():
    with patch('clock_sync.time.time',return_value=100),patch('clock_sync.time.monotonic',return_value=10):
        c=clock_sync.ReferenceClock(4.7,[4.7,4.7],['test'])
    with patch('clock_sync.time.time',return_value=999),patch('clock_sync.time.monotonic',return_value=12):
        assert c.timestamp()==pytest.approx(106.7)


def test_trigger_at_corrected_noon_not_local_noon():
    # Replay today's 4.71 second slow clock: scheduler must NOT wait until local noon.
    ticks=[0.0];fire=datetime(2030,1,1,12,tzinfo=CN)
    corrected_start=fire-timedelta(seconds=10)
    def now():return corrected_start+timedelta(seconds=ticks[0])
    def sleep(n):ticks[0]+=n
    info=wait_for_start(fire,lambda:None,now=now,monotonic=lambda:ticks[0],sleep=sleep)
    local_fired=now()-timedelta(seconds=4.71)
    assert local_fired==fire-timedelta(seconds=4.71)
    assert info['trigger_lateness_ms']==0


def test_gateway_is_corroboration_not_blind_date_correction():
    c=clock_sync.ReferenceClock(4.7,[4.7,4.7],['test'])
    c.check_gateway({'http_date':'Thu, 01 Jan 1970 00:01:44 GMT','local_send':100,'rtt_ms':30})
    assert c.info['gateway_corroborated']
    with pytest.raises(SafeError):
        c.check_gateway({'http_date':'Thu, 01 Jan 1970 00:01:50 GMT','local_send':100,'rtt_ms':30})


@pytest.mark.parametrize('suffix',['（单打）','(单打)',' （单打） '])
def test_single_court_suffix_is_not_excluded(suffix):
    for n in range(9,13):
        name=f'仙林体育馆3F羽毛球馆{n}号场地'+suffix
        assert court_number(name)==n
        assert tier(name)==1


def test_all_four_tiers_including_singles_sorted():
    settings={'windows':[dict(start='19:00',end='20:00',quantity=1)],'priority':{'tier_order':[0,1,2,3],'preferred_3f_court':6}}
    names=['仙林训练馆2F羽毛球1号场地','仙林体育馆1F羽毛球1号场地','仙林体育馆3F羽毛球馆9号场地（单打）','仙林体育馆3F羽毛球馆1号场地','仙林体育馆3F羽毛球馆6号场地']
    rows=[dict(name=n,id=str(i),date='2030-01-01',start='19:00',end='20:00',available=False) for i,n in enumerate(names)]
    p,_=daily.build_plan(settings,rows,'2030-01-01')
    assert p['targets'][0]['courts']==names[::-1]


def test_7070_alone_does_not_authorize_retry_or_expose_message():
    result=classify_rejection(7070,'synthetic-private-value 预约失败，请刷新')
    assert result['category']=='server_rejected_unknown'
    assert result['message_terms']==['预约','失败','刷新']
    assert 'synthetic-private-value' not in json.dumps(result)


def test_explicit_soldout_alternative_spelling():
    assert classify_rejection(123,'该场地已被预定')['category']=='sold_out'


def test_unknown_rejection_only_reads_then_stops(tmp_path):
    row=dict(id='test-slot',name='A',date='2030-01-01',start='19:00',end='20:00',available=True)
    plan=dict(version=2,targets=[dict(id='g',date=row['date'],start=row['start'],end=row['end'],courts=['A'],quantity=1)],max_orders=1)
    c=MagicMock();c.timings=[]
    c.submit.return_value=dict(state='blocked',reason='server_rejected_unknown',err_code=7070)
    c.slots.return_value=[dict(row,available=False)]
    result=booking.run(plan,c,tmp_path,lambda _:None,prepared=[row],context={'mode':'scheduled'})
    c.submit.assert_called_once();c.slots.assert_called_once()
    assert result['state']=='blocked'
    assert result['attempts'][0]['post_rejection_read']['available_count']==0
    assert result['context']['mode']=='scheduled'


def test_submission_payload_is_generated_after_throttle():
    c=object.__new__(Client);c.claims={'studentId':'synthetic'}
    c.clock_time=lambda:1004.7
    c.request=MagicMock(return_value=MagicMock(status_code=500))
    row=dict(id='slot',date='2030-01-01')
    c.submit(row)
    factory=c.request.call_args.kwargs['data_factory']
    assert factory()['timestamp']=='1004700'
    c.clock_time=lambda:1005.7
    assert factory()['timestamp']=='1005700'


def test_request_calls_payload_factory_after_wait(tmp_path):
    (tmp_path/'private').mkdir()
    c=object.__new__(Client);c.last_request=0;c.timings=[];c.clock_time=lambda:10
    c.http=MagicMock();c.http.request.return_value=MagicMock(status_code=200,headers={})
    events=[]
    def payload():events.append('payload');return {'timestamp':'test'}
    with patch('api.ROOT',tmp_path),patch('api.time.monotonic',return_value=.2),patch('api.time.sleep',side_effect=lambda _:events.append('wait')):
        c.request('POST','/booking/test',data_factory=payload)
    assert events==['wait','payload']
    assert 'data_factory' not in c.http.request.call_args.kwargs


def test_default_two_distinct_hours():
    assert daily.DEFAULT['windows']==[dict(start='19:00',end='20:00',quantity=1),dict(start='20:00',end='21:00',quantity=1)]

def test_two_scheduled_hours_submit_without_intermediate_get(tmp_path):
    rows=[dict(id=str(i),name='仙林体育馆3F羽毛球馆6号场地',date='2030-01-01',start=start,end=end,available=True) for i,(start,end) in enumerate([('19:00','20:00'),('20:00','21:00')])]
    plan,_=daily.build_plan(daily.DEFAULT,rows,'2030-01-01')
    c=MagicMock();c.timings=[];c.submit.return_value=dict(state='success')
    c.slots.side_effect=AssertionError('unexpected GET between prepared targets')
    result=booking.run(plan,c,tmp_path,lambda _:None,prepared=rows,before_first_submit=lambda:{'test':True})
    assert result['state']=='complete'
    assert c.submit.call_count==2
    c.slots.assert_not_called()
    assert [call.args[0]['start'] for call in c.submit.call_args_list]==['19:00','20:00']


def test_scheduled_explicit_soldout_refreshes_before_next_post(tmp_path):
    names=['仙林体育馆3F羽毛球馆6号场地','仙林体育馆3F羽毛球馆9号场地（单打）']
    rows=[dict(id=str(i),name=name,date='2030-01-01',start='19:00',end='20:00',available=True) for i,name in enumerate(names)]
    settings={'windows':[dict(start='19:00',end='20:00',quantity=1)],'priority':daily.DEFAULT['priority']}
    plan,_=daily.build_plan(settings,rows,'2030-01-01')
    c=MagicMock();c.timings=[];events=[]
    def submit(s):
        events.append('POST')
        return dict(state='sold_out' if s['id']=='0' else 'success')
    def slots(date):events.append('GET');return [dict(rows[0],available=False),rows[1]]
    c.submit.side_effect=submit;c.slots.side_effect=slots
    result=booking.run(plan,c,tmp_path,lambda _:None,prepared=rows,before_first_submit=lambda:{})
    assert events==['POST','GET','POST']
    assert result['state']=='complete'
