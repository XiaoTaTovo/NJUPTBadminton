"""Live API adapter. No payment, cancellation, auth bypass or automatic HTTP retries."""
import base64
import hashlib
import re
import json
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests
from urllib.parse import urlencode
from transport_metrics import MeasuredAdapter

ROOT = Path(__file__).resolve().parent
CN = timezone(timedelta(hours=8))
BASE = 'https://wechat.njupt.edu.cn/mini_program/v4'

from safe_errors import SafeError

def now_cn():
    return datetime.now(CN)

def safe_message_pattern(message):
    """Keep only fixed business phrases, separators and clock times, never arbitrary text."""
    text=str(message or '')
    if any(x in text.lower() for x in ('token','cookie','password','身份证','手机号','密码')):
        return '[敏感消息已隐藏]'
    terms=('预约时间','预定时间','开放时间','当前时间','时间范围','不在','未到','尚未','未开始','未开放',
           '已结束','已经','已被预定','已被预订','已被预约','预定','预约','场地','其他人','他人','有人',
           '每天','每日','仅限','只能','请','重新','选择','刷新','重试','稍后','失败','成功','抢先','占用')
    allowed=re.compile('|'.join(re.escape(x) for x in sorted(terms,key=len,reverse=True))+r'|(?<!\d)(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?(?!\d)|[，。！：、,!:;；~～—– -]')
    out=[];end=0
    for m in allowed.finditer(text[:500]):
        if m.start()>end and (not out or out[-1]!='[…]'):out.append('[…]')
        out.append(m.group());end=m.end()
    if end<len(text):out.append('[…]')
    return ''.join(out)[:240]


def classify_rejection(err_code, message):
    """Map server text to a safe operational category; never persist the raw text."""
    msg = str(message or '').strip()
    # Fixed semantic categories only; never persist arbitrary matching text.
    if str(err_code) == '5004' or any(x in msg for x in ('登录', 'token', 'TOKEN', '认证')):
        category = 'login_expired'
    elif any(x in msg for x in ('未支付', '待支付')):
        category = 'unpaid_order_limit'
    elif any(x in msg for x in ('订单上限', '预约上限', '数量上限', '最多预约2', '最多预约两', '只能预约2', '只能预约两', '超限', '权限')):
        category = 'account_limit'
    elif any(x in msg for x in ('频繁', '限流', '过快', '请求过多')):
        category = 'rate_limit'
    elif any(x in msg for x in ('验证码', '风控', '安全验证')):
        category = 'verification_required'
    elif any(x in msg for x in ('未到预约时间','尚未到预约时间','未到开放时间','尚未开放','暂未开放','预约尚未开始')):
        category = 'not_open_yet'
    elif any(x in msg for x in ('不在预约时间','非预约时间','不在允许的预约时间','不在可预约时间')):
        category = 'outside_booking_window'
    elif any(x in msg for x in ('已被预约', '已被预订', '已被预定', '已被他人', '已约满', '已满', '已被占用', '无余量', '售罄','场地已经被预定','场地已经被预约','其他人预定','其他人预约')):
        category = 'sold_out'
    elif any(x in msg for x in ('重复预约', '同一时段', '每人', '不能同时', '不允许预约')):
        category = 'booking_rule_rejected'
    elif any(x in msg for x in ('预约时间','预定时间','开放时间')):
        category = 'time_window_hint'
    else:
        category = 'server_rejected_unknown'
    return {
        'classifier_version':2,
        'message_template':category,
        'boundary_retry_eligible':category=='not_open_yet',
        'category': category,
        'err_code': err_code if type(err_code) is int and abs(err_code)<1000000000 else (err_code if isinstance(err_code,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,24}',err_code) else None),
        'message_terms': [term for term in ('场地','场次','预约','预定','预订','时间','开始','结束','未开放','过期','签名','校验','失败','刷新','重试','抢先','手慢','繁忙','占用','满','频繁','锁定') if term in msg],
        'message_pattern': safe_message_pattern(msg),
        'message_length': len(msg),
        'message_digest': hashlib.sha256(msg.encode('utf-8')).hexdigest()[:12] if msg else None,
    }

class Client:
    def __init__(self, credential=None, *, defer_expiry=False):
        raw = credential or json.loads((ROOT/'private/session.json').read_text(encoding='utf-8'))
        token = raw['token']
        try:
            part = token.split('.')[1]
            self.claims = json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))
            if not defer_expiry and float(self.claims['exp']) <= time.time()+300:
                raise ValueError()
        except Exception:
            raise SafeError(code='CREDENTIAL_COVERAGE_INSUFFICIENT') from None
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.mount('https://',MeasuredAdapter(max_retries=0))
        self.http.headers['token'] = token
        self.http.proxies = {'https': 'http://127.0.0.1:7897'}
        self.type_id = self.cached_type_id()
        self.timings = []
        self.last_request = -float('inf')
        self.min_request_interval = 1.0
        self.clock_time = time.time
        self.telemetry_incomplete = False

    @staticmethod
    def cached_type_id():
        try:
            saved=json.loads((ROOT/'private/venue-type.json').read_text(encoding='utf-8'))
            value=saved.get('type_id')
            if saved.get('base')==BASE and isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',value):
                return value
        except (OSError,ValueError,AttributeError):
            pass
        return None

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        """Sign AFTER limiter wait; prepared POST avoids Session.prepare_request at release."""
        enter=time.perf_counter_ns()
        interval=float(getattr(self,'min_request_interval',1.0))
        earliest=self.last_request+interval
        not_after=getattr(self,'submit_not_after',None) if method=='POST' else None
        if not_after is not None and max(time.monotonic(),earliest)>not_after:
            self.submit_not_after=None
            raise SafeError(code='BOUNDARY_RETRY_EXPIRED')
        delay=earliest-time.monotonic()
        if delay>0: time.sleep(delay)
        limiter_done=time.perf_counter_ns()
        if getattr(self,'submission_guard',None):
            guard=self.submission_guard;self.submission_guard=None;guard()
        if not_after is not None and time.monotonic()>not_after:
            self.submit_not_after=None
            raise SafeError(code='BOUNDARY_RETRY_EXPIRED')
        self.submit_not_after=None
        factory=kwargs.pop('data_factory',None)
        prepared=kwargs.pop('_prepared_request',None)
        if factory is not None:
            data=factory()
            if prepared is not None:
                body=urlencode(data)
                prepared.body=body
                prepared.headers['Content-Length']=str(len(body.encode('ascii')))
            else: kwargs['data']=data
        signature_done=time.perf_counter_ns()
        event={'method':method,'endpoint':'booking' if '/booking/' in path else 'slots' if '/time/display/' in path else 'types',
               'request_enter_ns':enter,'limiter_finished_ns':limiter_done,'signature_ready_ns':signature_done,
               'http_status':None,'http_date':None,'timing_version':2}
        if getattr(self,'task_id',None):event['task_id']=self.task_id
        release=getattr(self,'release_perf_ns',None);self.release_perf_ns=None
        def header_hook(response,*args,**kw):
            trace=getattr(response,'_booking_transport_trace',{})
            for key in ('transport_enter_ns','response_headers_ns','connection_reuse_observed'):
                if key in trace:event[key]=trace[key]
            event.setdefault('response_headers_ns',time.perf_counter_ns())
            return response
        if prepared is not None:prepared._booking_trace=event
        else: kwargs['hooks']={'response':header_hook}
        event['local_send']=time.time()
        event['corrected_send']=getattr(self,'clock_time',time.time)()
        self.last_request=time.monotonic()
        event['http_call_started_ns']=time.perf_counter_ns()
        response=None
        try:
            if prepared is not None:
                response=self.http.send(prepared,timeout=(5,8),verify=True,proxies=dict(self.http.proxies),
                                        allow_redirects=False,stream=True)
            else:
                response=self.http.request(method,BASE+path,timeout=(5,8),verify=True,allow_redirects=False,stream=True,**kwargs)
            event.setdefault('response_headers_ns',time.perf_counter_ns())
            # Consume fully so the pool can reuse the connection. Not logged.
            _=response.content
            event['response_body_done_ns']=time.perf_counter_ns()
            event['http_status']=response.status_code
            raw_date=response.headers.get('Date')
            try:
                from email.utils import format_datetime
                event['http_date']=format_datetime(parsedate_to_datetime(raw_date),usegmt=True)
            except (ValueError,TypeError,OverflowError):pass
            retry=response.headers.get('Retry-After','')
            try:
                event['retry_after_seconds']=int(retry) if retry.isdigit() else max(0,int(parsedate_to_datetime(retry).timestamp()-time.time()))
            except (ValueError,TypeError,OverflowError):pass
            return response
        except Exception as exc:
            event['error_type']=type(exc).__name__
            raise
        finally:
            if response is not None and callable(getattr(response,'close',None)):response.close()
            end=event.setdefault('response_body_done_ns',time.perf_counter_ns())
            start=event['http_call_started_ns']
            event.update(limiter_wait_ms=round((limiter_done-enter)/1e6,4),
                         signature_prepare_ms=round((signature_done-limiter_done)/1e6,4),
                         rtt_ms=round((end-start)/1e6,4),http_total_ms=round((end-start)/1e6,4),
                         gate_to_http_call_ms=round((start-release)/1e6,4) if release else None,
                         gate_to_transport_ms=round((event['transport_enter_ns']-release)/1e6,4) if release and 'transport_enter_ns' in event else None)
            self.timings.append(event)
            try:
                with (ROOT/'private/request-events.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps(event,ensure_ascii=False)+'\n')
            except OSError:
                self.telemetry_incomplete=True
                print('请求日志写入失败；保留当前响应，停止后续新增提交。')

    def get_list(self, path, **kwargs):
        try:
            r = self.request('GET', path, **kwargs)
        except Exception:
            raise SafeError(code='SLOTS_QUERY_FAILED') from None
        if r.status_code in (401,403):
            raise SafeError(code='AUTH_REJECTED')
        if r.status_code != 200:
            raise SafeError(code='SLOTS_QUERY_FAILED', details={'http_status':r.status_code})
        try:
            b = r.json()
        except (ValueError, TypeError):
            raise SafeError(code='SLOTS_QUERY_FAILED') from None
        if isinstance(b,dict) and b.get('success') is False:
            category=classify_rejection(b.get('errCode',b.get('code')),b.get('msg',b.get('message')))['category']
            raise SafeError(code='AUTH_REJECTED' if category=='login_expired' else 'SLOTS_QUERY_FAILED')
        if not isinstance(b, dict) or not isinstance(b.get('data'), list):
            raise SafeError(code='SLOTS_QUERY_FAILED')
        return b['data']

    def types(self):
        rows = self.get_list('/venue/user/types')
        matches = [x for x in rows if isinstance(x,dict) and '羽毛球' in str(x.get('name',''))]
        if len(matches) != 1:
            raise SafeError('羽毛球类型无法唯一识别。')
        self.type_id = str(matches[0]['id'])
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',self.type_id):
            raise SafeError('类型标识格式异常，停止请求。')
        try:
            import uuid
            path=ROOT/'private/venue-type.json'
            tmp=path.with_name('venue-type-'+uuid.uuid4().hex+'.tmp')
            tmp.write_text(json.dumps({'base':BASE,'type_id':self.type_id},ensure_ascii=False),encoding='utf-8')
            tmp.replace(path)
        except OSError:
            print('类型缓存写入失败，本次仍可使用已查询的类型。')
        return rows

    def slots(self, date):
        if self.type_id is None:
            self.types()
        rows = self.get_list('/venue/user/time/display/'+self.type_id, params={'date':date})
        result = []
        for e in rows:
            for tf in e['timeFields']:
                for st in tf['stadiumInfos']:
                    if type(st.get('status')) is not bool:
                        raise SafeError('场次状态不再是布尔值，请人工检查接口。')
                    result.append({'id':str(st['id']), 'date':e['localDate'], 'name':st['name'],
                                   'start':tf['startTime'][:5], 'end':tf['endTime'][:5], 'available':st['status']})
        return result

    def _identity(self):
        ui=self.claims.get('userInfo',{})
        if isinstance(ui,str): ui=json.loads(ui)
        sid=self.claims.get('studentId') or ui.get('studentId')
        if not sid: raise SafeError('缺少本地身份字段，停止提交。')
        return str(sid)

    def prepare_submit(self, slot):
        """Prepare headers, URL, identity and hash prefix before release. No request is sent."""
        sid=self._identity();frozen=dict(slot)
        path='/venue/user/booking/pomelo/v2/'+frozen['id']
        prefix=hashlib.sha256(f"{frozen['id']}|{sid}|{frozen['date']}|".encode())
        def payload():
            ts=str(int(getattr(self,'clock_time',time.time)()*1000))
            h=prefix.copy();h.update((ts+'|4pGmY6s9zX').encode())
            return {'timestamp':ts,'fingerprint':h.hexdigest(),'date':frozen['date']}
        prepared=None
        if isinstance(getattr(self,'http',None),requests.Session):
            prepared=self.http.prepare_request(requests.Request('POST',BASE+path,
                data={'timestamp':'0','fingerprint':'0'*64,'date':frozen['date']}))
        return lambda:self._submit_prepared(frozen,path,payload,prepared)

    def _submit_prepared(self, slot, path, payload, prepared):
        classification_start=None
        try:
            r=self.request('POST',path,data_factory=payload,**({'_prepared_request':prepared} if prepared is not None else {}))
            classification_start=time.perf_counter_ns()
            if r.status_code!=200:return {'state':'unknown','reason':'http_'+str(r.status_code)}
            b=r.json()
            if b.get('success') is True:
                data=b.get('data') or {};d=data.get('detail') or {};order_id=(data.get('order') or {}).get('orderId')
                valid=(order_id is not None and str(d.get('orderId'))==str(order_id) and d.get('stadiumName')==slot['name'] and d.get('startTime')==slot['date']+' '+slot['start']+':00' and d.get('endTime')==slot['date']+' '+slot['end']+':00')
                if not valid:return {'state':'unknown','reason':'success_detail_mismatch'}
                return {'state':'success','reason':'server_reported_success','order_id':str(order_id),'price':d.get('price'),'raw_status':d.get('status')}
            if b.get('success') is not False:return {'state':'unknown','reason':'unrecognized_response'}
            def has_order(value):
                if isinstance(value,dict):return any((str(k).lower() in ('orderid','order_id','order','detail') and bool(v)) or has_order(v) for k,v in value.items())
                if isinstance(value,list):return any(has_order(v) for v in value)
                return False
            if has_order(b):return {'state':'unknown','reason':'rejection_contains_order_evidence'}
            rejection=classify_rejection(b.get('errCode'),b.get('errMsg'));category=rejection['category']
            if category in ('login_expired','unpaid_order_limit','account_limit','rate_limit','verification_required','booking_rule_rejected'):
                return {'state':'blocked','reason':category,**rejection}
            return {'state':'sold_out' if category=='sold_out' else 'rejected','reason':'explicit_unavailable' if category=='sold_out' else 'explicit_business_rejection','definitive_rejection':True,**rejection}
        except SafeError as exc:
            if exc.code=='BOUNDARY_RETRY_EXPIRED':return {'state':'not_submitted','reason':'boundary_retry_expired'}
            return {'state':'unknown','reason':'transport_or_schema_error'}
        except Exception:
            return {'state':'unknown','reason':'transport_or_schema_error'}
        finally:
            self.last_classification_ms=(time.perf_counter_ns()-classification_start)/1e6 if classification_start is not None else None

    def submit(self, slot):
        try:return self.prepare_submit(slot)()
        except SafeError:return {'state':'blocked','reason':'missing_local_identity'}
