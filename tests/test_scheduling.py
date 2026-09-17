import unittest
from datetime import datetime,timedelta
from api import CN,SafeError,classify_rejection
from scheduling import wait_for_start

class SchedulingTests(unittest.TestCase):
    def clock(self):
        ticks=[0.0];base=datetime(2030,1,1,11,59,tzinfo=CN)
        return ticks,base,lambda:base+timedelta(seconds=ticks[0]),lambda:ticks[0],lambda d:ticks.__setitem__(0,ticks[0]+d)
    def test_one_warmup_at_minus20_no_early_trigger(self):
        t,b,n,m,s=self.clock();calls=[]
        r=wait_for_start(b+timedelta(seconds=60),lambda:calls.append(t[0]),n,m,s)
        self.assertEqual(calls,[40]);self.assertAlmostEqual(t[0],60)
        self.assertEqual(r['warmup'],'completed')
    def test_near_start_no_warmup(self):
        t,b,n,m,s=self.clock();calls=[]
        wait_for_start(b+timedelta(seconds=5),lambda:calls.append(1),n,m,s)
        self.assertEqual(calls,[]);self.assertAlmostEqual(t[0],5)
    def test_slow_warmup_no_late_booking(self):
        t,b,n,m,s=self.clock()
        with self.assertRaises(SafeError):wait_for_start(b+timedelta(seconds=60),lambda:s(30),n,m,s)
    def test_warmup_error_propagated(self):
        t,b,n,m,s=self.clock()
        def fail():raise SafeError('authentication')
        with self.assertRaises(SafeError):wait_for_start(b+timedelta(seconds=60),fail,n,m,s)
    def test_ambiguous_rejection_not_treated_as_sold_out(self):
        for text in ('系统不可用，请稍后','预约失败','服务不可用','错误时间段'):
            self.assertEqual(classify_rejection(None,text)['category'],'server_rejected_unknown')
    def test_sensitive_error_code_not_logged(self):
        self.assertIsNone(classify_rejection({'token':'secret'},'系统错误')['err_code'])
    def test_explicit_sold_out_still_recognized(self):
        self.assertEqual(classify_rejection(123,'场地已被预约')['category'],'sold_out')

if __name__=='__main__':unittest.main()
