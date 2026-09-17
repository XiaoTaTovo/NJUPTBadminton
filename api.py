"""Live API adapter. No payment, cancellation, auth bypass or automatic HTTP retries."""
import base64
import hashlib
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
        self.type_id = None
        self.timings = []
        self.last_request = -float('inf')

    def close(self):
        self.http.close()

    def request(self, method, path, **kwargs):
        # All calls, including reads, share a conservative 1-second start interval.
        time.sleep(max(0, 1-(time.monotonic()-self.last_request)))
        self.last_request = time.monotonic()
        wall = time.time()
        endpoint = 'booking' if '/booking/' in path else 'slots' if '/time/display/' in path else 'types'
        event = {'method':method, 'endpoint':endpoint, 'local_send':wall,
                 'rtt_ms':None, 'http_status':None, 'http_date':None}
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
        ts = str(int(time.time()*1000))
        # Protocol signature observed in the reference repository and verified in one live test.
        fingerprint = hashlib.sha256(f"{slot['id']}|{sid}|{slot['date']}|{ts}|4pGmY6s9zX".encode()).hexdigest()
        try:
            r = self.request('POST','/venue/user/booking/pomelo/v2/'+slot['id'],
                             data={'timestamp':ts,'fingerprint':fingerprint,'date':slot['date']})
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
            msg = str(b.get('errMsg') or '')
            # Business limit/auth/captcha errors always stop, never change account or loop.
            if b.get('errCode') == 5004 or any(x in msg for x in ('超限','上限','未支付','频繁','验证码','登录','权限')):
                reason = ('login_expired' if b.get('errCode') == 5004 or '登录' in msg else
                          'unpaid_order_limit' if '未支付' in msg else
                          'rate_limit' if '频繁' in msg else
                          'verification_required' if '验证码' in msg else
                          'account_or_rate_limit')
                return {'state':'blocked','reason':reason}
            if any(x in msg for x in ('已被预约','已被预订','已被他人','已约满','已满','已被占用')):
                return {'state':'sold_out','reason':'explicit_unavailable'}
            # Unknown explicit failures still stop: don't assume a retry is safe.
            return {'state':'blocked','reason':'unrecognized_business_failure'}
        except Exception:
            return {'state':'unknown','reason':'transport_or_schema_error'}
