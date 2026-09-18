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

ROOT = Path(__file__).resolve().parent
CN = timezone(timedelta(hours=8))
BASE = 'https://wechat.njupt.edu.cn/mini_program/v4'

class SafeError(RuntimeError):
    pass

def now_cn():
    return datetime.now(CN)

def classify_rejection(err_code, message):
    """Map server text to a safe operational category; never persist the raw text."""
    msg = str(message or '').strip()
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
    elif any(x in msg for x in ('已被预约', '已被预订', '已被预定', '已被他人', '已约满', '已满', '已被占用', '无余量', '售罄')):
        category = 'sold_out'
    elif any(x in msg for x in ('重复预约', '同一时段', '每人', '不能同时', '不允许预约')):
        category = 'booking_rule_rejected'
    else:
        category = 'server_rejected_unknown'
    return {
        'category': category,
        'err_code': err_code if type(err_code) is int and abs(err_code)<1000000000 else (err_code if isinstance(err_code,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,24}',err_code) else None),
        'message_terms': [term for term in ('场地','场次','预约','预定','预订','时间','开始','结束','未开放','过期','签名','校验','失败','刷新','重试','抢先','手慢','繁忙','占用','满','频繁','锁定') if term in msg],
        'message_length': len(msg),
        'message_digest': hashlib.sha256(msg.encode('utf-8')).hexdigest()[:12] if msg else None,
    }

class Client:
    def __init__(self, credential=None):
        raw = credential or json.loads((ROOT/'private/session.json').read_text(encoding='utf-8'))
        token = raw['token']
        try:
            part = token.split('.')[1]
            self.claims = json.loads(base64.urlsafe_b64decode(part+'='*(-len(part)%4)))
            if float(self.claims['exp']) <= time.time()+300:
                raise ValueError()
        except Exception:
            raise SafeError('凭据过期、即将过期或无法解析，请先刷新。') from None
        self.http = requests.Session()
        self.http.trust_env = False
        self.http.headers['token'] = token
        self.http.proxies = {'https': 'http://127.0.0.1:7897'}
        self.type_id = self.cached_type_id()
        self.timings = []
        self.last_request = -float('inf')
        self.clock_time = time.time

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
        # All calls, including reads, share a conservative 1-second start interval.
        delay = 1-(time.monotonic()-self.last_request)
        if delay > 0:
            time.sleep(delay)
        self.last_request = time.monotonic()
        wall = time.time()
        data_factory=kwargs.pop('data_factory',None)
        if data_factory is not None:
            kwargs['data']=data_factory()
        endpoint = 'booking' if '/booking/' in path else 'slots' if '/time/display/' in path else 'types'
        event = {'method':method, 'endpoint':endpoint, 'local_send':wall,
                 'rtt_ms':None, 'http_status':None, 'http_date':None,
                 'corrected_send':getattr(self,'clock_time',time.time)()}
        try:
            r = self.http.request(method, BASE+path, timeout=(5, 8), allow_redirects=False, **kwargs)
            event.update(http_status=r.status_code, http_date=r.headers.get('Date'))
            retry = r.headers.get('Retry-After', '')
            try:
                seconds = int(retry) if retry.isdigit() else max(0, int(parsedate_to_datetime(retry).timestamp()-time.time()))
                event['retry_after_seconds'] = seconds
            except (ValueError, TypeError, OverflowError):
                pass
            return r
        except Exception as exc:
            event['error_type'] = type(exc).__name__
            raise
        finally:
            event['rtt_ms'] = round((time.monotonic()-self.last_request)*1000, 2)
            self.timings.append(event)
            try:
                # No URL parameters, headers, credential values or response bodies.
                with (ROOT/'private/request-events.jsonl').open('a',encoding='utf-8') as f:
                    f.write(json.dumps(event,ensure_ascii=False)+'\n')
            except OSError:
                # Never turn a successful POST into an exception because logging failed.
                print('请求日志写入失败；当前请求结果仍按服务端响应处理。')

    def get_list(self, path, **kwargs):
        r = self.request('GET', path, **kwargs)
        if r.status_code != 200:
            raise SafeError(f'只读接口 HTTP {r.status_code}，停止请求。')
        b = r.json()
        if not isinstance(b, dict) or not isinstance(b.get('data'), list):
            raise SafeError('认证、限额或接口结构异常，停止请求。')
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

    def submit(self, slot):
        """Exactly one POST. Anything uncertain stays unknown, never retry here."""
        ui = self.claims.get('userInfo', {})
        if isinstance(ui,str):
            ui = json.loads(ui)
        sid = self.claims.get('studentId') or ui.get('studentId')
        if not sid:
            return {'state':'blocked', 'reason':'missing_local_identity'}
        def payload():
            # Generate after rate-limit waiting, using the same reference clock as the scheduler.
            ts = str(int(getattr(self,'clock_time',time.time)()*1000))
            fingerprint = hashlib.sha256(f"{slot['id']}|{sid}|{slot['date']}|{ts}|4pGmY6s9zX".encode()).hexdigest()
            return {'timestamp':ts,'fingerprint':fingerprint,'date':slot['date']}
        try:
            r = self.request('POST','/venue/user/booking/pomelo/v2/'+slot['id'],data_factory=payload)
            if r.status_code != 200:
                return {'state':'unknown','reason':'http_'+str(r.status_code)}
            b = r.json()
            if b.get('success') is True:
                data = b.get('data') or {}
                d = data.get('detail') or {}
                order_id = (data.get('order') or {}).get('orderId')
                valid = (order_id is not None and str(d.get('orderId')) == str(order_id)
                         # detail.stadiumId identifies the physical court, not the time-slot ID.
                         and d.get('stadiumName') == slot['name']
                         and d.get('startTime') == slot['date']+' '+slot['start']+':00'
                         and d.get('endTime') == slot['date']+' '+slot['end']+':00')
                if not valid:
                    return {'state':'unknown','reason':'success_detail_mismatch'}
                return {'state':'success', 'reason':'server_reported_success', 'order_id':str(order_id),
                        'price':d.get('price'), 'raw_status':d.get('status')}
            if b.get('success') is not False:
                return {'state':'unknown','reason':'unrecognized_response'}
            # An explicit rejection can still carry conflicting order data. Never retry
            # such a response until the user checks orders.
            def has_order(value):
                if isinstance(value,dict):
                    return any((str(k).lower() in ('orderid','order_id','order','detail') and bool(v))
                               or has_order(v) for k,v in value.items())
                if isinstance(value,list):
                    return any(has_order(v) for v in value)
                return False
            if has_order(b):
                return {'state':'unknown','reason':'rejection_contains_order_evidence'}
            rejection = classify_rejection(b.get('errCode'), b.get('errMsg'))
            category = rejection['category']
            if category in ('login_expired','unpaid_order_limit','account_limit',
                            'rate_limit','verification_required','booking_rule_rejected'):
                return {'state':'blocked', 'reason':category, **rejection}
            # No hard-coded meaning for 7070. Explicit success:false, no order evidence,
            # and no recognized account/security stop permits a DIFFERENT candidate.
            return {'state':'sold_out' if category=='sold_out' else 'rejected',
                    'reason':'explicit_unavailable' if category=='sold_out' else 'explicit_business_rejection',
                    'definitive_rejection':True, **rejection}
        except Exception:
            return {'state':'unknown','reason':'transport_or_schema_error'}
