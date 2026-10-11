"""A5: put(store,q,context,frames,backend,source_run_id); replay(...) -> (AgentRun,frames) or None."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import unicodedata

from app.core import trace

APP = Path(__file__).resolve().parents[1]
CODE_REV = hashlib.sha256(b''.join(p.read_bytes() for p in sorted(APP.rglob('*.py')) if 'tests' not in p.parts)).hexdigest()


def qhash(q, context):
    normalized = ' '.join(unicodedata.normalize('NFKC', q).casefold().split())
    raw = json.dumps([normalized, context], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()


def key(store, q, context):
    # Index row counts change on every rebuild and CODE_REV on every deploy, so stale answers are never replayed.
    return qhash(q, [context, store.index_counts(), CODE_REV])


def put(store, q, context, frames, backend, source_run_id):
    store.cache_put(key(store, q, context), q, context, {'frames': frames}, backend, source_run_id)


def replay(store, q, context):
    cached = store.cache_get(key(store, q, context))
    if cached is None:
        return None
    source = store.get_run(cached['source_run_id'])
    run = trace.start_run(store, q, context, source_run_id=source.run_id)
    frames = deepcopy(cached['response']['frames'])
    ids = {f['data']['run_id'] for f in frames} | {source.run_id}
    def replace(value):
        if isinstance(value, dict):
            return {k: replace(v) for k, v in value.items()}
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, str):
            for old in ids:
                if value == old or value.startswith(old + '.s'):
                    return run.run_id + value[len(old):]
        return value
    frames = replace(frames)
    with trace.traced(run, 'cache_replay', {'source_run_id': source.run_id}, state='parse') as t:
        t.exec_state('cached')
        t.evidence([e.eid for e in source.evidence])
        t.output({'frames': len(frames)})
    store.add_evidence(run.run_id, source.evidence)
    store.set_hits(run.run_id, [m.model_copy(update={'exec_state':'cached'}) for m in source.hits])
    claims = [c.model_copy(update={'run_id': run.run_id}) for c in source.claims]
    for frame in frames:
        if frame['event'] == 'status':
            frame['data'].update(cached=True, source_run_id=source.run_id, weave_url=source.weave_url)
        if 'exec_state' in frame['data']:
            frame['data']['exec_state'] = 'cached'
        if frame['event'] == 'tool_result':
            frame['data']['envelope']['exec_state'] = 'cached'
        if frame['event'] == 'hits':
            for moment in frame['data']['moments']:
                moment['exec_state'] = 'cached'
    trace.finish_run(run, source.status, final=source.final, claims=claims)
    return store.get_run(run.run_id), frames
