import unittest
from datetime import datetime
from unittest.mock import MagicMock,patch
import app
from api import CN
from booking import choose
from preferences import tier,ordered,court_number

class PreferenceTests(unittest.TestCase):
    def test_four_tiers(self):
        cases={'仙林体育馆3F羽毛球馆1号场地':0,'仙林体育馆3楼羽毛球8号场地':0,'仙林体育馆三楼羽毛球九号场地':1,'仙林体育馆３Ｆ羽毛球１２号场地':1,'仙林体育馆主馆1F羽毛球12号场地':2,'仙林训练馆2F羽毛球1号场地':3,'三牌楼羽毛球馆一号场地':4,'仙林体育馆3F羽毛球馆13号场地':4,'仙林体育馆3F羽毛球场':4}
        for name,rank in cases.items():
            with self.subTest(name=name):self.assertEqual(tier(name),rank)
    def test_no_floor_digit_confusion(self):
        self.assertIsNone(court_number('仙林体育馆3楼羽毛球场'))
        self.assertEqual(court_number('仙林体育馆3F羽毛球馆十一号场地'),11)
    def test_same_tier_stable_no_1f_doubles_bias(self):
        names=['仙林体育馆1F羽毛球12号场地','仙林体育馆1F羽毛球1号场地']
        self.assertEqual(ordered(names),names)
    def test_runtime_reorders_existing_plan_and_never_expands(self):
        names=['仙林训练馆2F羽毛球1号场地','仙林体育馆1F羽毛球1号场地','仙林体育馆3F羽毛球9号场地','仙林体育馆3F羽毛球8号场地']
        g={'id':'g','date':'2030-01-01','start':'18:00','end':'19:00','quantity':1,'courts':names}
        rows=[dict(id=str(i),name=n,date=g['date'],start=g['start'],end=g['end'],available=True) for i,n in enumerate(names)]
        self.assertEqual(choose([g],rows,{},set(),set())[1]['name'],names[3])
        rows[3]['available']=False
        self.assertEqual(choose([g],rows,{},set(),set())[1]['name'],names[2])
        g['courts']=[names[0]]
        self.assertEqual(choose([g],rows,{},set(),set())[1]['name'],names[0])

class WizardHelpTests(unittest.TestCase):
    def rows(self):
        return [dict(name=f'仙林体育馆3F羽毛球馆{n}号场地',date='2030-01-01',start=start,end=end,available=True)
                for start,end in [('18:00','19:00'),('19:00','20:00')] for n in (1,2)]
    def run_wizard(self,answers):
        c=MagicMock();c.slots.return_value=self.rows()
        with patch('app.Client',return_value=c),patch('app.now_cn',return_value=datetime(2030,1,1,10,tzinfo=CN)),patch('builtins.input',side_effect=answers),patch('app.atomic') as save:
            app.wizard()
            c.submit.assert_not_called()
            return save.call_args.args[1]
    def test_two_groups_two_separate_time_choices(self):
        p=self.run_wizard(['','','2','1','1,2','1','2','1,2','1'])
        self.assertEqual([g['start'] for g in p['targets']],['18:00','19:00'])
        self.assertEqual(p['max_orders'],2)
    def test_same_time_two_courts_one_group(self):
        p=self.run_wizard(['','','1','1','1,2','2'])
        self.assertEqual(len(p['targets']),1)
        self.assertEqual(p['targets'][0]['quantity'],2)
    def test_two_time_numbers_reprompt(self):
        p=self.run_wizard(['','','1','1,2','1','1','1'])
        self.assertEqual(p['targets'][0]['start'],'18:00')
    def test_help_file_exists(self):
        with patch('builtins.print') as output:
            app.show_help()
        text=output.call_args.args[0]
        for word in ('时段序号','候选场地','所需数量','Retry-After','启动时间'):
            self.assertIn(word,text)

if __name__=='__main__':unittest.main()
