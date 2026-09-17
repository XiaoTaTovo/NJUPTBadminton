"""Stdlib-only environment bootstrap: avoid uv sync on every launch."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parent
STAMP=ROOT/'.venv/booking-dependencies.json'

def main():
    digest=hashlib.sha256(b''.join((ROOT/n).read_bytes() for n in ('pyproject.toml','uv.lock'))).hexdigest()
    try: previous=json.loads(STAMP.read_text()).get('digest')
    except (OSError,ValueError): previous=None
    ready=all(importlib.util.find_spec(n) for n in ('requests','yaml','psutil','mitmproxy'))
    if previous!=digest or not ready:
        print('首次启动或依赖有变化：正在同步项目uv环境…',flush=True)
        subprocess.run(['uv','sync','--locked','--quiet'],cwd=ROOT,check=True)
        STAMP.write_text(json.dumps({'digest':digest}),encoding='utf-8')
    # Separate process so updated imports are not cached in the bootstrap interpreter.
    return subprocess.call([sys.executable,str(ROOT/'launcher.py')],cwd=ROOT)

if __name__=='__main__':
    try: sys.exit(main())
    except KeyboardInterrupt: sys.exit(0)
    except Exception as e:
        print('环境启动失败：'+type(e).__name__+'。请在项目目录执行 uv sync --locked。')
        sys.exit(2)
