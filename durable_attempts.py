"""Small synchronous recovery records; reports are never the v4 source of truth.

Intent and result are separate files. A missing/torn result leaves its intent
unresolved and blocks restart. No credentials or raw order bodies are persisted.
"""
import hashlib
import json
import math
import os
import time
from pathlib import Path

from safe_errors import SafeError

SLOT_FIELDS = ('id', 'date', 'name', 'start', 'end')
STATES = {'success', 'sold_out', 'rejected', 'unknown', 'blocked', 'not_submitted'}


def write_record(path, obj):
    start = time.perf_counter_ns()
    cpu = time.thread_time_ns()
    payload = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    encoded = time.perf_counter_ns()
    tmp = path.with_suffix('.tmp')
    with tmp.open('wb') as f:
        opened = time.perf_counter_ns()
        f.write(payload)
        written = time.perf_counter_ns()
        f.flush()
        flushed = time.perf_counter_ns()
        os.fsync(f.fileno())
        synced = time.perf_counter_ns()
    closed = time.perf_counter_ns()
    os.replace(tmp, path)
    replaced = time.perf_counter_ns()
    return dict(serialization_ms=(encoded-start)/1e6, open_ms=(opened-encoded)/1e6,
                write_ms=(written-opened)/1e6, flush_ms=(flushed-written)/1e6,
                fsync_ms=(synced-flushed)/1e6, close_ms=(closed-synced)/1e6,
                replace_ms=(replaced-closed)/1e6, total_ms=(replaced-start)/1e6,
                thread_cpu_ms=(time.thread_time_ns()-cpu)/1e6, bytes=len(payload))


class AttemptStore:
    def __init__(self, private, run_id, task_id=None):
        self.private = Path(private)
        self.run_id = run_id
        self.task_id = task_id

    def persist(self, attempt, number, *, result=False):
        identity = f'{self.run_id}-{number:03d}'
        attempt['attempt_id'] = identity
        obj = dict(version=1, attempt_id=identity, run_id=self.run_id,
                   task_id=self.task_id, target=attempt['target'],
                   slot={k:attempt['slot'][k] for k in SLOT_FIELDS},
                   submitted_at=attempt['submitted_at'],
                   state=attempt['state'] if result else 'submitting')
        if result:
            if obj['state'] not in STATES:
                obj['state'] = 'unknown'
            # Hash only: enough to associate evidence, not a usable order number.
            if obj['state'] == 'success' and attempt.get('order_id') is not None:
                obj['order_ref'] = hashlib.sha256(str(attempt['order_id']).encode()).hexdigest()
            obj['recorded_at'] = time.time()
        return write_record(self.private / f"{'result' if result else 'intent'}-{identity}.json", obj)


def recover(private):
    """Return minimal attempts. Malformed/orphan records fail closed, without raw errors."""
    private = Path(private)
    records = {}
    try:
        for p in private.glob('intent-*.json'):
            intent = json.loads(p.read_text(encoding='utf-8'))
            if not isinstance(intent, dict):
                raise ValueError()
            key = p.stem[len('intent-'):]
            if intent['version'] != 1 or intent['attempt_id'] != key or intent['state'] != 'submitting':
                raise ValueError()
            if not all(isinstance(intent['slot'][k], str) and intent['slot'][k] for k in SLOT_FIELDS):
                raise ValueError()
            if not isinstance(intent['run_id'], str) or not key.startswith(intent['run_id']+'-'):
                raise ValueError()
            if not math.isfinite(float(intent['submitted_at'])): raise ValueError()
            result = private / f'result-{key}.json'
            value = intent
            if result.exists():
                value = json.loads(result.read_text(encoding='utf-8'))
                if not isinstance(value, dict):
                    raise ValueError()
                if (value.get('version') != 1 or value.get('state') not in STATES or
                        any(value.get(k) != intent[k] for k in ('attempt_id','run_id','task_id','target','slot'))):
                    raise ValueError()
                if not math.isfinite(float(value['submitted_at'])): raise ValueError()
            records[key] = value
        for p in private.glob('result-*.json'):
            if p.stem[len('result-'):] not in records:
                raise ValueError()
    except (OSError, ValueError, TypeError, KeyError):
        raise SafeError(code='RECOVERY_RECORD_INVALID') from None
    return records
