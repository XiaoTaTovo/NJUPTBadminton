"""Read-only Windows time measurement; never changes the operating-system clock."""
import re
import statistics
import subprocess
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from api import CN, SafeError

class ReferenceClock:
    def __init__(self, offset, samples, sources):
        self.offset = offset
        self.anchor_mono = time.monotonic()
        self.anchor_utc = time.time() + offset
        self.info = {'offset_seconds': round(offset,6), 'sample_count':len(samples),
                     'sample_spread_seconds':round(max(samples)-min(samples),6),
                     'sources':sources, 'confidence':'medium',
                     'precision_note':'NTP观测估计，样本离散不是误差上界；不保证毫秒精度',
                     'measured_at_local':datetime.now(CN).isoformat()}

    def timestamp(self):
        return self.anchor_utc + time.monotonic() - self.anchor_mono

    def now(self):
        return datetime.fromtimestamp(self.timestamp(),CN)

    def age(self):
        return time.monotonic()-self.anchor_mono

    def check_gateway(self, event):
        """Date corroboration only, not the authority for sub-second correction."""
        if not event or not event.get('http_date'):
            raise SafeError(code='GATEWAY_TIME_CONFLICT')
        try:
            server=parsedate_to_datetime(event['http_date']).timestamp()
            corrected_send=event.get('corrected_send',event['local_send']+self.offset)
            rtt=event['rtt_ms']/1000
        except (TypeError,ValueError,KeyError):
            raise SafeError(code='GATEWAY_TIME_CONFLICT') from None
        # Date is whole seconds and may originate at a gateway. Permit quantization/network,
        # but stop on a material disagreement instead of silently using a conflicting clock.
        if server > corrected_send+rtt+1 or server+1 < corrected_send-1:
            raise SafeError(code='GATEWAY_TIME_CONFLICT')
        self.info['gateway_corroborated']=True


def parse_offsets(text):
    # w32tm's signed offset suffix is locale independent except for the decimal separator.
    return [float(x.replace(',','.')) for x in re.findall(r',\s*([+-]\d+[.,]\d+)s\s*$',text,re.M)]


def calibrate(*, deadline=None, observer=None, round_number=1, runner=None, monotonic=None):
    """Bounded read-only sampling; deadline is an absolute monotonic deadline."""
    runner=runner or subprocess.run
    mono=monotonic or time.monotonic
    emit=observer or (lambda *args, **kwargs: None)
    last=SafeError(code='CLOCK_SAMPLES_INSUFFICIENT')
    for host in ('time.windows.com','time.cloudflare.com'):
        remaining=10 if deadline is None else min(10,deadline-mono())
        if remaining<=0: break
        started=mono()
        details={'source':host,'round':round_number,'remaining_seconds':remaining}
        emit('clock_sample_started',details)
        values=[]
        try:
            result=runner(['w32tm','/stripchart','/computer:'+host,'/dataonly','/samples:3','/period:1'],
                capture_output=True,text=True,errors='replace',timeout=remaining,
                creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            values=parse_offsets(result.stdout)
            if type(result.returncode) is int: details['return_code']=result.returncode
            if deadline is not None and mono()>=deadline:
                raise SafeError(code='CLOCK_COMMAND_TIMEOUT')
            if len(values)<2: raise SafeError(code='CLOCK_SAMPLES_INSUFFICIENT')
            offset=statistics.median(values)
            spread=max(values)-min(values)
            details.update(sample_count=len(values),offset_seconds=offset,spread_seconds=spread)
            if abs(offset)>60: raise SafeError(code='CLOCK_OFFSET_OUT_OF_RANGE')
            if spread>.25: raise SafeError(code='CLOCK_SAMPLE_SPREAD_HIGH')
        except subprocess.TimeoutExpired:
            last=SafeError(code='CLOCK_COMMAND_TIMEOUT')
        except OSError:
            last=SafeError(code='CLOCK_SAMPLES_INSUFFICIENT')
        except SafeError as exc:
            last=exc
        else:
            details['elapsed_ms']=(mono()-started)*1000
            emit('clock_sample_completed',details)
            return ReferenceClock(offset,values,[host])
        details.update(sample_count=len(values),elapsed_ms=(mono()-started)*1000,last_error_code=last.code)
        emit('clock_sample_failed',details)
    raise last


def refresh_clock(deadline, *, observer=None, calibrator=None, monotonic=None, sleep=None):
    mono=monotonic or time.monotonic
    sleep=sleep or time.sleep
    calibrator=calibrator or calibrate
    emit=observer or (lambda *args, **kwargs: None)
    last='CLOCK_SAMPLES_INSUFFICIENT'
    for round_number in range(1,4):
        if mono()>=deadline: break
        try:
            clock=calibrator(deadline=deadline,observer=emit,round_number=round_number)
            if mono()>=deadline: raise SafeError(code='CLOCK_COMMAND_TIMEOUT')
            return clock
        except SafeError as exc:
            last=exc.code
        if round_number<3 and deadline-mono()>5:
            emit('clock_retry_wait',{'round':round_number,'retry_in_seconds':5,
                                    'remaining_seconds':deadline-mono(),'last_error_code':last})
            sleep(5)
        else: break
    raise SafeError(code='CLOCK_REFRESH_EXHAUSTED',details={'last_error_code':last,'round':round_number})
