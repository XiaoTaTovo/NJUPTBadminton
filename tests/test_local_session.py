import base64
import json
import sys
import time
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import local_capture_addon as addon
import local_session as launch

def token(exp):
    payload = base64.urlsafe_b64encode(json.dumps({'exp': exp}).encode()).decode().rstrip('=')
    return 'header.' + payload + '.signature'

class LocalSessionTests(unittest.TestCase):
    def test_invalid_expired_token(self):
        for raw in ('', 'abc', token(time.time()-1), token(True), token(float('nan'))):
            self.assertIsNone(addon.token_exp(raw))

    def test_domain_path_guard_and_save(self):
        with tempfile.TemporaryDirectory() as td, patch.dict('os.environ', {'NJUPT_CAPTURE_DIR': td}):
            req = SimpleNamespace(pretty_host='example.com', path='/mini_program/v4/venue/user/types', headers={'token': token(time.time()+3600)})
            capture = addon.Capture()
            capture.request(SimpleNamespace(request=req))
            self.assertFalse((Path(td)/'session.json').exists())
            req.pretty_host = addon.HOST
            req.path = '/other'
            capture.request(SimpleNamespace(request=req))
            self.assertFalse((Path(td)/'session.json').exists())
            req.path = '/mini_program/v4/venue/user/types'
            capture.request(SimpleNamespace(request=req))
            self.assertEqual(json.loads((Path(td)/'session.json').read_text())['token'], req.headers['token'])

    def test_restore_preserves_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            backup = Path(td)/'backup.json'
            values = {'ProxyEnable': [1, 4], 'ProxyServer': ['127.0.0.1:7897', 1], 'AutoConfigURL': None}
            backup.write_text(json.dumps(values))
            with patch.object(launch, 'BACKUP', backup), patch.object(launch, 'apply') as apply:
                launch.restore()
                apply.assert_called_once_with(values)
                self.assertFalse(backup.exists())

    def test_restore_keeps_backup_on_failure(self):
        with tempfile.TemporaryDirectory() as td:
            backup = Path(td)/'backup.json'
            backup.write_text('{}')
            with patch.object(launch, 'BACKUP', backup), patch.object(launch, 'apply', side_effect=OSError):
                with self.assertRaises(OSError):
                    launch.restore()
                self.assertTrue(backup.exists())

if __name__ == '__main__':
    unittest.main()
