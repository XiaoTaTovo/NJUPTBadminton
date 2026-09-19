"""Persist recurring preferences; resolve today's identifiers BEFORE the release gate."""
import copy
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from api import ROOT, Client, SafeError, CN, now_cn
from booking import atomic, run, validate
from preferences import DEFAULT_PRIORITY, tier, rank, court_number
from scheduling import wait_for_start
from clock_sync import calibrate
import uuid

SETTINGS=ROOT/'private/daily-settings.json'
DEFAULT={'version':1,'windows':[{'start':'19:00','end':'20:00','quantity':1},{'start':'20:00','end':'21:00','quantity':1}],
         'priority':DEFAULT_PRIORITY,'max_attempts_per_target':30}


def validate_settings(settings):
    ws=settings.get('windows',[])
    if not 1<=len(ws)<=6:
        raise SafeError('需要1–6个打球时段。')
    seen=set()
    for w in ws:
        start=datetime.strptime(w['start'],'%H:%M').time()
        end=datetime.strptime(w['end'],'%H:%M').time()
        key=(w['start'],w['end'])
        if start>=end or key in seen or type(w['quantity']) is not int or w['quantity']<1:
            raise SafeError('时段必须唯一且开始早于结束，数量必须是正整数。')
        seen.add(key)
    if sum(w['quantity'] for w in ws)>6:
        raise SafeError('总场数最多6。')
    policy=settings['priority']
    if sorted(policy.get('tier_order',[]))!=[0,1,2,3] or type(policy.get('preferred_3f_court')) is not int or not 1<=policy['preferred_3f_court']<=8:
        raise SafeError('优先级必须包含四档各一次，首选双打场号为1–8。')
    return settings


def load_settings(path=SETTINGS, legacy=None):
    if path.exists():
        return validate_settings(json.loads(path.read_text(encoding='utf-8')))
    settings=copy.deepcopy(DEFAULT)
    old=legacy or ROOT/'private/plan.json'
    if old.exists():
        p=json.loads(old.read_text(encoding='utf-8'))
        windows={}
        for g in p.get('targets',[]):
            key=(g['start'],g['end'])
            windows[key]=windows.get(key,0)+g['quantity']
        if windows:
            settings['windows']=[dict(start=s,end=e,quantity=q) for (s,e),q in windows.items()]
    validate_settings(settings)
    path.parent.mkdir(exist_ok=True)
    atomic(path,settings)
    return settings


def build_plan(settings, rows, date):
    """Auto include the four approved Xianlin tiers only; never reuse another date's ID."""
    targets=[]; prepared=[]
    for i,w in enumerate(settings['windows'],1):
        matches=[s for s in rows if s['date']==date and s['start']==w['start'] and s['end']==w['end'] and tier(s['name'])<4]
        matches.sort(key=lambda s:(*rank(s['name'],settings['priority']),court_number(s['name']) or 999,s['name']))
        names=list(dict.fromkeys(s['name'] for s in matches))
        if len(names)<w['quantity']:
            raise SafeError('目标日期的接口没有返回足够场次标识，不能提前准备；不会用昨天的ID或12点才盲查。')
        targets.append({'id':f'daily-{i}','date':date,**w,'courts':names,'priority':settings['priority']})
        prepared.extend(matches)
    plan={'version':2,'target_order':'configured','targets':targets,'max_orders':sum(w['quantity'] for w in settings['windows']),
          'max_attempts_per_target':30}
    return validate(plan),prepared


def wait_until_prefetch(fire, now=now_cn, monotonic=time.monotonic, sleep=time.sleep):
    """One wait, no background polling. Prepare at T-60s, or immediately if closer."""
    delay=max(0,(fire-now()).total_seconds()-60)
    target=monotonic()+delay
    while monotonic()<target:
        sleep(min(1,target-monotonic()))
    remaining=(fire-now()).total_seconds()
    if remaining<=0:
        raise SafeError('已经错过提前准备窗口，停止；如需今天立即预约请明确选择立即模式。')
    if remaining>65:
        raise SafeError('等待中系统时钟变化，停止以防用错误时刻提交。')


def describe(settings):
    print('每日日期：今天；开抢：北京时间12:00；地点：仙林四档自动备选。')
    policy=settings['priority']
    labels=['新馆3楼双打(1–8)','新馆3楼单打(9–12)','体育馆1楼任意','训练馆2楼任意']
    print('默认首选：新馆3楼'+str(policy['preferred_3f_court'])+'号；顺序：'+' > '.join(labels[i] for i in policy['tier_order']))
    for w in settings['windows']:
        print(f"每天 {w['start']}–{w['end']}，需要 {w['quantity']} 场")


def execute(immediate=False, confirm=input):
    settings=load_settings();describe(settings)
    now=now_cn();date=now.date().isoformat();fire=now.replace(hour=12,minute=0,second=0,microsecond=0)
    if not immediate and now>=fire:
        print('今天中午12点已过。没有自动改明天或立即下单；今天仍要订请选主菜单2（今天立即预约）。')
        return None
    print('本次：'+('今天立即提交' if immediate else f'{date} 11:59前后准备场次，12:00直接提交首选'))
    if confirm('1（确认本次真实预约，不付款） 0/回车（返回）：').strip()!='1':
        return None
    c=None
    phase='credentials'
    context={'mode':'immediate' if immediate else 'scheduled'}
    try:
        c=Client()
        clock=None
        if not immediate:
            print('只读校准参考时间（不修改Windows时钟）…',flush=True)
            phase='clock_calibration'
            clock=calibrate()
            c.clock_time=clock.timestamp
            print(f'参考时间相对本机偏差 {clock.offset:+.3f} 秒；按校准后12点触发，不按未校准本机时间。',flush=True)
            if clock.now()>=fire:
                raise SafeError('校准后已过今天12点，停止，不自动补跑。')
        if not immediate and c.claims['exp']<=fire.timestamp()+300:
            raise SafeError('凭据不能覆盖12点后五分钟，请临近开抢刷新一次。')
        if not immediate:
            print('等待提前一分钟准备；无需反复点击启动。',flush=True)
            phase='prefetch_wait'
            wait_until_prefetch(fire,now=clock.now)
            if clock.age()>120:
                clock=calibrate()
                c.clock_time=clock.timestamp
                wait_until_prefetch(fire,now=clock.now)
        phase='prepare_slots'
        rows=c.slots(date)
        if clock:
            clock.check_gateway(c.timings[-1] if c.timings else None)
        prepared_at=clock.now() if clock else now_cn()
        plan,prepared=build_plan(settings,rows,date)
        inventory=[]
        for w in settings['windows']:
            raw=[s for s in rows if s['date']==date and s['start']==w['start'] and s['end']==w['end']]
            counts=[sum(tier(s['name'])==i for s in raw) for i in range(4)]
            inventory.append({'start':w['start'],'end':w['end'],'tier_counts':counts,
                              'unrecognized_names':sorted({s['name'] for s in raw if tier(s['name'])==4})})
            print(f"{w['start']}–{w['end']} 候选：3楼双打{counts[0]}、3楼单打{counts[1]}、1楼{counts[2]}、训练馆{counts[3]}（0表示本次接口未返回该档，不伪造场次）。")
        snapshot={'prepared_at':prepared_at.isoformat(),'date':date,'plan':plan,'slots':prepared,'inventory':inventory}
        evidence='prepared-'+uuid.uuid4().hex+'.json'
        atomic(ROOT/'private'/evidence,snapshot)
        context['preparation_file']=evidence
        atomic(ROOT/'private/prepared-today.json',snapshot)
        atomic(ROOT/'private/plan.json',plan)
        if not immediate:
            if (fire-prepared_at).total_seconds()<3:
                raise SafeError('场次准备完成时距12点不足3秒，停止，避免把超时准备误当准点提交。')
            # Before release, availability can mean "not open yet". IDs must still be returned
            # for TODAY; server remains authoritative and every POST may be rejected.
            prepared=[dict(s,observed_available=s['available'],available=True) for s in prepared]
            print('当天场次已准备；开抢前不把false快照认定为永久售罄。首笔用预备ID，到点不再GET。',flush=True)
            context['clock']=clock.info
            def gate():
                info=wait_for_start(fire,None,now=clock.now)
                info.update(prepared_at=prepared_at.isoformat(),first_post_uses_prefetched_id=True)
                return info
        else:
            gate=None
        phase='booking_engine'
        result=run(plan,c,prepared=prepared,before_first_submit=gate,context=context)
        successes=[a for a in result.get('attempts',[]) if a['state']=='success']
        credited=result.get('credited_slots',[])
        print('本轮结果：',result['state'],'；本轮新增成功：',len(successes),'；历史记录计入：',len(credited))
        for s in [a['slot'] for a in successes]+credited:
            print('【预约记录】',s['start']+'–'+s['end'],s['name'],'；支付/取消/过期状态请到小程序核对。')
        return result
    except BaseException as exc:
        try:
            atomic(ROOT/'private'/('launch-failure-'+uuid.uuid4().hex+'.json'),
                   {'local_time':now_cn().isoformat(),'phase':phase,'context':context,'error_type':type(exc).__name__})
        except OSError:
            pass
        raise
    finally:
        if c is not None:
            c.close()


def change_windows():
    settings=load_settings()
    n=int(input('每天几个不同打球时段（1–6）：'))
    if not 1<=n<=6:raise SafeError('时段数量须1–6。')
    ws=[]
    for i in range(n):
        text=input(f'第{i+1}组 开始-结束（例如19:00-20:00）：').strip()
        start,end=text.split('-')
        quantity=int(input('该时段需要几场（回车1）：').strip() or '1')
        ws.append({'start':start.strip(),'end':end.strip(),'quantity':quantity})
    settings['windows']=ws;validate_settings(settings);atomic(SETTINGS,settings)
    print('已保存每天复用的时段和数量，不会下单。')


def change_priority():
    settings=load_settings()
    print('1（恢复默认：3楼6号→其他双打→3楼单打→1楼→训练馆）\n2（自定义四档顺序和首选双打场号）\n0（返回）')
    action=input('请选择：').strip()
    if action=='1':settings['priority']=copy.deepcopy(DEFAULT_PRIORITY)
    elif action=='2':
        print('档位：1=3楼双打，2=3楼单打，3=体育馆1楼，4=训练馆2楼。需每档一次。')
        order=[int(x)-1 for x in input('顺序，例如1,2,3,4：').replace('，',',').split(',')]
        preferred=int(input('3楼双打首选场号（1–8；默认6）：').strip() or '6')
        settings['priority']={'tier_order':order,'preferred_3f_court':preferred}
    else:return
    validate_settings(settings);atomic(SETTINGS,settings);print('优先级已保存，不会下单。')
