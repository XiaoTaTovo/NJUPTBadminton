import copy
import json
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from unittest.mock import patch
import pytest
import daily
import booking
import bootstrap
from api import SafeError, CN

DATE='2030-01-01'
def row(name, sid='1', available=True):
    return dict(id=sid,date=DATE,name=name,start='19:00',end='20:00',available=available)
def settings(quantity=1):
    s=copy.deepcopy(daily.DEFAULT);s['windows'][0]['quantity']=quantity;return s

def test_default_full_fallback_and_six_first():
    names=['仙林训练馆2楼1号场','仙林体育馆1楼1号场','仙林体育馆3楼9号场','仙林体育馆3楼1号场','仙林体育馆3楼6号场']
    rows=[row(n,str(i)) for i,n in enumerate(names)]
    p,prepared=daily.build_plan(settings(2),rows,DATE)
    assert p['targets'][0]['courts']==list(reversed(names))
    _,selected=booking.choose(p['targets'],prepared,{},set(),set())
    assert selected['name']==names[-1]

def test_custom_priority_and_unknown_excluded():
    s=settings();s['priority']['tier_order']=[3,2,1,0]
    rows=[row('仙林体育馆3楼6号场'),row('仙林训练馆2楼1号场','2'),row('三牌楼1号场','3')]
    p,prepared=daily.build_plan(s,rows,DATE)
    assert len(prepared)==2
    assert p['targets'][0]['courts'][0]=='仙林训练馆2楼1号场'

def test_no_wrong_date_identifiers():
    with pytest.raises(SafeError):daily.build_plan(settings(),[row('仙林体育馆3楼6号场')],'2030-01-02')

def test_more_than_thirty_approved_candidates():
    rows=[row(f'仙林体育馆1楼{i}号场',str(i)) for i in range(1,41)]
    p,_=daily.build_plan(settings(),rows,DATE)
    assert len(p['targets'][0]['courts'])==40

def test_migrate_once_merge_quantity(tmp_path):
    old=tmp_path/'plan.json';dest=tmp_path/'daily.json'
    old.write_text(json.dumps({'targets':[dict(start='19:00',end='20:00',quantity=1)]*2}))
    s=daily.load_settings(dest,old)
    assert s['windows']==[dict(start='19:00',end='20:00',quantity=2)]
    old.write_text('{}')
    assert daily.load_settings(dest,old)==s

@pytest.mark.parametrize('exception',[None,KeyboardInterrupt,SafeError])
def test_gate_to_post_no_get_or_disk_write(tmp_path,exception):
    rows=[row('仙林体育馆3楼6号场')];p,_=daily.build_plan(settings(),rows,DATE)
    events=[];original=booking.atomic
    def save(*args):events.append('disk');return original(*args)
    class Client:
        timings=[]
        def slots(self,date):events.append('GET');raise AssertionError('unexpected GET')
        def submit(self,s):events.append('POST');return dict(state='success',reason='accepted')
    def gate():
        events.append('gate')
        if exception:raise exception()
        return {'test':True}
    with patch('booking.atomic',side_effect=save):
        if exception:
            with pytest.raises(exception):booking.run(p,Client(),tmp_path,lambda _:None,prepared=rows,before_first_submit=gate)
            assert 'POST' not in events
            assert not booking.existing_keys(tmp_path)
        else:
            result=booking.run(p,Client(),tmp_path,lambda _:None,prepared=rows,before_first_submit=gate)
            assert result['state']=='complete'
            assert events[events.index('gate')+1]=='POST'
    assert 'GET' not in events

def test_after_noon_never_opens_client():
    with patch('daily.now_cn',return_value=datetime(2030,1,1,13,tzinfo=CN)),patch('daily.load_settings',return_value=settings()),patch('daily.Client') as client:
        assert daily.execute(confirm=lambda _: '1') is None
        client.assert_not_called()

def test_prefetch_wait_stops_at_minus_sixty():
    fire=datetime(2030,1,1,12,tzinfo=CN);elapsed=[0.0]
    def sleep(n):elapsed[0]+=n
    daily.wait_until_prefetch(fire,now=lambda:fire-timedelta(seconds=120-elapsed[0]),monotonic=lambda:elapsed[0],sleep=sleep)
    assert elapsed[0]==60

def test_compatibility_bootstrap_only_delegates():
    with patch('launcher.main',return_value=0) as launch, patch('subprocess.run') as run:
        assert bootstrap.main()==0
        launch.assert_called_once()
        run.assert_not_called()


def test_daily_scheduled_flow_uses_today_ids_before_gate(tmp_path):
    fire=datetime(2030,1,1,12,tzinfo=CN)
    rows=[row('仙林体育馆3楼6号场',available=False)]
    events=[]
    class Client:
        claims={'exp':fire.timestamp()+3600}
        timings=[]
        def types(self):events.append('types')
        def slots(self,date):
            assert date==DATE;events.append('slots');return rows
        def close(self):events.append('close')
    def run(plan,client,**kw):
        assert kw['prepared'][0]['id']==rows[0]['id']
        assert kw['prepared'][0]['available'] is True
        kw['before_first_submit']()
        return {'state':'complete','completed':{'daily-1':1}}
    def wait(fire,warmup):events.append('gate');return {}
    (tmp_path/'private').mkdir()
    with patch('daily.ROOT',tmp_path),patch('daily.load_settings',return_value=settings()),patch('daily.now_cn',return_value=fire-timedelta(seconds=55)),patch('daily.Client',Client),patch('daily.wait_until_prefetch'),patch('daily.wait_for_start',side_effect=wait),patch('daily.run',side_effect=run):
        daily.execute(confirm=lambda _: '1')
    assert events==['types','slots','gate','close']
