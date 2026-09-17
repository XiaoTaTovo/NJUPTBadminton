import tempfile
import time
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
from datetime import datetime
from api import CN
import app
import launcher

class UIAndLauncherTests(unittest.TestCase):
    def test_today_default(self):
        with patch('builtins.input',return_value=''),patch('app.now_cn',return_value=datetime(2026,9,17,10,tzinfo=CN)):
            self.assertEqual(app.choose_date(),'2026-09-17')
    def test_bad_number_reprompts(self):
        with patch('builtins.input',side_effect=['x','7','2']):
            self.assertEqual(app.number('test',1,2,1),2)
    def test_wizard_defaults_xianlin_numeric_times(self):
        from unittest.mock import MagicMock
        c=MagicMock();c.slots.return_value=[{'date':'2030-01-01','name':'仙林一号场地','start':'18:00','end':'19:00','available':True}]
        with patch('app.Client',return_value=c),patch('app.now_cn',return_value=datetime(2030,1,1,10,tzinfo=CN)),patch('builtins.input',side_effect=['','','','1','1','']),patch('app.atomic') as save:
            app.wizard()
            p=save.call_args.args[1]
            self.assertEqual(p['targets'][0]['courts'],['仙林一号场地'])
            self.assertEqual(p['targets'][0]['start'],'18:00')
    def test_process_scope(self):
        root=Path('D:/test/project')
        self.assertEqual(launcher.owned_kind(['python.exe',str(root/'app.py')],'D:/',root),'app')
        self.assertEqual(launcher.owned_kind(['python.exe','app.py'],str(root),root),'app')
        self.assertIsNone(launcher.owned_kind(['python.exe','-c',str(root/'app.py')],str(root),root))
        self.assertIsNone(launcher.owned_kind(['python.exe','D:/other/app.py'],str(root),root))
        self.assertIsNone(launcher.owned_kind(['clash.exe'],str(root),root))
        self.assertIsNone(launcher.owned_kind(['mitmdump.exe','--listen-port','8080'],str(root),root))
        self.assertEqual(launcher.owned_kind(['mitmdump.exe','-s',str(root/'local_capture_addon.py')],str(root),root),'capture')
    def test_real_two_launches_only_replace_test_app(self):
        # Actual OS processes, isolated temporary project; no network, booking, or registry access.
        import psutil
        processes=[]
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            (root/'launcher.py').write_text(Path(launcher.__file__).read_text(encoding='utf-8'),encoding='utf-8')
            (root/'local_session.py').write_text("from pathlib import Path\nROOT=Path(__file__).parent\nBACKUP=ROOT/'never'\ndef protect_dir(): (ROOT/'private').mkdir(exist_ok=True)\ndef restore(): pass\ndef connection(port): return False\n")
            (root/'app.py').write_text("import os,time\nfrom pathlib import Path\nPath(__file__).with_name('pid').write_text(str(os.getpid()))\nwhile True: time.sleep(.1)\n")
            def wait_pid(old=None):
                for _ in range(100):
                    if (root/'pid').exists():
                        try: pid=int((root/'pid').read_text())
                        except ValueError: continue
                        if pid!=old:return pid
                    time.sleep(.05)
                self.fail('test app did not launch')
            try:
                first=subprocess.Popen([sys.executable,str(root/'launcher.py')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                processes.append(first);pid1=wait_pid();child1=psutil.Process(pid1)
                second=subprocess.Popen([sys.executable,str(root/'launcher.py')],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                processes.append(second);pid2=wait_pid(pid1);child2=psutil.Process(pid2)
                self.assertFalse(child1.is_running())
                self.assertTrue(child2.is_running())
                self.assertEqual(first.wait(timeout=5),0)
                child2.terminate();child2.wait(timeout=5)
                self.assertEqual(second.wait(timeout=5),0)
            finally:
                for proc in processes:
                    if proc.poll() is None:
                        for child in psutil.Process(proc.pid).children(recursive=True):
                            try:child.terminate();child.wait(timeout=5)
                            except psutil.NoSuchProcess:pass
                        proc.terminate();proc.wait(timeout=5)

if __name__=='__main__':unittest.main()
