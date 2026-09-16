from dataclasses import dataclass
from statistics import median
@dataclass(frozen=True)
class Sample:
 local_send: float; local_recv: float; server_epoch: float
 @property
 def rtt_ms(self): return (self.local_recv-self.local_send)*1000
 @property
 def offset_ms(self): return (self.server_epoch-(self.local_send+self.local_recv)/2)*1000
@dataclass(frozen=True)
class Calibration:
 offset_ms: float; rtt_ms: float; confidence: str
def calibrate(samples):
 xs=list(samples)
 if not xs: raise ValueError('至少需要一个时间样本')
 o=[x.offset_ms for x in xs]; r=[x.rtt_ms for x in xs];
 return Calibration(median(o),median(r),'高' if len(xs)>=5 and max(r)<100 and max(o)-min(o)<50 else '中' if len(xs)>=3 else '低')
def target_keys(t):
 return [(v['name'],c,t['start']+'-'+t['end']) for v in t.get('venues',[]) for c in v.get('courts',[])]
def select_candidates(t,available): return [k for k in target_keys(t) if k in available]
