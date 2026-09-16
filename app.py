"""Interactive entry point. Explicit preview/confirmation before any booking."""
import json
import time
from datetime import datetime
from pathlib import Path
from statistics import median
from email.utils import parsedate_to_datetime
from api import Client, ROOT, SafeError, now_cn, CN
from booking import validate, run, atomic

PRIVATE=ROOT/'private'
PLAN=PRIVATE/'plan.json'

def wizard():
    c=Client()
    try:
        date=input('预约日期 YYYY-MM-DD（回车=今天）：').strip() or now_cn().date().isoformat()
        datetime.strptime(date,'%Y-%m-%d')
        all_slots=c.slots(date)
        area=input('场馆筛选（三牌楼/仙林；回车=三牌楼）：').strip() or '三牌楼'
        if area not in ('三牌楼','仙林'):
            raise SafeError('请填写 三牌楼 或 仙林。')
        filtered=[s for s in all_slots if area in s['name'] and s['date']==date]
        if not filtered:
            raise SafeError('所选日期未返回该场馆场次，不能凭空创建场次。')
        times=sorted({(s['start'],s['end']) for s in filtered})
        print('接口已返回的时段：', '、'.join(a+'–'+b for a,b in times))
        count=int(input('配置几个目标组（1..6；同一时段多场用一个组）：'))
        if not 1<=count<=6:
            raise SafeError('目标组数量超出范围。')
        groups=[]
        for i in range(count):
            start=input(f'目标{i+1} 开始 HH:MM：').strip()
            end=input('结束 HH:MM：').strip()
            candidates=sorted([s for s in filtered if s['start']==start and s['end']==end],key=lambda s:s['name'])
            if not candidates:
                raise SafeError('该时段未返回场次。')
            for j,s in enumerate(candidates,1):
                print(j,s['name'],'可用' if s['available'] else '当前不可用')
            indices=[int(x.strip())-1 for x in input('按优先顺序输入候选序号，逗号分隔（例如 1,3,2）：').replace('，',',').split(',')]
            if any(j<0 or j>=len(candidates) for j in indices):
                raise SafeError('候选序号越界。')
            quantity=int(input('这个时段需要几场（默认1）：').strip() or '1')
            groups.append({'id':f'target-{i+1}','date':date,'start':start,'end':end,'quantity':quantity,
                           'courts':[candidates[j]['name'] for j in indices]})
        plan={'version':2,'targets':groups,'max_orders':sum(g['quantity'] for g in groups)}
        validate(plan); atomic(PLAN,plan)
        print('配置已保存，仅保存配置，不预约。')
        summary(plan)
    finally:
        c.close()

def summary(plan):
    for g in plan['targets']:
        print(f"{g['date']} {g['start']}–{g['end']}，需要 {g['quantity']} 场；候选："+' → '.join(g['courts']))
    print('总订单上限：',plan['max_orders'])

def load_plan():
    return validate(json.loads(PLAN.read_text(encoding='utf-8')))

def execute(scheduled=False):
    plan=load_plan(); summary(plan)
    fire=None
    if scheduled:
        text=input('启动北京时间 YYYY-MM-DD HH:MM:SS（例如当天12:00:00）：').strip()
        fire=datetime.strptime(text,'%Y-%m-%d %H:%M:%S').replace(tzinfo=CN)
        remaining=(fire-now_cn()).total_seconds()
        if not 0<remaining<=3600:
            raise SafeError('仅支持未来一小时内启动。临近放场先刷新凭据。')
    if input('会真实占场但不支付。输入 BOOK 确认（其他输入退出）：').strip()!='BOOK':
        return
    c=Client()
    try:
        c.types()
        if fire:
            remaining=(fire-now_cn()).total_seconds()
            if c.claims['exp']<=time.time()+remaining+300:
                raise SafeError('凭据无法覆盖启动时间，请先刷新。')
            deadline=time.monotonic()+remaining
            print('等待本机北京时间启动；不按秒级 HTTP Date 自动提前提交。请保持电脑唤醒。')
            while time.monotonic()<deadline:
                left=deadline-time.monotonic()
                time.sleep(min(1,max(0,left)))
            if (now_cn()-fire).total_seconds()>3:
                raise SafeError('可能休眠/时钟变化，错过启动窗口，拒绝补跑。')
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

def main():
    import local_session
    local_session.protect_dir()
    menu={'1':lambda:local_session.capture(300),'2':local_session.verify,'3':wizard,
          '4':lambda:execute(False),'5':lambda:execute(True),'6':benchmark,'7':results,'8':local_session.restore,'9':observe}
    while True:
        print('\n南邮预约助手\n1 刷新本人的凭据\n2 只读验证凭据\n3 查询并配置目标\n4 立即预约（需确认）\n5 定时预约（需确认）\n6 三次只读延迟测量\n7 查看本地执行结果\n8 恢复异常退出的系统代理\n9 根据配置只读观测放场\n0 退出')
        option=input('请选择：').strip()
        if option=='0':
            return
        try:
            if option in menu:
                menu[option]()
        except KeyboardInterrupt:
            print('已中止；若有请求在途，请先到小程序核对订单，不要重复提交。')
        except SafeError as e:
            print(str(e))
        except Exception as e:
            print('操作失败：'+type(e).__name__+'；不输出凭据或原始响应。')

if __name__=='__main__':
    main()
