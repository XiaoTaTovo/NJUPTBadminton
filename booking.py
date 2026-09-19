"""Deterministic maximum matching and fail-closed, bounded sequential booking."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from api import ROOT, SafeError, now_cn
from preferences import tier, rank


def slot_key(s):
    return '|'.join((s['date'], str(s['id']), s['start'], s['end']))


def validate(plan):
    if not isinstance(plan, dict) or plan.get('version') != 2:
        raise SafeError('配置必须为 version: 2；旧 example.yaml 不可直接提交。')
    if plan.get('target_order','scarcity') not in ('scarcity','configured'):
        raise SafeError('目标顺序必须为configured或scarcity。')
    groups = plan.get('targets', [])
    if not 1 <= len(groups) <= 6:
        raise SafeError('目标组数量须为 1..6。')
    seen = set()
    for g in groups:
        if not isinstance(g.get('id'), str) or g['id'] in seen:
            raise SafeError('目标 id 必须为唯一字符串。')
        seen.add(g['id'])
        d = datetime.strptime(g['date'], '%Y-%m-%d').date()
        start = datetime.strptime(g['start'], '%H:%M').time()
        end = datetime.strptime(g['end'], '%H:%M').time()
        if d < now_cn().date() or start >= end or (d == now_cn().date() and start <= now_cn().time().replace(tzinfo=None)):
            raise SafeError('日期/时间已过期或开始时间不早于结束时间。')
        courts = g.get('courts')
        if not isinstance(courts, list) or not courts or len(courts)>100 or any(not isinstance(n,str) or not n.strip() for n in courts) or len(set(courts))!=len(courts):
            raise SafeError('courts 必须是唯一完整场地名列表，禁止空名称。')
        if type(g.get('quantity')) is not int or not 1<=g['quantity']<=len(courts):
            raise SafeError('数量必须为正整数且不大于候选场地数。')
    # One wizard is scoped to one venue. Duplicate overlapping groups at one time
    # are almost always a mistaken way to request two courts; use quantity=2 instead.
    for i, left in enumerate(groups):
        for right in groups[i + 1:]:
            same_window = (left['date'], left['start'], left['end']) == (right['date'], right['start'], right['end'])
            same_candidates = set(left['courts']) == set(right['courts'])
            if same_window and same_candidates:
                raise SafeError('同一日期和时段的目标组候选有重叠；如果要两场，请合并成一个目标组并把数量填2，这是输入防误操作检查，不表示已经证实服务端会因两组配置拒绝。')
    attempts=plan.get('max_attempts_per_target',3)
    if type(attempts) is not int or not 1<=attempts<=30:
        raise SafeError('每组尝试上限必须为1–30。')
    total = sum(g['quantity'] for g in groups)
    if type(plan.get('max_orders')) is not int or not 1<=plan['max_orders']<=6 or total>plan['max_orders']:
        raise SafeError('订单上限 1..6，且须覆盖所有目标数量。')
    return plan


def choose(groups, found, completed, attempted, exhausted, return_only=None, target_order="scarcity"):
    """Maximum bipartite matching of outstanding units to currently available slots.
    Preserves feasible choices for constrained groups; not a prediction of competitors.
    """
    slots = {slot_key(s):s for s in found if s['available'] and slot_key(s) not in attempted}
    units, candidates = [], {}
    for i,g in enumerate(groups):
        if g['id'] in exhausted:
            continue
        cs = [k for k,s in slots.items() if s['date']==g['date'] and s['start']==g['start'] and s['end']==g['end'] and s['name'] in g['courts']]
        cs.sort(key=lambda k:(*rank(slots[k]['name'],g.get('priority')),g['courts'].index(slots[k]['name'])))
        for j in range(max(0,g['quantity']-completed.get(g['id'],0))):
            unit=(i,j); units.append(unit); candidates[unit]=cs
    owner = {}
    def assign(u, visited):
        for k in candidates[u]:
            if k in visited:
                continue
            visited.add(k)
            if k not in owner or assign(owner[k],visited):
                owner[k]=u
                return True
        return False
    for u in units:
        assign(u,set())
    if return_only is not None:
        owner = {k:u for k,u in owner.items() if k in return_only}
    if not owner:
        return None
    # Submit the most constrained assigned unit first; configured priority breaks ties.
    def selection_rank(pair):
        k,u=pair
        head=(u[0],len(candidates[u])) if target_order=='configured' else (len(candidates[u]),u[0])
        return (*head,candidates[u].index(k),u[1])
    k,u = min(owner.items(),key=selection_rank)
    return groups[u[0]], slots[k]


def atomic(path, obj):
    tmp=path.with_suffix('.tmp')
    with tmp.open('w',encoding='utf-8') as f:
        json.dump(obj,f,ensure_ascii=False,indent=2)
        f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)


@contextmanager
def exclusive(private):
    import msvcrt
    with (private/'booking.lock').open('a+b') as f:
        f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
        try:
            msvcrt.locking(f.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:
            raise SafeError('另一个预约进程正在运行。') from None
        try:
            yield
        finally:
            f.seek(0); msvcrt.locking(f.fileno(),msvcrt.LK_UNLCK,1)


def existing_keys(private):
    keys=set()
    acknowledgement=private/'cancelled-confirmations.json'
    cutoffs=json.loads(acknowledgement.read_text(encoding='utf-8')) if acknowledgement.exists() else {}
    def retained(slot, submitted_at):
        return float(submitted_at or 0)>float(cutoffs.get(slot_key(slot), -1))
    for path in private.glob('run-*.json'):
        run=json.loads(path.read_text(encoding='utf-8'))
        for a in run.get('attempts',[]):
            if a['state'] in ('unknown','submitting'):
                raise SafeError('历史预约结果未知，须先人工核对订单；禁止重新提交。')
            if a['state']=='success' and retained(a['slot'], a.get('submitted_at')):
                keys.add(slot_key(a['slot']))
    # One-shot development test from the initial integration. Never echo its raw response.
    legacy=private/'single-test.json'
    if legacy.exists():
        obj=json.loads(legacy.read_text(encoding='utf-8'))
        if obj.get('state')=='unknown':
            raise SafeError('早期单笔测试结果未知，请先人工核对。')
        if obj.get('state')=='server_reported_success':
            s=obj['target'].copy(); s['start']=s['start'][:5]; s['end']=s['end'][:5]
            if retained(s, obj.get('submitted_at')):
                keys.add(slot_key(s))
    return keys


def run(plan, client, private=None, notify=print, max_reads=60, max_seconds=60, prepared=None, before_first_submit=None, context=None):
    validate(plan)
    private=Path(private or ROOT/'private')
    private.mkdir(exist_ok=True)
    with exclusive(private):
        prior=existing_keys(private)
        plan_id=str(uuid.uuid4())
        record={'run_id':plan_id,'created_at':now_cn().isoformat(),'plan':plan,'attempts':[], 'observations':[], 'state':'running',
                'context':context or {'mode':'scheduled' if before_first_submit else 'immediate'},'credited_slots':[]}
        path=private/('run-'+plan_id+'.json')
        atomic(path,record)
        completed={}; attempted=set(prior); counts={}; exhausted=set()
        deadline=time.monotonic()+max_seconds
        pending_gate=before_first_submit
        prepared_batch=prepared is not None and before_first_submit is not None
        gate_released_at=None
        boundary_retry_used=False
        rejection_streak={}
        rejection_refresh_used=False
        try:
            for _ in range(max_reads):
                if all(completed.get(g['id'],0)>=g['quantity'] or g['id'] in exhausted for g in plan['targets']):
                    break
                if time.monotonic()>=deadline:
                    break
                found=[]
                if prepared is not None:
                    found=[dict(s) for s in prepared]
                    prepared=None
                    record.setdefault('first_selection_source','prefetched_today')
                else:
                    for date in dict.fromkeys(g['date'] for g in plan['targets']):
                        if time.monotonic()>=deadline:
                            break
                        found.extend(client.slots(date))
                observation={'observed_at':now_cn().isoformat(),'targets':[]}
                for g in plan['targets']:
                    matching=[s for s in found if s['date']==g['date'] and s['start']==g['start'] and s['end']==g['end'] and s['name'] in g['courts']]
                    observation['targets'].append({'target':g['id'],'matched':len(matching),
                        'available':sum(s.get('observed_available',s['available']) for s in matching),
                        'eligible_for_attempt':sum(s['available'] for s in matching),
                        'unattempted_available':sum(s['available'] and slot_key(s) not in attempted for s in matching)})
                record['observations'].append(observation)
                atomic(path,record)
                # Restore credit for previously successful matching slots, without rebooking.
                historical = [dict(s, available=s['available'] or slot_key(s) in prior) for s in found]
                credited = set()
                while True:
                    credit = choose(plan['targets'], historical, completed, (attempted-prior)|credited, set(), return_only=prior,target_order=plan.get('target_order','scarcity'))
                    if credit is None:
                        break
                    g, s = credit
                    k = slot_key(s)
                    completed[g['id']] = completed.get(g['id'], 0) + 1
                    record['credited_slots'].append(dict(s))
                    credited.add(k)
                    prior.discard(k)
                if all(completed.get(g['id'],0)>=g['quantity'] for g in plan['targets']):
                    record['state']='complete'; break
                next_item=choose(plan['targets'],found,completed,attempted,exhausted,target_order=plan.get('target_order','scarcity'))
                if next_item is None and pending_gate is not None:
                    raise SafeError('提前准备未找到可提交目标；已取消定时，不在12点改为盲查。')
                if next_item is None:
                    time.sleep(min(1,max(0,deadline-time.monotonic())))
                    continue
                group,slot=next_item
                if time.monotonic()>=deadline:
                    break
                if len([x for x in record['attempts'] if x['state']=='success'])>=plan['max_orders']:
                    break
                a={'target':group['id'],'slot':slot,'state':'submitting','submitted_at':time.time()}
                record['attempts'].append(a); atomic(path,record)  # durable intent before POST
                counts[group['id']]=counts.get(group['id'],0)+1
                attempted.add(slot_key(slot))
                if pending_gate is not None:
                    a['prepared_at']=a['submitted_at']
                    try:
                        # The intent is already durable. No GET or fsync between this gate and POST.
                        record['schedule']=pending_gate()
                    except BaseException as exc:
                        record.update(state='not_submitted',gate_error_type=type(exc).__name__)
                        a.update(state='not_submitted',reason='interrupted_before_send')
                        atomic(path,record)
                        raise
                    pending_gate=None
                    gate_released_at=time.monotonic()
                    deadline=gate_released_at+max_seconds
                    a['submitted_at']=time.time()
                timing_count=len(getattr(client,'timings',[]))
                try:
                    result=client.submit(slot)
                except Exception:
                    result={'state':'unknown','reason':'unhandled_submission_error'}
                a.update(result)
                events=getattr(client,'timings',[])
                if len(events)>timing_count and events[-1].get('endpoint')=='booking':
                    a['request_timing']=dict(events[-1])
                if a['state']=='success':
                    a['estimated_payment_deadline']=a.get('request_timing',{}).get('local_send',a['submitted_at'])+300
                atomic(path,record)
                if a['state']=='success':
                    if prepared_batch:
                        # Distinct configured time slots already have IDs. Reuse them after a
                        # confirmed success; explicit rejections also advance through prepared IDs.
                        prepared=found
                    completed[group['id']]=completed.get(group['id'],0)+1
                    notify('\a预约接口返回成功：'+slot['name']+' '+slot['date']+' '+slot['start']+'–'+slot['end']+'；请立即到小程序核对并付款（五分钟为估计，以页面为准）。')
                elif a['state'] in ('unknown','blocked'):
                    record['state']=a['state']
                    notify('停止：'+a['reason']+'；错误码 '+str(a.get('err_code','未知'))+'。已有订单保留，不自动取消或重试。')
                    break
                elif a['state'] in ('sold_out','rejected'):
                    if a['state']=='rejected' and a.get('definitive_rejection') is not True:
                        a.update(state='unknown',reason='unverified_rejection')
                        record['state']='unknown'; break
                    if prepared_batch:
                        prepared=found
                    category=a.get('category')
                    near_boundary=(gate_released_at is not None and time.monotonic()-gate_released_at<=3)
                    if (a.get('definitive_rejection') is True and category in ('not_open_yet','outside_booking_window')
                            and near_boundary and not boundary_retry_used):
                        # One bounded exception: explicit timing rejection immediately at release
                        # does not consume the preferred court. Client still enforces >=1s spacing.
                        attempted.discard(slot_key(slot));boundary_retry_used=True
                        a['action']='retry_same_slot_once_at_release_boundary'
                        notify('服务端明确提示预约时间未到/不在窗口：在正常请求间隔后保留首选重试一次。')
                    else:
                        fingerprint=(a.get('err_code'),a.get('message_digest'))
                        prev,count=rejection_streak.get(group['id'],(None,0))
                        count=count+1 if prev==fingerprint else 1
                        rejection_streak[group['id']]=(fingerprint,count)
                        if (prepared_batch and not rejection_refresh_used and count>=3 and a.get('message_digest')):
                            prepared=None;rejection_refresh_used=True
                            a['action']='refresh_once_after_repeated_rejection'
                            notify('连续3次相同拒绝：只读刷新一次可用性，避免继续逐个提交过期快照。')
                        else:
                            a['action']='next_candidate'
                        notify('本候选明确拒绝：'+str(category or a.get('reason','未知'))+'；错误码 '+str(a.get('err_code','未提供'))+'；继续备选。')
                else:
                    record['state']='unknown'; break
                if counts[group['id']]>=plan.get('max_attempts_per_target',max(3,group['quantity'])):
                    exhausted.add(group['id'])
                if all(completed.get(g['id'],0)>=g['quantity'] for g in plan['targets']):
                    record['state']='complete'; break
        finally:
            if record['state']=='running':
                record['state']='partial_or_exhausted'
            record['completed']=completed
            record['timings']=list(getattr(client,'timings',[]))
            atomic(path,record)
        return record
