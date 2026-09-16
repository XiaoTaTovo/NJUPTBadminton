"""Local-only capture addon. No flows or credential values are logged."""
import base64
import json
import math
import os
from pathlib import Path
import tempfile
import time

HOST = 'wechat.njupt.edu.cn'

def token_exp(token):
    try:
        if not isinstance(token, str) or len(token) > 32768 or token.count('.') != 2:
            return None
        raw = token.split('.')[1]
        obj = json.loads(base64.urlsafe_b64decode(raw + '=' * (-len(raw) % 4)))
        exp = obj.get('exp')
        if isinstance(exp, bool) or not isinstance(exp, (int, float)) or not math.isfinite(exp):
            return None
        return exp if exp > time.time() + 300 else None
    except (ValueError, TypeError, AttributeError):
        return None

class Capture:
    def request(self, flow):
        if flow.request.pretty_host != HOST or not flow.request.path.startswith('/mini_program/v4/venue/user/'):
            return
        token = flow.request.headers.get('token', '')
        exp = token_exp(token)
        if exp is None:
            return
        folder = Path(os.environ['NJUPT_CAPTURE_DIR'])
        target = folder / 'session.json'
        fd, name = tempfile.mkstemp(dir=folder, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump({'token': token, 'expires_at': exp, 'captured_at': time.time()}, f)
            os.replace(name, target)
        finally:
            if os.path.exists(name):
                os.unlink(name)

addons = [Capture()]
