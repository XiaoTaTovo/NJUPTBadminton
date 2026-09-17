"""Replace only this project's assistant processes; leave unrelated Python/Clash alone."""
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
import psutil

ROOT = Path(__file__).resolve().parent


def owned_kind(command, cwd, root=ROOT):
    if not command:
        return None
    exe = Path(command[0]).name.lower()
    def matches(arg, filename):
        try:
            p=Path(arg)
            if not p.is_absolute():
                if not cwd: return False
                p=Path(cwd)/p
            return p.resolve()==(root/filename).resolve()
        except (OSError, ValueError):
            return False
    if exe in ('python.exe','pythonw.exe','python','pythonw'):
        # Inspect the script position, not arbitrary argument text or python -c strings.
        args=command[1:]
        while args and args[0] in ('-u','-B','-I'):
            args=args[1:]
        if args and matches(args[0],'app.py'):
            return 'app'
        if args and matches(args[0],'local_session.py') and 'capture' in args[1:]:
            return 'capture'
    if exe in ('mitmdump.exe','mitmdump'):
        for i,arg in enumerate(command[:-1]):
            if arg in ('-s','--scripts') and matches(command[i+1],'local_capture_addon.py'):
                return 'capture'
    return None


def owned_processes():
    found=[]
    for p in psutil.process_iter(['name']):
        if p.pid==os.getpid(): continue
        if (p.info.get('name') or '').lower() not in ('python.exe','pythonw.exe','python','pythonw','mitmdump.exe','mitmdump'):
            continue
        try:
            kind=owned_kind(p.cmdline(),p.cwd())
            if kind:
                found.append((p,kind,p.create_time()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


def stop_owned():
    count=0
    # Stop producers before their proxy children; rescan catches a child spawned during shutdown.
    for _ in range(3):
        found=owned_processes()
        if not found: return count
        found.sort(key=lambda x:x[1]!='app')
        for p,kind,created in found:
            try:
                if p.create_time()!=created or owned_kind(p.cmdline(),p.cwd())!=kind:
                    continue
                p.terminate()
                try:
                    p.wait(timeout=5)
                except psutil.TimeoutExpired:
                    raise RuntimeError('旧程序未退出；为避免重复运行，已取消启动。') from None
                count+=1
            except psutil.NoSuchProcess:
                pass
    if owned_processes():
        raise RuntimeError('仍检测到旧助手进程，停止启动。')
    return count


@contextmanager
def startup_lock():
    import msvcrt
    path=ROOT/'private/startup.lock'
    with path.open('a+b') as f:
        f.seek(0);f.write(b'0');f.flush();f.seek(0)
        deadline=time.monotonic()+20
        while True:
            try:
                msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
                break
            except OSError:
                if time.monotonic()>deadline:
                    raise RuntimeError('其他启动器正在切换，请稍后重试。') from None
                time.sleep(.1)
        try: yield
        finally:
            f.seek(0);msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


def main():
    from local_session import BACKUP, restore
    (ROOT/'private').mkdir(exist_ok=True)
    with startup_lock():
        print('正在检查本项目旧进程…',flush=True)
        count=stop_owned()
        if count:
            print(f'已关闭本项目旧进程 {count} 个。若曾提交订单，请先核对；未知状态不会清空。',flush=True)
        if BACKUP.exists():
            restore()
        print('正在打开菜单…',flush=True)
        proc=subprocess.Popen([sys.executable,str(ROOT/'app.py')],cwd=ROOT)
    code=proc.wait()
    # Replacement on Windows terminates the app: exit old launcher without leaving a pause prompt.
    return 0 if code in (0,1,15) else code


if __name__=='__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('启动已由用户中止；再次双击即可重开。')
        sys.exit(0)
    except Exception as e:
        print('启动失败：'+(str(e) if isinstance(e,RuntimeError) else type(e).__name__))
        sys.exit(2)
