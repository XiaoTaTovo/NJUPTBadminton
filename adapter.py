"""南邮接口适配器：仅在本机已有 token 时使用；不负责获取或打印 token。"""
from dataclasses import dataclass
import time, requests
BASE='https://wechat.njupt.edu.cn/mini_program/v4'
@dataclass
class Slot:
 id:str; name:str; start:str; end:str; available:bool; date:str
class NJUPTAdapter:
 def __init__(self, token, verify_tls=True, timeout=8):
  if not token: raise ValueError('缺少本地 token')
  self.s=requests.Session(); self.s.headers.update({'token':token}); self.verify_tls=verify_tls; self.timeout=timeout
 def _get(self,path,params=None):
  r=self.s.get(BASE+path,params=params,timeout=self.timeout,verify=self.verify_tls); r.raise_for_status(); return r
 def server_sample(self):
  t0=time.time(); r=self._get('/venue/user/types'); t1=time.time()
  return {'sent':t0,'received':t1,'date':r.headers.get('Date'),'status':r.status_code}
 def badminton_type_id(self):
  data=self._get('/venue/user/types').json().get('data') or []
  for x in data:
   if '羽毛球' in str(x.get('name','')): return str(x['id'])
  raise LookupError('未找到羽毛球类型')
 def slots(self,date,type_id=None):
  type_id=type_id or self.badminton_type_id(); payload=self._get(f'/venue/user/time/display/{type_id}',{'date':date}).json()
  out=[]
  for entry in payload.get('data') or []:
   local=entry.get('localDate',date)
   for tf in entry.get('timeFields') or []:
    for st in tf.get('stadiumInfos') or []:
     out.append(Slot(str(st.get('id')),st.get('name',''),tf.get('startTime','')[:5],tf.get('endTime','')[:5],bool(st.get('status')),local))
  return out
