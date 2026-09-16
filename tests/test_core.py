import unittest
from njpt_booking.core import *
class T(unittest.TestCase):
 def test_cal(self):
  c=calibrate([Sample(i,i+.05,i+.027) for i in range(5)]);self.assertAlmostEqual(c.offset_ms,2)
 def test_select(self):
  t={'start':'18:00','end':'19:00','venues':[{'name':'A','courts':['1','2']}]};self.assertEqual(select_candidates(t,{('A','2','18:00-19:00')}),[('A','2','18:00-19:00')])
if __name__=='__main__':unittest.main()
