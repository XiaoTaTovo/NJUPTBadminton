"""Persist recurring preferences; resolve today's identifiers BEFORE the release gate."""
import copy
import math
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from api import ROOT, Client, SafeError, CN, now_cn
from booking import atomic, run, validate
from preferences import DEFAULT_PRIORITY, tier, rank, court_number
from scheduling import wait_for_start
from clock_sync import calibrate, refresh_clock
from task_runtime import TaskJournal, WakeLease, WaitGuard, wait_until, task_lock, set_title
import uuid

SETTINGS=ROOT/'private/daily-settings.json'
DEFAULT={'version':1,'windows':[{'start':'20:00','end':'21:00','quantity':1},{'start':'21:00','end':'22:00','quantity':1}],
         'priority':DEFAULT_PRIORITY,'max_attempts_per_target':30,
         'focus_target':'20:00-21:00','focus_court_number':6,'strategy':'coverage'}


def validate_settings(settings):
    ws=settings.get('windows',[])
    if not 1<=len(ws)<=2:
        raise SafeError('最多两个目标组，总场数不超过2。')
    seen=set()
    for w in ws:
        start=datetime.strptime(w['start'],'%H:%M').time()
        end=datetime.strptime(w['end'],'%H:%M').time()
        key=(w['start'],w['end'])
        if start>=end or key in seen or type(w['quantity']) is not int or w['quantity']<1:
            raise SafeError('时段必须唯一且开始早于结束，数量必须是正整数。')
        seen.add(key)
    if sum(w['quantity'] for w in ws)>2:
        raise SafeError('总场数最多2，符合账户占用上限。')
    policy=settings['priority']
    ws=settings.get('windows',[])
    focus_target=settings.get('focus_target') or (f"{ws[0]['start']}-{ws[0]['end']}" if ws else '未设置')
    settings['focus_target']=focus_target
    if focus_target not in {f"{w['start']}-{w['end']}" for w in ws}:
        raise SafeError('焦点时段必须存在于当前预约时段中。')
    attempts=settings.get('max_attempts_per_target',30)
    if type(attempts) is not int or not 1<=attempts<=30:raise SafeError('每个目标尝试上限必须为1–30。')
    if settings.get('strategy','coverage') not in ('coverage','focus'):
        raise SafeError('strategy只能是coverage或focus。')
    settings['focus_target']=focus_target
    settings.setdefault('focus_court_number',6)
    if type(settings.get('focus_court_number',6)) is not int or not 1<=settings.get('focus_court_number',6)<=8:
        raise SafeError('focus_court_number必须为1–8。')
    if sorted(policy.get('tier_order',[]))!=[0,1,2,3] or type(policy.get('preferred_3f_court')) is not int or not 1<=policy['preferred_3f_court']<=8:
        raise SafeError('优先级必须包含四档各一次，首选双打场号为1–8。')
    return settings


def load_settings(path=SETTINGS, legacy=None):
    if path.exists():
        settings=validate_settings(json.loads(path.read_text(encoding='utf-8')))
        atomic(path,settings)
        return settings
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
    if settings['windows']:
        settings['focus_target']=f"{settings['windows'][0]['start']}-{settings['windows'][0]['end']}"
    validate_settings(settings)
    path.parent.mkdir(exist_ok=True)
    atomic(path,settings)
    return settings


def build_plan(settings, rows, date):
    """Auto include the four approved Xianlin tiers only; never reuse another date's ID."""
    targets=[]; prepared=[]
    for i,w in enumerate(settings['windows'],1):
        matches=[s for s in rows if s['date']==date and s['start']==w['start'] and s['end']==w['end'] and tier(s['name'])<4]
        focused=f"{w['start']}-{w['end']}"==settings.get('focus_target',f"{settings['windows'][0]['start']}-{settings['windows'][0]['end']}")
        matches.sort(key=lambda s:(0 if focused and tier(s['name'])==0 and court_number(s['name'])==settings.get('focus_court_number',6) else 1,
                                   *rank(s['name'],settings['priority']),court_number(s['name']) or 999,s['name']))
        names=list(dict.fromkeys(s['name'] for s in matches))
        if focused and not any(tier(n)==0 and court_number(n)==settings.get('focus_court_number',6) for n in names):
            print('提示：焦点场没有返回当天场次ID，只能按已配置的其他候选执行；不会伪造ID。')
        if len(names)<w['quantity']:
            raise SafeError('目标日期的接口没有返回足够场次标识，不能提前准备；不会用昨天的ID或12点才盲查。')
        targets.append({'id':f'daily-{i}','date':date,**w,'courts':names,'priority':settings['priority'],
                        '_focus_order':0 if f"{w['start']}-{w['end']}"==settings.get('focus_target') else 1})
        prepared.extend(matches)
    targets.sort(key=lambda g: (g['_focus_order'], settings['windows'].index({'start':g['start'],'end':g['end'],'quantity':g['quantity']})))
    for g in targets: g.pop('_focus_order',None)
    plan={'version':2,'strategy':settings.get('strategy','coverage'),'target_order':'round_robin','focus_target':settings.get('focus_target') or f"{settings['windows'][0]['start']}-{settings['windows'][0]['end']}",
          'focus_court_number':settings.get('focus_court_number',6),'targets':targets,'max_orders':sum(w['quantity'] for w in settings['windows']),
          'max_attempts_per_target':settings.get('max_attempts_per_target',30)}
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
    ws=settings.get('windows',[])
    focus_target=settings.get('focus_target') or (f"{ws[0]['start']}-{ws[0]['end']}" if ws else '未设置')
    settings['focus_target']=focus_target
    settings.setdefault('focus_court_number',6)
    if type(settings.get('focus_court_number',6)) is not int or not 1<=settings.get('focus_court_number',6)<=8:
        raise SafeError('focus_court_number必须为1–8。')
    labels=['新馆3楼双打(1–8)','新馆3楼单打(9–12)','体育馆1楼任意','训练馆2楼任意']
    print('焦点目标：'+str(settings.get('focus_target') or f"{settings['windows'][0]['start']}-{settings['windows'][0]['end']}")+'，新馆3楼'+str(settings.get('focus_court_number',6))+'号；')
    print('调度模式：'+('指定场优先' if settings.get('strategy')=='focus' else '两段覆盖优先')+'；焦点首选优先于普通候选排序。')
    print('默认首选：新馆3楼'+str(policy['preferred_3f_court'])+'号；顺序：'+' > '.join(labels[i] for i in policy['tier_order']))
    for w in settings['windows']:
        print(f"每天 {w['start']}–{w['end']}，需要 {w['quantity']} 场")


def execute(immediate=False, confirm=input):
    settings=load_settings(); describe(settings)
    now=now_cn(); fire=now.replace(hour=12,minute=0,second=0,microsecond=0)
    cutoff=fire-timedelta(seconds=70)
    if not immediate and now>=cutoff:
        print('已到11:58:50，拒绝新定时任务；不自动补跑。如需立即预约，请主动选择菜单2。')
        return None
    print('本次：'+('今天立即提交' if immediate else '11:57:30重新校时；11:59准备；12:00提交'))
    if confirm('1（确认真实预约；已核对账户空余名额，不付款） 0/回车（返回）：').strip()!='1': return None
    private=ROOT/'private'
    try:
        with task_lock(private):
            return _execute_task(settings,fire,immediate,confirm,private)
    except (SafeError,OSError) as exc:
        from safe_errors import MESSAGES
        code=exc.code if isinstance(exc,SafeError) else 'LOG_WRITE_FAILED'
        print('【任务未启动】',code,MESSAGES[code])
        try: confirm('按回车返回菜单：')
        except (KeyboardInterrupt,EOFError): pass
        return None


def _execute_task(settings,fire,immediate,confirm,private):
    # Establish private durable diagnostics BEFORE constructing the network client.
    journal=TaskJournal(private,'immediate' if immediate else 'scheduled',fire,settings['windows'])
    c=None; wake=WakeLease(); clock=None; result=None; guard=None
    context={'mode':'immediate' if immediate else 'scheduled','task_id':journal.task_id}
    def coverage():
        try:
            expiry=float(c.claims['exp'])
            required=fire.timestamp()+300 if not immediate else now_cn().timestamp()+300
            if not math.isfinite(expiry) or expiry<=required: raise ValueError()
        except (KeyError,TypeError,ValueError,OverflowError):
            raise SafeError(code='CREDENTIAL_COVERAGE_INSUFFICIENT') from None
    try:
        c=Client(defer_expiry=not immediate)
        c.task_id=journal.task_id
        if not immediate:
            awake=wake.acquire()
            journal.event('wake_request',{'wake_requested':awake})
            if not awake and confirm('无法申请系统保持唤醒。1（我会保持电脑唤醒并继续） 0（停止）：').strip()!='1':
                raise SafeError(code='USER_CANCELLED')
            refresh_at=fire-timedelta(seconds=150)
            cutoff=fire-timedelta(seconds=70)
            if now_cn()<refresh_at:
                journal.transition('initial_calibration')
                print('初次只读校时；不会修改Windows时间…',flush=True)
                clock=calibrate(observer=journal.event)
                journal.clock=clock; c.clock_time=clock.timestamp
                coverage()
                if clock.now().date()!=fire.date() or clock.now()>=cutoff:
                    raise SafeError(code='PREPARE_DEADLINE_MISSED')
                journal.transition('refresh_wait')
                print('已进入定时等待；11:57:30重新校时，当前尚未准备场次。',flush=True)
                wait_until(refresh_at,clock,journal,fire,strict=False)
            journal.transition('refresh_calibration')
            reference_now=clock.now() if clock else now_cn()
            budget=(cutoff-reference_now).total_seconds()
            if budget<=0: raise SafeError(code='PREPARE_DEADLINE_MISSED')
            deadline=time.monotonic()+budget
            print('临近重新校时（最多3轮，截止11:58:50）…',flush=True)
            clock=refresh_clock(deadline,observer=journal.event,calibrator=calibrate)
            journal.clock=clock; c.clock_time=clock.timestamp
            if clock.now().date()!=fire.date() or clock.now()>=cutoff:
                raise SafeError(code='PREPARE_DEADLINE_MISSED')
            coverage()
            journal.event('clock_adopted',{'offset_seconds':clock.offset,'clock_age_seconds':clock.age()})
            print(f'临近校时成功，参考偏差{clock.offset:+.3f}秒；凭据可覆盖12:05。',flush=True)
            guard=WaitGuard(strict=True)
            journal.transition('prefetch_wait')
            wait_until(fire-timedelta(seconds=60),clock,journal,fire,guard=guard)
        else:
            coverage()
        date=fire.date().isoformat()
        journal.transition('prepare_slots')
        try:
            rows=c.slots(date)
        except SafeError: raise
        except Exception: raise SafeError(code='SLOTS_QUERY_FAILED') from None
        if getattr(c,'telemetry_incomplete',False) is True: raise SafeError(code='LOG_WRITE_FAILED')
        if clock:
            # Network time is expected; clock-relation changes are not.
            guard.check(check_gap=False)
            clock.check_gateway(c.timings[-1] if c.timings else None)
            journal.event('gateway_checked',{'gateway_corroborated':True})
        prepared_at=clock.now() if clock else now_cn()
        plan,prepared=build_plan(settings,rows,date)
        inventory=[]
        for w in settings['windows']:
            raw=[s for s in rows if s['date']==date and s['start']==w['start'] and s['end']==w['end']]
            counts=[sum(tier(s['name'])==i for s in raw) for i in range(4)]
            inventory.append({'start':w['start'],'end':w['end'],'tier_counts':counts})
            print(f"{w['start']}–{w['end']} 候选：3楼双打{counts[0]}、3楼单打{counts[1]}、1楼{counts[2]}、训练馆{counts[3]}")
        snapshot={'task_id':journal.task_id,'prepared_at':prepared_at.isoformat(),'date':date,'plan':plan,'slots':prepared,'inventory':inventory}
        evidence='prepared-'+uuid.uuid4().hex+'.json'
        atomic(private/evidence,snapshot); atomic(private/'prepared-today.json',snapshot); atomic(private/'plan.json',plan)
        context['preparation_file']=evidence
        if not immediate:
            if (fire-clock.now()).total_seconds()<3: raise SafeError(code='PREPARE_DEADLINE_MISSED')
            prepared=[dict(s,observed_available=s['available'],available=True) for s in prepared]
            context['clock']=clock.info
            journal.transition('ready_wait')
            print('【已就绪，等待12:00提交】当天候选已准备；不要再次双击或按Ctrl+C。',flush=True)
            guard.check()
            def tick():
                guard.check(); journal.heartbeat(fire)
            gate_info={'prepared_at':prepared_at.isoformat(),'first_post_uses_prefetched_id':True}
            c.submission_guard=guard.check
            gate_observation={}
            def gate():
                info=wait_for_start(fire,None,now=clock.now,tick=tick,spin_ms=.5,defer_format=True)
                # No datetimes, titles or event construction after release. Materialize after response.
                gate_observation.update(info)
                info.update(gate_info)
                return info
        else:
            journal.transition('booking_engine'); gate=None
        def after_result():
            if not immediate and journal.data.get('phase')=='ready_wait':
                journal.transition('booking_engine')
                journal.data['release_reference_epoch']=gate_observation.get('fired_epoch')
                journal.data['release_perf_ns']=gate_observation.get('fired_perf_ns')
            if not journal.data.get('submission_observed'):
                journal.data['submission_observed']=True
                journal.event('submission_observed')
        c.after_booking_result=after_result
        result=run(plan,c,prepared=prepared,before_first_submit=gate,context=context)
        state=result['state']
        journal.finish(state,client=c,result=result)
        print('本轮结果：',state,'；本轮新增成功：',journal.data.get('success_count',0),'；历史计入：',journal.data.get('credited_count',0))
        if state in ('unknown','blocked'):
            print('【停止新增提交】请到小程序核对订单，不自动重试。')
            confirm('按回车确认已看到结果并返回菜单：')
        return result
    except BaseException as exc:
        post_count=sum(e.get('endpoint')=='booking' for e in getattr(c,'timings',[])) if c else 0
        cancelled=isinstance(exc,(KeyboardInterrupt,EOFError)) or isinstance(exc,SafeError) and exc.code=='USER_CANCELLED'
        state='unknown' if post_count else 'cancelled' if cancelled else 'stopped'
        try: journal.finish(state,exc=exc,client=c,result=result)
        except (OSError,SafeError):
            print('任务诊断写入失败，状态可能不完整；请人工核对。')
        code=exc.code if isinstance(exc,SafeError) else 'USER_CANCELLED' if cancelled else 'INTERNAL_ERROR'
        from safe_errors import MESSAGES
        print('\a【任务已停止：不会继续自动提交】',flush=True)
        print('失败阶段：',journal.data['phase'],'；原因：',code,MESSAGES[code])
        print('校时轮次：',getattr(exc,'details',{}).get('round','不适用'))
        print('记录到的预约请求：',post_count,'；'+('可能已有订单，请人工核对，勿重试。' if post_count else '尚未记录到预约请求。'))
        if not cancelled:
            try: confirm('按回车确认已看到停止原因并返回菜单：')
            except (KeyboardInterrupt,EOFError): pass
        return None
    finally:
        if c is not None: c.close()
        wake.close()
        set_title('预约助手：菜单（非定时等待）')


def change_windows():
    settings=load_settings()
    n=int(input('每天几个不同打球时段（1–2，总场数最多2）：'))
    if not 1<=n<=2:raise SafeError('时段数量须1–2。')
    ws=[]
    for i in range(n):
        text=input(f'第{i+1}组 开始-结束（例如19:00-20:00）：').strip()
        start,end=text.split('-')
        quantity=int(input('该时段需要几场（回车1）：').strip() or '1')
        ws.append({'start':start.strip(),'end':end.strip(),'quantity':quantity})
    settings['windows']=ws
    if settings.get('focus_target') not in {f"{w['start']}-{w['end']}" for w in ws}:settings['focus_target']=f"{ws[0]['start']}-{ws[0]['end']}"
    validate_settings(settings);atomic(SETTINGS,settings)
    print('已保存每天复用的时段和数量，不会下单。')


def change_priority():
    settings=load_settings()
    print('1（恢复默认：3楼6号→其他双打→3楼单打→1楼→训练馆）\n2（自定义四档顺序和首选双打场号）\n3（焦点时段、场号与覆盖/指定场模式）\n0（返回）')
    action=input('请选择：').strip()
    if action=='1':
        settings['priority']=copy.deepcopy(DEFAULT_PRIORITY);settings['focus_court_number']=6;settings['strategy']='coverage'
    elif action=='2':
        print('档位：1=3楼双打，2=3楼单打，3=体育馆1楼，4=训练馆2楼。需每档一次。')
        order=[int(x)-1 for x in input('顺序，例如1,2,3,4：').replace('，',',').split(',')]
        preferred=int(input('3楼双打首选场号（1–8；默认6）：').strip() or '6')
        settings['priority']={'tier_order':order,'preferred_3f_court':preferred}
        print('焦点时段的单独首选仍由选项3配置，其优先级高于普通候选排序。')
    elif action=='3':
        for i,w in enumerate(settings['windows'],1):print(f"{i}（{w['start']}–{w['end']}）")
        choice=int(input('焦点时段序号：'))-1
        if not 0<=choice<len(settings['windows']):raise SafeError('焦点时段序号无效。')
        w=settings['windows'][choice];settings['focus_target']=f"{w['start']}-{w['end']}"
        settings['focus_court_number']=int(input('焦点场号（新馆3楼1–8，回车6）：').strip() or '6')
        mode=input('1（两段覆盖优先，默认） 2（指定场优先，明确未开放时优先重试一次）：').strip() or '1'
        if mode not in ('1','2'):raise SafeError('模式只能选1或2。')
        settings['strategy']='coverage' if mode=='1' else 'focus'
    else:return
    validate_settings(settings);atomic(SETTINGS,settings);print('优先级已保存，不会下单。')
