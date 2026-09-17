"""Interactive entry point. Explicit preview/confirmation before any booking."""
import json
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from email.utils import parsedate_to_datetime
from api import Client, ROOT, SafeError, now_cn, CN
from booking import validate, run, atomic
from preferences import tier, label, ordered, RULE
from user_help import GROUP_HELP, MENU, show_help
from scheduling import wait_for_start

PRIVATE=ROOT/'private'
PLAN=PRIVATE/'plan.json'

def number(prompt, low, high, default=None):
    while True:
        raw=input(prompt).strip()
        if not raw and default is not None:
            return default
        if raw.isdigit() and low <= int(raw) <= high:
            return int(raw)
        print(f'请输入 {low}–{high} 范围内的数字。')


def choose_date():
    today=now_cn().date().isoformat()
    choice=number(f'预约日期：1（今天 {today}，默认） 2（指定其他日期）：',1,2,1)
    if choice==1:
        return today
    while True:
        raw=input('其他日期（YYYY-MM-DD）：').strip()
        try:
            datetime.strptime(raw,'%Y-%m-%d')
            return raw
        except ValueError:
            print('日期格式错误，例如 2026-09-18。')


def wizard():
    print('配置流程：日期 → 场馆 → 组数 → 每组时段/候选/数量 → 保存。全程不会下单。')
    c=Client()
    try:
        date=choose_date()
        datetime.strptime(date,'%Y-%m-%d')
        all_slots=c.slots(date)
        area={1:'仙林',2:'三牌楼'}[number('场馆：1（仙林，默认） 2（三牌楼）：',1,2,1)]
        if area=='仙林':
            print('默认优先级：'+RULE+'（只在你选择的候选中生效）')
        filtered=[s for s in all_slots if area in s['name'] and s['date']==date]
        if not filtered:
            raise SafeError('所选日期未返回该场馆场次，不能凭空创建场次。')
        times=sorted({(s['start'],s['end']) for s in filtered})
        print(GROUP_HELP)
        count=number('目标组数量（1–6；每个时段一组，回车默认1组）：',1,6,1)
        groups=[]
        for i in range(count):
            print(f'【第 {i+1}/{count} 组】请选择这一个组的时间段；填完场地和数量才进入下一组。')
            for j,(start,end) in enumerate(times,1):
                print(f'{j}（{start}–{end}）')
            selected=number('时段序号（只填1个数字；如1，不填1,2或18:00）：',1,len(times))
            start,end=times[selected-1]
            if any(g['start']==start and g['end']==end and g['date']==date for g in groups):
                raise SafeError('这个日期和时段已经配置过；如果需要同一时段多场，请在第一个目标组把数量填2，不要再建重复组。')
            candidates=sorted([s for s in filtered if s['start']==start and s['end']==end],key=lambda s:(tier(s['name']),s['name']))
            if not candidates:
                raise SafeError('该时段未返回场次。')
            print(f'已选：{date} {start}–{end}。下面的数字是列表序号，不一定等于场地号。')
            print('当前可用只是查询快照；不可用场地也可作备选，正式运行会重新查询。')
            for j,s in enumerate(candidates,1):
                print(f"{j}（{s['name']}；{'当前可用' if s['available'] else '当前不可用'}；{label(s['name'])}）")
            while True:
                raw=input('候选序号（逗号分隔；例如1,3,2；优先级规则优先，同一档按输入顺序）：').replace('，',',')
                try:
                    indices=[int(x.strip())-1 for x in raw.split(',')]
                    if len(set(indices))!=len(indices) or any(j<0 or j>=len(candidates) for j in indices):
                        raise ValueError()
                    break
                except ValueError:
                    print('请输入不重复且有效的场地序号。')
            remaining=6-sum(g['quantity'] for g in groups)-(count-i-1)
            upper=min(remaining,len(indices))
            quantity=number(f'第{i+1}组需要几场（1=任一候选成功；2=需要两场；本组可填1–{upper}，回车默认1）：',1,upper,1)
            groups.append({'id':f'target-{i+1}','date':date,'start':start,'end':end,'quantity':quantity,
                           'courts':ordered([candidates[j]['name'] for j in indices])})
        plan={'version':2,'targets':groups,'max_orders':sum(g['quantity'] for g in groups)}
        validate(plan); atomic(PLAN,plan)
        print('配置已保存（替换上次配置），没有预约。请核对下方清单，再选菜单4或5执行。')
        summary(plan)
    finally:
        c.close()

def summary(plan):
    print('场地优先级：'+RULE+'；同一档按你的候选顺序，未知名称排最后。')
    for g in plan['targets']:
        print(f"{g['date']} {g['start']}–{g['end']}，需要 {g['quantity']} 场；候选："+' → '.join(ordered(g['courts'])))
    print('总订单上限：',plan['max_orders'])

def load_plan():
    return validate(json.loads(PLAN.read_text(encoding='utf-8')))

def execute(scheduled=False):
    if scheduled:
        from daily import execute as daily_execute
        return daily_execute()
    plan=load_plan(); summary(plan)
    if number('确认：1（真实预约，不付款） 0（返回，默认）：',0,1,0)!=1:
        return
    c=Client()
    try:
        c.types()
        result=run(plan,c)
        print('本轮状态：',result['state'],'；成功数量：',sum(result['completed'].values()))
        print('未自动支付、取消或查询订单。请在小程序核对；结果未知必须人工处理。')
    finally:
        c.close()

def benchmark():
    c=Client()
    try:
        for _ in range(3):
            c.types()
        samples=c.timings
        for i,t in enumerate(samples,1):
            print(f"样本{i}: RTT {t['rtt_ms']:.1f} ms；HTTP Date {t['http_date']}")
        print('RTT 中位数：',median(x['rtt_ms'] for x in samples),'ms')
        print('HTTP Date 为秒级且可能来自网关；不输出毫秒级校时保证。RTT/2不是实际单程延迟。')
        from core import Sample, calibrate
        valid=[]
        for t in samples:
            if t['http_date']:
                valid.append(Sample(t['local_send'],t['local_send']+t['rtt_ms']/1000,parsedate_to_datetime(t['http_date']).timestamp()))
        if valid:
            cal=calibrate(valid)
            print(f'网关 Date 粗略偏差：{cal.offset_ms:+.0f} ms；估计不确定度至少约 ±{cal.uncertainty_ms:.0f} ms；{cal.confidence}')
        atomic(PRIVATE/'clock-samples.json',samples)
    finally:
        c.close()

def observe():
    plan=load_plan()
    c=Client(); last={}; count=0
    path=PRIVATE/'release-observations.jsonl'
    print('只读观测20轮，每轮至少间隔3秒。首次看见可用不等于刚放场；Ctrl+C可退出。')
    try:
        for _ in range(20):
            for date in dict.fromkeys(g['date'] for g in plan['targets']):
                sent=now_cn().isoformat(); slots=c.slots(date); received=now_cn().isoformat()
                for g in plan['targets']:
                    if g['date']!=date: continue
                    for name in g['courts']:
                        matching=[s for s in slots if s['date']==date and s['start']==g['start'] and s['end']==g['end'] and s['name']==name]
                        state='absent' if not matching else 'available' if matching[0]['available'] else 'unavailable'
                        key=(date,g['start'],g['end'],name)
                        previous=last.get(key)
                        event={'date':date,'start':g['start'],'end':g['end'],'court':name,'sent_at':sent,'received_at':received,'state':state,
                               'previous_observed_at':previous[1] if previous else None,'previous_state':previous[0] if previous else None}
                        with path.open('a',encoding='utf-8') as f: f.write(json.dumps(event,ensure_ascii=False)+'\n')
                        if previous is None or previous[0]!=state:
                            print(name,g['start'],state,received,'（客户端观测，不证明精确服务端放场时刻）')
                        last[key]=(state,received)
                count+=1
            time.sleep(3)
        print('只读查询轮次完成：',count)
    finally:
        c.close()

def results():
    paths=sorted(PRIVATE.glob('run-*.json'),key=lambda p:p.stat().st_mtime,reverse=True)
    for p in paths[:5]:
        r=json.loads(p.read_text(encoding='utf-8'))
        print(r['created_at'],r['state'])
        for a in r['attempts']:
            s=a['slot']; print(' ',s['name'],s['date'],s['start'],s['end'],a['state'],a.get('reason',''))
            if a['state']=='success':
                left=max(0,int(a['estimated_payment_deadline']-time.time()))
                print('  估算支付剩余秒数：',left,'；以小程序实际状态为准。')

def advanced_main():
    import local_session
    local_session.protect_dir()
    menu={'1':lambda:local_session.capture(300),'2':local_session.verify,'3':wizard,
          '4':lambda:execute(False),'5':lambda:execute(True),'6':benchmark,'7':results,'8':local_session.restore,'9':observe,'10':show_help}
    while True:
        print(MENU)
        option=input('请选择：').strip()
        if option=='0':
            return
        try:
            if option in menu:
                menu[option]()
            else:
                print('请输入菜单数字0–10；输入10可查看完整帮助。')
        except KeyboardInterrupt:
            print('已中止；若有请求在途，请先到小程序核对订单，不要重复提交。')
        except SafeError as e:
            print(str(e))
        except Exception as e:
            print('操作失败：'+type(e).__name__+'；不输出凭据或原始响应。')

def main():
    import daily
    import local_session
    local_session.protect_dir()
    menu={'1':daily.execute,'2':lambda:daily.execute(immediate=True),
          '3':daily.change_windows,'4':daily.change_priority,
          '5':lambda:local_session.capture(300),'6':local_session.verify,
          '7':results,'8':advanced_main,'9':show_help}
    while True:
        try:
            print('\n【每日快捷预约】')
            daily.describe(daily.load_settings())
            print('1（今天12点预约，默认） 2（今天立即预约）\n3（修改每日时段/场数） 4（可选：修改优先级）\n5（获取/刷新凭据） 6（只读验证登录）\n7（查看结果） 8（高级工具，0返回） 9（字段帮助） 0（退出）')
            option=input('输入数字（回车选1；提交前仍需确认）：').strip() or '1'
            if option=='0': return
            if option in menu: menu[option]()
            else: print('请输入0–9。')
        except (KeyboardInterrupt, EOFError):
            print('已退出等待；若曾提交，请到小程序核对，勿盲目重试。')
            return
        except SafeError as e: print(str(e))
        except Exception as e: print('操作失败：'+type(e).__name__+'；未输出凭据或原始响应。')

if __name__=='__main__':
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print('已退出，未继续执行。')
    except Exception as exc:
        print(str(exc) if isinstance(exc, (SafeError, RuntimeError)) else '启动失败：'+type(exc).__name__+'；请使用 setup_environment.bat 检查环境。')
        raise SystemExit(2)

