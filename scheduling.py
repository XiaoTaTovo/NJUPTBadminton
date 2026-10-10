"""Bounded preflight: one read-only warmup, never an early booking."""
import time
from api import now_cn, SafeError


def wait_for_start(fire, warmup, now=now_cn, monotonic=time.monotonic, sleep=time.sleep, tick=None, spin_ms=0, defer_format=False):
    remaining=(fire-now()).total_seconds()
    if remaining<=0:
        raise SafeError('确认时启动时间已过，拒绝补跑。')
    deadline=monotonic()+remaining
    info={'scheduled_at':fire.isoformat(),'warmup':'disabled' if warmup is None else 'skipped_near_start'}
    if remaining>20 and warmup is not None:
        while deadline-monotonic()>20:
            sleep(min(1,deadline-monotonic()-20))
        if abs((fire-now()).total_seconds()-(deadline-monotonic()))>2:
            raise SafeError('等待期间系统时钟变化，请重新校准并启动。')
        info['warmup_started_at']=now().isoformat()
        warmup()  # Same session; exceptions stop rather than blindly booking with bad auth.
        info['warmup']='completed'
        info['warmup_completed_at']=now().isoformat()
    # Bounded final spin only with the real monotonic clock; injected clocks never busy-loop.
    spin=min(max(float(spin_ms),0),1.0)/1000 if monotonic is time.monotonic else 0
    while (left:=deadline-monotonic())>spin:
        sleep(min(.2,max(0,left-spin)))
        if tick is not None: tick()
    while monotonic()<deadline: pass
    fired_ns=time.perf_counter_ns()
    fired=now()
    drift=(fired-fire).total_seconds()
    if drift<0 or drift>3:
        raise SafeError('时钟变化或休眠导致错过窗口，拒绝提前提交或补跑。')
    info.update(fired_perf_ns=fired_ns,fired_epoch=fired.timestamp(),trigger_lateness_ms=round(drift*1000,3))
    if not defer_format: info['fired_at']=fired.isoformat()
    return info
