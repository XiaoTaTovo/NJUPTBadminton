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
            raise SafeError('缺少HTTP Date旁证，停止定时；没有改系统时间。')
        try:
            server=parsedate_to_datetime(event['http_date']).timestamp()
            corrected_send=event.get('corrected_send',event['local_send']+self.offset)
            rtt=event['rtt_ms']/1000
        except (TypeError,ValueError,KeyError):
            raise SafeError('时间旁证无法解析，停止定时。') from None
        # Date is whole seconds and may originate at a gateway. Permit quantization/network,
        # but stop on a material disagreement instead of silently using a conflicting clock.
        if server > corrected_send+rtt+1 or server+1 < corrected_send-1:
            raise SafeError('NTP参考时间与网关Date明显不一致，停止定时，请检查时间源。')
        self.info['gateway_corroborated']=True


def parse_offsets(text):
    # w32tm's signed offset suffix is locale independent except for the decimal separator.
    return [float(x.replace(',','.')) for x in re.findall(r',\s*([+-]\d+[.,]\d+)s\s*$',text,re.M)]


def calibrate():
    samples=[];sources=[]
    for host in ('time.windows.com','time.cloudflare.com'):
        try:
            result=subprocess.run(['w32tm','/stripchart','/computer:'+host,'/dataonly','/samples:3','/period:1'],
                capture_output=True,text=True,errors='replace',timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW)
            values=parse_offsets(result.stdout)
        except (OSError,subprocess.TimeoutExpired):
            values=[]
        if len(values)>=2:
            samples.extend(values);sources.append(host)
            break  # bounded fallback only; no continuous NTP polling
    if len(samples)<2:
        raise SafeError('无法取得至少2个只读校时样本，已停止定时；检查UDP123连接。不会悄悄按未校准本机时间抢场。')
    offset=statistics.median(samples)
    if abs(offset)>60 or max(samples)-min(samples)>.25:
        raise SafeError('校时偏差过大或样本抖动超过250ms，停止定时，请先人工检查系统时钟。')
    return ReferenceClock(offset,samples,sources)
