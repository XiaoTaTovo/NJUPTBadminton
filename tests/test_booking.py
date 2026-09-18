import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from api import Client, SafeError
from booking import choose, validate, run, existing_keys, slot_key, exclusive

DATE='2030-01-01'
def slot(name='A', start='18:00', sid='1', available=True):
    return {'id':sid,'date':DATE,'name':name,'start':start,'end':'19:00' if start=='18:00' else '20:00','available':available}
def group(name='g', courts=None, quantity=1, start='18:00'):
    return {'id':name,'date':DATE,'start':start,'end':'19:00' if start=='18:00' else '20:00','quantity':quantity,'courts':courts or ['A','B']}
def plan(groups=None):
    gs=groups or [group()]
    return {'version':2,'targets':gs,'max_orders':sum(g['quantity'] for g in gs)}

class Fake:
    timings=[]
    def __init__(self, rows, outcomes=None):
        self.rows=rows; self.calls=[]; self.outcomes=list(outcomes or [])
    def slots(self,date): return copy.deepcopy(self.rows)
    def submit(self,s):
        self.calls.append(s)
        return self.outcomes.pop(0) if self.outcomes else {'state':'success','reason':'server_reported_success'}

class EngineTests(unittest.TestCase):
    def test_matching_preserves_scarce_group(self):
        gs=[group('flex'),group('scarce',['A'])]
        g,s=choose(gs,[slot(),slot('B',sid='2')],{},set(),set())
        self.assertEqual(g['id'],'scarce'); self.assertEqual(s['name'],'A')
    def test_same_group_respects_candidate_priority(self):
        gs=[group(quantity=2)]
        g,s=choose(gs,[slot(),slot('B',sid='2')],{},set(),set())
        self.assertEqual(s['name'],'A')
    def test_only_exact_time_date_names(self):
        self.assertIsNone(choose([group()], [slot('wrong')],{},set(),set()))
        self.assertIsNone(choose([group()], [slot(available=False)],{},set(),set()))
    def test_duplicate_same_window_same_candidates_rejected(self):
        p=plan([group('a',['A','B']),group('b',['B','A'])])
        with self.assertRaises(SafeError): validate(p)

    def test_bad_plans(self):
        for change in ({'max_orders':0},{'version':1},{'max_orders':True}):
            p=plan(); p.update(change)
            with self.assertRaises(SafeError): validate(p)
        p=plan();p['targets'][0]['courts']=['A','A']
        with self.assertRaises(SafeError):validate(p)
    def test_same_and_different_time_three_orders(self):
        p=plan([group(quantity=2),group('later',['C'],1,'19:00')])
        f=Fake([slot(),slot('B',sid='2'),slot('C','19:00','3')])
        with tempfile.TemporaryDirectory() as td:
            r=run(p,f,td,lambda _:None,max_reads=5)
            self.assertEqual(r['state'],'complete');self.assertEqual(len(f.calls),3)
            self.assertEqual(len(set(slot_key(x) for x in f.calls)),3)
    def test_unknown_stops_and_blocks_restart(self):
        f=Fake([slot(),slot('B',sid='2')],[{'state':'unknown','reason':'timeout'}])
        with tempfile.TemporaryDirectory() as td:
            r=run(plan(),f,td,lambda _:None,max_reads=3)
            self.assertEqual(r['state'],'unknown');self.assertEqual(len(f.calls),1)
            with self.assertRaises(SafeError):run(plan(),f,td,lambda _:None)
    def test_sold_out_switches_not_repeats(self):
        f=Fake([slot(),slot('B',sid='2')],[{'state':'sold_out','reason':'taken'}])
        with tempfile.TemporaryDirectory() as td:
            r=run(plan(),f,td,lambda _:None,max_reads=3)
            self.assertEqual(r['state'],'complete');self.assertEqual([s['name'] for s in f.calls],['A','B'])
    def test_refreshes_after_sold_out(self):
        class Changing(Fake):
            def slots(self,date):
                if not self.calls: return [slot()]
                return [slot(available=False),slot('B',sid='2')]
        f=Changing([], [{'state':'sold_out','reason':'taken'}])
        with tempfile.TemporaryDirectory() as td:
            r=run(plan(),f,td,lambda _:None,max_reads=3)
            self.assertEqual(r['state'],'complete')
            self.assertEqual([s['name'] for s in f.calls],['A','B'])
    def test_interrupt_keeps_submission_intent(self):
        class Interrupt(Fake):
            def submit(self,s): raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(KeyboardInterrupt):run(plan(),Interrupt([slot()]),td,lambda _:None,max_reads=1)
            with self.assertRaises(SafeError):existing_keys(Path(td))
    def test_prior_success_reserves_scarce_group(self):
        f=Fake([slot(),slot('B',sid='2')])
        with tempfile.TemporaryDirectory() as td:
            run(plan([group('first',['A'])]),f,td,lambda _:None,max_reads=1)
            r=run(plan([group('flex'),group('scarce',['A'])]),f,td,lambda _:None,max_reads=3)
            self.assertEqual(r['state'],'complete')
            self.assertEqual(len(f.calls),2)
    def test_limit_stops_retains_success(self):
        f=Fake([slot(),slot('B',sid='2')],[{'state':'success'}, {'state':'blocked','reason':'account_or_rate_limit'}])
        with tempfile.TemporaryDirectory() as td:
            r=run(plan([group(quantity=2)]),f,td,lambda _:None,max_reads=3)
            self.assertEqual(r['state'],'blocked');self.assertEqual(r['completed']['g'],1)
    def test_restart_does_not_rebook(self):
        f=Fake([slot()])
        with tempfile.TemporaryDirectory() as td:
            run(plan(),f,td,lambda _:None,max_reads=2)
            run(plan(),f,td,lambda _:None,max_reads=2)
            self.assertEqual(len(f.calls),1)
    def test_cancel_confirmation_only_clears_old_success(self):
        import time
        f=Fake([slot()])
        with tempfile.TemporaryDirectory() as td:
            run(plan(),f,td,lambda _:None,max_reads=1)
            p=Path(td)/'cancelled-confirmations.json'
            p.write_text(json.dumps({slot_key(slot()):time.time()}))
            self.assertNotIn(slot_key(slot()),existing_keys(Path(td)))
            run(plan(),f,td,lambda _:None,max_reads=1)
            self.assertIn(slot_key(slot()),existing_keys(Path(td)))
            run(plan(),f,td,lambda _:None,max_reads=1)
            self.assertEqual(len(f.calls),2)
    def test_lock_rejects_concurrency(self):
        with tempfile.TemporaryDirectory() as td:
            with exclusive(Path(td)):
                with self.assertRaises(SafeError):
                    with exclusive(Path(td)):pass
    def test_stale_available_flag_not_duplicate(self):
        f=Fake([slot()])
        with tempfile.TemporaryDirectory() as td,patch('booking.time.sleep'):
            r=run(plan([group(quantity=2)]),f,td,lambda _:None,max_reads=3)
            self.assertEqual(len(f.calls),1); self.assertEqual(r['state'],'partial_or_exhausted')
    def test_max_attempts(self):
        f=Fake([slot(str(i),sid=str(i)) for i in range(5)],[{'state':'sold_out'}]*5)
        with tempfile.TemporaryDirectory() as td,patch('booking.time.sleep'):
            run(plan([group(courts=[str(i) for i in range(5)])]),f,td,lambda _:None,max_reads=8)
            self.assertEqual(len(f.calls),3)

class ResponseTests(unittest.TestCase):
    def client(self,body=None,status=200):
        c=object.__new__(Client);c.claims={'studentId':'synthetic-test-identity'}
        c.request=MagicMock(return_value=MagicMock(status_code=status,json=lambda:body))
        return c
    def test_success_requires_matching_details(self):
        b={'success':True,'data':{'order':{'orderId':'synthetic-order'},'detail':{'orderId':'synthetic-order','stadiumId':'1','stadiumName':'A','startTime':DATE+' 18:00:00','endTime':DATE+' 19:00:00','price':8,'status':1}}}
        c=self.client(b);self.assertEqual(c.submit(slot())['state'],'success')
        b['data']['detail']['stadiumName']='wrong'
        self.assertEqual(c.submit(slot())['state'],'unknown')
    def test_timeout_and_500_not_retried(self):
        c=self.client(status=500);self.assertEqual(c.submit(slot())['state'],'unknown');c.request.assert_called_once()
        c=self.client();c.request.side_effect=TimeoutError
        self.assertEqual(c.submit(slot())['state'],'unknown');c.request.assert_called_once()
    def test_classification(self):
        for body,state in [({'success':False,'errMsg':'已有未支付订单，预约超限'},'blocked'),({'success':False,'errMsg':'场地已被预约'},'sold_out'),({'success':False,'errMsg':'陌生错误'},'rejected'),({},'unknown')]:
            self.assertEqual(self.client(body).submit(slot())['state'],state)

if __name__=='__main__':unittest.main()
