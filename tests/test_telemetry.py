import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from api import Client

class TelemetryTests(unittest.TestCase):
    def client(self):
        c=object.__new__(Client)
        c.last_request=-float('inf');c.timings=[];c.http=MagicMock()
        return c
    def test_failure_recorded_without_secrets(self):
        c=self.client();c.http.request.side_effect=TimeoutError('secret-text')
        with tempfile.TemporaryDirectory() as td, patch('api.ROOT',Path(td)):
            (Path(td)/'private').mkdir()
            with self.assertRaises(TimeoutError):c.request('POST','/venue/user/booking/pomelo/v2/123',data={'token':'secret-text'})
            raw=(Path(td)/'private/request-events.jsonl').read_text()
            self.assertNotIn('secret-text',raw)
            self.assertNotIn('123',raw.split('endpoint')[0])
            self.assertEqual(json.loads(raw)['error_type'],'TimeoutError')
    def test_retry_after_and_endpoint(self):
        c=self.client();c.http.request.return_value=MagicMock(status_code=429,headers={'Retry-After':'60'})
        with tempfile.TemporaryDirectory() as td,patch('api.ROOT',Path(td)):
            (Path(td)/'private').mkdir()
            c.request('GET','/venue/user/types')
            self.assertEqual(c.timings[0]['retry_after_seconds'],60)
            self.assertEqual(c.timings[0]['endpoint'],'types')
            c.http.request.assert_called_once()
    def test_logging_failure_does_not_hide_response(self):
        c=self.client();response=MagicMock(status_code=200,headers={});c.http.request.return_value=response
        with tempfile.TemporaryDirectory() as td,patch('api.ROOT',Path(td)):
            self.assertIs(c.request('POST','/venue/user/booking/pomelo/v2/123'),response)

if __name__=='__main__':unittest.main()
