"""Time samples are estimates, never millisecond guarantees from HTTP Date."""
from dataclasses import dataclass
import math
from statistics import median

@dataclass(frozen=True)
class Sample:
    local_send: float
    local_recv: float
    server_epoch: float
    # HTTP Date only has second granularity; use monotonic RTT to derive local_recv.
    resolution_seconds: float = 1.0
    @property
    def rtt_ms(self):
        return (self.local_recv-self.local_send)*1000
    @property
    def offset_ms(self):
        return (self.server_epoch-(self.local_send+self.local_recv)/2)*1000

@dataclass(frozen=True)
class Calibration:
    offset_ms: float
    rtt_ms: float
    confidence: str
    uncertainty_ms: float

def calibrate(samples):
    xs=list(samples)
    if not xs or any(not all(math.isfinite(v) for v in (x.local_send,x.local_recv,x.server_epoch,x.resolution_seconds)) or x.local_recv<x.local_send or x.resolution_seconds<=0 for x in xs):
        raise ValueError('时间样本为空或非法')
    offsets=[x.offset_ms for x in xs]
    rtts=[x.rtt_ms for x in xs]
    # This is a conservative estimate, not a statistical confidence interval.
    uncertainty=max(x.resolution_seconds*1000+x.rtt_ms/2 for x in xs)+(max(offsets)-min(offsets))/2
    return Calibration(median(offsets),median(rtts),'低（时间源和单程延迟未验证）',uncertainty)

def target_keys(t):
    return [(v['name'],c,t['start']+'-'+t['end']) for v in t.get('venues',[]) for c in v.get('courts',[])]

def select_candidates(t, available):
    return [k for k in target_keys(t) if k in available]
