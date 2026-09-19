from datetime import datetime
from unittest.mock import MagicMock,patch
import json
import pytest
from api import classify_rejection,safe_message_pattern
from booking import choose,run
from daily import build_plan,DEFAULT


def make_rows(count1=4,count2=2):
    out=[]
    for start,end,count in [('20:00','21:00',count1),('21:00','22:00',count2)]:
        for i,n in enumerate([6,1,2,3][:count]):
            out.append(dict(id=f'{start}-{n}',date='2030-01-01',start=start,end=end,name=f'仙林体育馆3F羽毛球馆{n}号场地',available=True))
    return out


def plan(rows):
    return build_plan({'windows':[dict(start='20:00',end='21:00',quantity=1),dict(start='21:00',end='22:00',quantity=1)],'priority':DEFAULT['priority']},rows,'2030-01-01')[0]


def test_configured_time_order_overrides_smaller_candidate_count():
    rows=make_rows();p=plan(rows)
    g,s=choose(p['targets'],rows,{},set(),set(),target_order=p['target_order'])
    assert s['start']=='20:00' and '6号' in s['name']
    assert choose(p['targets'],rows,{},set(),set())[1]['start']=='21:00' # legacy behavior


def test_timing_rejection_not_treated_as_sold_out():
    assert classify_rejection(7070,'当前不在预约时间范围')['category']=='outside_booking_window'
    assert classify_rejection(7070,'尚未到预约时间')['category']=='not_open_yet'
    assert classify_rejection(7070,'场地已被预定')['category']=='sold_out'


def test_message_pattern_never_exposes_arbitrary_identifiers():
    text='张三13812345678 当前不在预约时间范围12:00-22:00'
    result=safe_message_pattern(text)
    assert '张三' not in result and '13812345678' not in result
    assert '12:00-22:00' in result
    assert safe_message_pattern('token=synthetic')=='[敏感消息已隐藏]'


def test_release_boundary_retries_preferred_once_only(tmp_path):
    rows=make_rows();p=plan(rows);c=MagicMock();c.timings=[]
    failure=dict(state='rejected',definitive_rejection=True,category='outside_booking_window',err_code=7070,message_digest='timing')
    c.submit.side_effect=[failure,failure,dict(state='success'),dict(state='success')]
    result=run(p,c,tmp_path,lambda _:None,prepared=rows,before_first_submit=lambda:{})
    ids=[call.args[0]['id'] for call in c.submit.call_args_list]
    assert ids==['20:00-6','20:00-6','20:00-1','21:00-6']
    assert result['state']=='complete'
    c.slots.assert_not_called()


def test_repeated_rejection_refresh_skips_stale_candidates(tmp_path):
    rows=make_rows();p=plan(rows);c=MagicMock();c.timings=[]
    failure=dict(state='rejected',definitive_rejection=True,category='server_rejected_unknown',err_code=7070,message_digest='same')
    c.submit.side_effect=[failure,failure,failure,dict(state='success'),dict(state='success')]
    c.slots.return_value=[dict(s,available=(s['id']=='20:00-3' or s['start']=='21:00')) for s in rows]
    result=run(p,c,tmp_path,lambda _:None,prepared=rows,before_first_submit=lambda:{})
    assert result['state']=='complete'
    c.slots.assert_called_once()
    assert result['attempts'][2]['action']=='refresh_once_after_repeated_rejection'


def test_request_timing_and_history_credit_are_explicit(tmp_path):
    rows=make_rows();p=plan(rows);c=MagicMock();c.timings=[]
    def submit(s):
        c.timings.append(dict(endpoint='booking',local_send=12345,corrected_send=12346,rtt_ms=10))
        return dict(state='success')
    c.submit.side_effect=submit
    first=run(p,c,tmp_path,lambda _:None,prepared=rows,before_first_submit=lambda:{})
    assert first['attempts'][0]['request_timing']['local_send']==12345
    assert first['attempts'][0]['estimated_payment_deadline']==12645
    second=run(p,c,tmp_path,lambda _:None,prepared=rows)
    assert not second['attempts']
    assert len(second['credited_slots'])==2
