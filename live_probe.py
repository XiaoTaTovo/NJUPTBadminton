"""Bounded live diagnostics; all runtime records remain private."""
import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
import requests

ROOT = Path(__file__).resolve().parent
BASE = 'https://wechat.njupt.edu.cn/mini_program/v4'

def session():
    token = json.loads((ROOT/'private/session.json').read_text(encoding='utf-8'))['token']
    s = requests.Session()
    s.trust_env = False
    s.proxies = {'https': 'http://127.0.0.1:7897'}
    s.headers['token'] = token
    return s

def read(s, path, **kwargs):
    start = time.perf_counter()
    r = s.get(BASE+path, timeout=15, allow_redirects=False, **kwargs)
    elapsed = (time.perf_counter()-start)*1000
    r.raise_for_status()
    if r.status_code != 200:
        raise ValueError('unexpected HTTP status')
    body = r.json()
    if not isinstance(body, dict) or not isinstance(body.get('data'), list):
        raise ValueError('authentication or schema failure')
    return body['data'], elapsed, r.headers.get('Date')

def slots(s, date):
    types, _, _ = read(s, '/venue/user/types')
    matches = [t for t in types if '羽毛球' in str(t.get('name',''))]
    if len(matches) != 1:
        raise ValueError('ambiguous badminton type')
    data, elapsed, _ = read(s, '/venue/user/time/display/'+str(matches[0]['id']), params={'date':date})
    out = []
    for e in data:
        for tf in e.get('timeFields', []):
            for st in tf.get('stadiumInfos', []):
                out.append({'id':st['id'], 'name':st['name'], 'date':e['localDate'],
                            'start':tf['startTime'], 'end':tf['endTime'], 'available': st.get('status') is True})
    return out, elapsed

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--date', default=datetime.now(timezone(timedelta(hours=8))).date().isoformat())
    p.add_argument('--observe', type=int, default=1, help='1..30 snapshots, three seconds apart; read-only')
    a = p.parse_args()
    if not 1 <= a.observe <= 30:
        p.error('observe must be 1..30')
    datetime.strptime(a.date, '%Y-%m-%d')
    previous = {}
    with session() as s:
        for i in range(a.observe):
            found, latency = slots(s, a.date)
            now = datetime.now(timezone.utc).isoformat()
            snapshot = {'observed_at':now, 'date':a.date, 'rtt_ms':latency, 'slots':found}
            with (ROOT/'private/observations.jsonl').open('a', encoding='utf-8') as f:
                f.write(json.dumps(snapshot, ensure_ascii=False)+'\n')
            print(f'查询耗时 {latency:.1f} ms；这是客户端响应耗时，不是服务器处理耗时。')
            for st in found:
                key=(st['date'],st['id'],st['start'])
                if st['available'] and '三牌楼' in st['name']:
                    print(st['name'], st['date'], st['start'], st['end'],
                          '已观察到可用（首次快照不证明放场时刻）' if key not in previous else '可用')
                previous[key] = st['available']
            if i+1 < a.observe:
                time.sleep(3)

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('只读检测失败：'+type(exc).__name__+'；不输出原始请求或响应。')
        raise SystemExit(1)
