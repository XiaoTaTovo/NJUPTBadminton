"""Windows local capture launcher. No booking/payment endpoints."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import winreg

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'private'
BACKUP = DATA / 'proxy-backup.json'
TOKEN = DATA / 'session.json'
REPO = Path(r'D:\01_Workspace\12_Dev_Projects\GitHub_project\NJUPT_badminton_booking')
MITM = ROOT / '.venv/Scripts/mitmdump.exe'
if not MITM.exists():
    MITM = REPO / '.venv/Scripts/mitmdump.exe'
KEY = r'Software\Microsoft\Windows\CurrentVersion\Internet Settings'
NAMES = ('ProxyEnable', 'ProxyServer', 'ProxyOverride', 'AutoConfigURL')

def protect_dir():
    DATA.mkdir(exist_ok=True)
    sid = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True).strip().split(',')[-1].strip('"')
    subprocess.run(['icacls', str(DATA), '/inheritance:r', '/grant:r', f'*{sid}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], check=True, stdout=subprocess.DEVNULL)

def snapshot():
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY) as k:
        out = {}
        for name in NAMES:
            try:
                v, kind = winreg.QueryValueEx(k, name)
                out[name] = [v, kind]
            except FileNotFoundError:
                out[name] = None
        return out

def apply(values):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as k:
        for name, item in values.items():
            if item is None:
                try:
                    winreg.DeleteValue(k, name)
                except FileNotFoundError:
                    pass
            else:
                winreg.SetValueEx(k, name, 0, item[1], item[0])
    for option in (39, 37):
        ctypes.windll.wininet.InternetSetOptionW(None, option, None, 0)

def restore():
    if not BACKUP.exists():
        print('没有待恢复的代理快照。')
        return
    apply(json.loads(BACKUP.read_text(encoding='utf-8')))
    BACKUP.unlink()
    print('已恢复抓包前的系统代理。')

def connection(port):
    try:
        with socket.create_connection(('127.0.0.1', port), timeout=1):
            return True
    except OSError:
        return False

def status():
    if not TOKEN.exists():
        print('尚未捕获凭据。')
        return False
    from local_capture_addon import token_exp
    obj = json.loads(TOKEN.read_text(encoding='utf-8'))
    exp = token_exp(obj.get('token'))
    print('本地凭据存在；可解码且剩余超过五分钟：', bool(exp))
    print('注意：本地解析不验证签名，也不证明服务端仍接受该凭据。')
    return bool(exp)

def verify():
    if not status():
        return
    import requests
    token = json.loads(TOKEN.read_text(encoding='utf-8'))['token']
    with requests.Session() as s:
        s.trust_env = False
        r = s.get('https://wechat.njupt.edu.cn/mini_program/v4/venue/user/types', headers={'token': token},
                  proxies={'https': 'http://127.0.0.1:7897'}, timeout=15, allow_redirects=False)
        print('只读请求 HTTP 状态：', r.status_code)
        if r.status_code != 200:
            print('未验证成功；不输出响应正文。')
            return
        body = r.json()
        rows = body.get('data') if isinstance(body, dict) else None
        ok = isinstance(rows, list) and any(isinstance(x, dict) and '羽毛球' in str(x.get('name', '')) for x in rows)
        print('已读取羽毛球类型，服务端凭据验证通过。' if ok else '未读到羽毛球类型；不能判定凭据有效。')

def capture(seconds):
    if BACKUP.exists():
        raise RuntimeError('存在未恢复的代理快照，请先执行 restore。')
    if not MITM.exists() or not connection(7897):
        raise RuntimeError('项目 mitmdump 或 Clash 7897 不可用。')
    if connection(8080):
        raise RuntimeError('8080 已占用。请在之前手动启动代理的窗口按 Ctrl+C，再试。')
    if input('将临时修改系统代理并仅捕获本人南邮会话，结束后恢复。输入 CAPTURE 继续：') != 'CAPTURE':
        return
    protect_dir()
    before = snapshot()
    with BACKUP.open('x', encoding='utf-8') as f:
        json.dump(before, f)
    env = os.environ.copy()
    env['NJUPT_CAPTURE_DIR'] = str(DATA)
    started = time.time()
    proc = None
    try:
        proc = subprocess.Popen([str(MITM), '--listen-host', '127.0.0.1', '--listen-port', '8080',
            '--mode', 'upstream:http://127.0.0.1:7897', '--allow-hosts', r'^wechat\.njupt\.edu\.cn:443$',
            '--set', 'flow_detail=0', '--set', 'termlog_verbosity=error', '-s', str(ROOT / 'local_capture_addon.py')],
            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(50):
            if proc.poll() is not None:
                raise RuntimeError('代理进程启动失败。')
            if connection(8080):
                break
            time.sleep(.1)
        else:
            raise RuntimeError('代理启动超时。')
        apply({'ProxyEnable': [1, winreg.REG_DWORD], 'ProxyServer': ['127.0.0.1:8080', winreg.REG_SZ],
               'ProxyOverride': ['<local>', winreg.REG_SZ], 'AutoConfigURL': None})
        print('现在手动进入：企业微信 → 工作台 → 南邮小程序 → 体育场馆 → 羽毛球。不要提交预约。')
        print('等待最多五分钟。不要点关闭窗口；使用 Ctrl+C 可恢复代理并退出。')
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError('代理进程提前退出。')
            state = snapshot()
            if state['ProxyServer'] != ['127.0.0.1:8080', winreg.REG_SZ] or state['ProxyEnable'] != [1, winreg.REG_DWORD]:
                raise RuntimeError('系统代理被其他程序修改，已停止。请暂停 Clash 的系统代理自动接管。')
            if TOKEN.exists() and TOKEN.stat().st_mtime >= started:
                print('凭据已写入本机 private/session.json（不显示内容）。')
                return
            time.sleep(.5)
        print('未捕获新凭据。请检查 CA 信任和小程序是否遵循系统代理；不自动降低 TLS 安全。')
    finally:
        # Restore before terminating: preserve network even if process termination fails.
        try:
            restore()
        finally:
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()

def main():
    p = argparse.ArgumentParser()
    p.add_argument('command', choices=['capture', 'status', 'verify', 'restore'])
    a = p.parse_args()
    try:
        {'capture': lambda: capture(300), 'status': status, 'verify': verify, 'restore': restore}[a.command]()
    except KeyboardInterrupt:
        print('已中止。')
    except Exception as exc:
        # Never echo arbitrary exception text: HTTP errors can include credential-bearing data.
        if isinstance(exc, RuntimeError):
            print(str(exc))
        else:
            print('操作失败，错误类别：' + type(exc).__name__ + '。不输出可能包含凭据的详情。')
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
