"""Deterministic maximum matching and fail-closed, bounded sequential booking."""
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from api import ROOT, SafeError, now_cn, CN
from preferences import tier, rank, court_number


def slot_key(s):
    return '|'.join((s['date'], str(s['id']), s['start'], s['end']))


def validate(plan):
    if not isinstance(plan, dict) or plan.get('version') != 2:
        raise SafeError('配置必须为 version: 2；旧 example.yaml 不可直接提交。')
    if plan.get('target_order','scarcity') not in ('scarcity','configured','round_robin'):
        raise SafeError('目标顺序必须为configured、round_robin或scarcity。')
    groups = plan.get('targets', [])
    if not 1 <= len(groups) <= 2:
        raise SafeError('目标组数量须为1..2。')
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
    if type(plan.get('max_orders')) is not int or not 1<=plan['max_orders']<=2 or total>plan['max_orders']:
        raise SafeError('订单硬上限为2，且须覆盖所有目标数量。')
    if plan.get('strategy','coverage') not in ('coverage','focus'):
        raise SafeError('strategy必须为coverage或focus。')
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
        candidate_index=groups[u[0]]['courts'].index(slots[k]['name'])
        if target_order=='configured':
            head=(u[0],len(candidates[u]))
        elif target_order=='round_robin':
            # Candidate rank is the round; tie-break by focus/group order.
            focus_index=groups[u[0]].get('_focus_order',u[0])
            head=(candidate_index,focus_index)
        else:
            head=(len(candidates[u]),u[0])
        return (*head,candidate_index,u[1])
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
    """Single in-flight request; at most ONE recovery GET across every branch."""
    from serial_queues import SerialQueues
    validate(plan)
    private=Path(private or ROOT/'private');private.mkdir(exist_ok=True)
    with exclusive(private):
        prior=existing_keys(private)
        # Conservative local evidence, NOT a claim about all orders in the live account.
        held_prior={k for k in prior if k.split('|',1)[0]>=now_cn().date().isoformat()}
        creditable=set(prior)
        plan_id=str(uuid.uuid4())
        record={'run_id':plan_id,'log_version':3,'created_at':now_cn().isoformat(),'plan':plan,
                'attempts':[],'observations':[],'state':'running','context':context or {},'credited_slots':[],
                'recovery_reads':0,'local_prior_reserved':len(held_prior),'account_evidence':'local_only_manual_account_check_required'}
        path=private/('run-'+plan_id+'.json');atomic(path,record)
        completed={};attempted=set(prior);counts={};exhausted=set()
        deadline=time.monotonic()+max_seconds;pending_gate=before_first_submit
        gate_released_at=None;boundary_used=False;retry_pending=None;post_turn=0
        new_successes=0;initial_reads=0;sold_streak={};refresh_due=False;refresh_date=None
        scheduler=SerialQueues(plan['targets'],slot_key)
        found=list(prepared) if prepared is not None else []
        needs_initial=prepared is None
        name_order={g['id']:{n:i for i,n in enumerate(g['courts'])} for g in plan['targets']}
        def observe():
            nonlocal creditable
            targets=[]
            for g in plan['targets']:
                matching=[s for s in found if s['date']==g['date'] and s['start']==g['start'] and s['end']==g['end'] and s['name'] in name_order[g['id']]]
                targets.append({'target':g['id'],'matched':len(matching),'available':sum(s.get('observed_available',s['available']) for s in matching),
                                'eligible_for_attempt':sum(s['available'] for s in matching)})
            record['observations'].append({'observed_at':now_cn().isoformat(),'targets':targets})
            historical=[dict(s,available=s['available'] or slot_key(s) in creditable) for s in found]
            while True:
                credit=choose(plan['targets'],historical,completed,attempted-creditable,set(),return_only=creditable)
                if credit is None:break
                g,sl=credit;creditable.discard(slot_key(sl))
                completed[g['id']]=completed.get(g['id'],0)+1;record['credited_slots'].append(dict(sl))
            scheduler.rebuild(found)
            atomic(path,record)
        def refresh():
            nonlocal found,refresh_due
            if record['recovery_reads']>=1 or initial_reads+record['recovery_reads']>=max_reads:return False
            if time.monotonic()>=deadline:return False
            date=refresh_date or next((g['date'] for g in plan['targets'] if completed.get(g['id'],0)<g['quantity'] and g['id'] not in exhausted),None)
            if date is None:return False
            record['recovery_reads']+=1;refresh_due=False
            fresh=client.slots(date)
            found=[s for s in found if s['date']!=date]+list(fresh)
            observe();return True
        try:
            if needs_initial:
                for date in dict.fromkeys(g['date'] for g in plan['targets']):
                    if initial_reads>=max_reads:break
                    initial_reads+=1;found.extend(client.slots(date))
                record['first_selection_source']='initial_query'
            else:record['first_selection_source']='prefetched_today'
            observe()
            while time.monotonic()<deadline:
                if getattr(client,'telemetry_incomplete',False) is True:
                    record.update(state='blocked',exit_reason='telemetry_failure_before_next_send');break
                if all(completed.get(g['id'],0)>=g['quantity'] for g in plan['targets']):
                    record.update(state='complete',exit_reason='targets_complete');break
                if all(completed.get(g['id'],0)>=g['quantity'] or g['id'] in exhausted for g in plan['targets']):
                    record['exit_reason']='attempt_budget_exhausted';break
                if len(held_prior)+new_successes>=2 or sum(completed.values())>=plan['max_orders']:
                    record.update(state='blocked',exit_reason='local_reservation_cap');break
                first_round_done=all(counts.get(g['id'],0)>0 or completed.get(g['id'],0)>=g['quantity'] or g['id'] in exhausted for g in plan['targets'])
                if refresh_due and first_round_done:refresh()
                selected=None;is_retry=False;retry_deadline=None
                if retry_pending:
                    g,sl,eligible_turn,retry_deadline=retry_pending
                    earliest=time.monotonic()
                    last=getattr(client,'last_request',None)
                    if type(last) in (int,float):earliest=max(earliest,last+getattr(client,'min_request_interval',1.0))
                    if earliest>retry_deadline or completed.get(g['id'],0)>=g['quantity'] or g['id'] in exhausted:
                        retry_pending=None;retry_deadline=None
                    elif post_turn>=eligible_turn:
                        selected=(g,sl);is_retry=True;retry_pending=None
                        scheduler.cursor=(plan['targets'].index(g)+1)%len(plan['targets'])
                if selected is None:
                    if plan.get('target_order')=='round_robin':selected=scheduler.take(completed,attempted,exhausted)
                    else:selected=choose(plan['targets'],found,completed,attempted,exhausted,target_order=plan.get('target_order','scarcity'))
                if selected is None:
                    if pending_gate is not None:
                        raise SafeError('提前准备没有可提交目标；停止，不在12点盲查。')
                    if refresh():continue
                    record['exit_reason']='no_candidates_refresh_budget_exhausted';break
                group,slot=selected;candidate_ns=time.perf_counter_ns()
                # Static request construction / identity parsing takes place BEFORE the release gate.
                primer=getattr(type(client),'prepare_submit',None)
                prepare_ns=time.perf_counter_ns()
                send=client.prepare_submit(slot) if callable(primer) else lambda:client.submit(slot)
                prepared_local_ms=(time.perf_counter_ns()-prepare_ns)/1e6
                if hasattr(client,'last_request'):client.submit_not_after=retry_deadline if is_retry else None
                index=name_order[group['id']][slot['name']]
                is_focus=plan.get('focus_target')==f"{group['start']}-{group['end']}" and tier(slot['name']) in (0,1) and court_number(slot['name'])==plan.get('focus_court_number',6)
                a={'target':group['id'],'slot':dict(slot,tier=tier(slot['name']),court_number=court_number(slot['name']),candidate_index=index,is_focus_target=is_focus),
                   'state':'submitting','submitted_at':time.time(),'candidate_index':index,'candidate_tier':tier(slot['name']),
                   'is_focus_target':is_focus,'is_boundary_retry':is_retry,'candidate_ready_mono_ns':candidate_ns,'local_prepare_ms':prepared_local_ms}
                record['attempts'].append(a)
                persist_started=time.perf_counter_ns();atomic(path,record)
                a['intent_persist_ms']=(time.perf_counter_ns()-persist_started)/1e6
                a['intent_persisted_mono_ns']=time.perf_counter_ns()
                counts[group['id']]=counts.get(group['id'],0)+1;attempted.add(slot_key(slot))
                timing_count=len(getattr(client,'timings',[]))
                if pending_gate is not None:
                    a['prepared_at']=a['submitted_at']
                    try:record['schedule']=pending_gate()
                    except BaseException as exc:
                        record.update(state='not_submitted',gate_error_type=type(exc).__name__)
                        a.update(state='not_submitted',reason='interrupted_before_send');atomic(path,record);raise
                    pending_gate=None
                    gate_released_at=time.monotonic();deadline=gate_released_at+max_seconds
                    client.release_perf_ns=record['schedule'].get('fired_perf_ns',time.perf_counter_ns())
                try:result=send()
                except Exception:result={'state':'unknown','reason':'unhandled_submission_error'}
                if record.get('schedule',{}).get('fired_epoch') is not None:
                    record['schedule'].setdefault('fired_at',datetime.fromtimestamp(record['schedule']['fired_epoch'],CN).isoformat())
                post_turn+=1;a.update(result)
                a['classification_completed_mono_ns']=time.perf_counter_ns()
                classification_ms=getattr(client,'last_classification_ms',None)
                if type(classification_ms) in (int,float):a['business_classification_ms']=round(classification_ms,4)
                events=getattr(client,'timings',[])
                if len(events)>timing_count and events[-1].get('endpoint')=='booking':a['request_timing']=dict(events[-1])
                a['submitted_at']=a.get('request_timing',{}).get('local_send',a['submitted_at'])
                if a['state']=='success':
                    a['estimated_payment_deadline']=a['submitted_at']+300
                    completed[group['id']]=completed.get(group['id'],0)+1;new_successes+=1
                # Response classification finishes BEFORE durable result IO.
                atomic(path,record)
                if getattr(client,'after_booking_result',None):client.after_booking_result()
                if getattr(client,'telemetry_incomplete',False) is True:
                    record.update(state='unknown',telemetry_incomplete=True,exit_reason='telemetry_failure');break
                if a['state']=='success':
                    notify('\a预约成功：'+slot['name']+' '+slot['start']+'–'+slot['end']+'；请到小程序核对并手动支付。')
                elif a['state'] in ('unknown','blocked'):
                    record.update(state=a['state'],exit_reason=a.get('reason','unknown'))
                    notify('停止新增提交：结果未知或账户限制；请人工核对。');break
                elif a['state']=='not_submitted':
                    a['action']='boundary_retry_expired_without_send'
                elif a['state'] in ('sold_out','rejected'):
                    if a['state']=='rejected' and a.get('definitive_rejection') is not True:
                        a.update(state='unknown',reason='unverified_rejection');record['state']='unknown';break
                    category=a.get('category')
                    near=gate_released_at is not None and time.monotonic()-gate_released_at<=3
                    # Only a clear NOT-YET-open rejection qualifies; outside-window could mean closed.
                    if a.get('boundary_retry_eligible') is True and category=='not_open_yet' and near and not boundary_used:
                        boundary_used=True
                        eligible_turn=post_turn if plan.get('strategy','coverage')=='focus' or len(plan['targets'])==1 else post_turn+1
                        retry_pending=(group,slot,eligible_turn,gate_released_at+3)
                        a['action']='queue_one_explicit_not_open_retry'
                    else:
                        sold_streak[group['id']]=sold_streak.get(group['id'],0)+1 if a['state']=='sold_out' and category=='sold_out' else 0
                        if sold_streak[group['id']]>=3 and record['recovery_reads']==0:
                            refresh_due=True;refresh_date=group['date'];a['action']='refresh_once_after_inventory_rejections'
                        else:a['action']='next_candidate'
                    notify('本候选明确失败：'+str(category or a.get('reason','未知'))+'；继续串行候选。')
                else:record.update(state='unknown',exit_reason='unrecognized_submission_result');break
                if counts[group['id']]>=plan.get('max_attempts_per_target',max(3,group['quantity'])):exhausted.add(group['id'])
        finally:
            if record['state']=='running':record['state']='partial_or_exhausted'
            record.setdefault('exit_reason','time_budget_exhausted')
            record['completed']=completed;record['initial_reads']=initial_reads
            record['timings']=list(getattr(client,'timings',[]));atomic(path,record)
        return record
