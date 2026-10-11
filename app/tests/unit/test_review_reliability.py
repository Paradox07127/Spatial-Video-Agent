"""Recovery and cache regressions from the deployment review; no external services."""
import time
from types import SimpleNamespace

import pytest

from app.core import cache
from app.tests.unit.test_store import store, index_db


def test_begin_run_survives_remote_failure_and_queues_latest_row(store, monkeypatch):
    store.backend = 'vastdb'
    def offline(*args):
        raise RuntimeError('remote unavailable')
    monkeypatch.setattr(store, 'write_vastdb', offline)
    rid = store.begin_run('Find a worker', {})
    assert store.get_run(rid).status == 'running'
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    assert store.get_run(rid).status == 'done'
    pending = store.rows(store.app, 'SELECT * FROM mirror_outbox')
    assert len(pending) == 1 and pending[0]['row_key'] == rid
    sent = []
    monkeypatch.setattr(store, 'write_vastdb', lambda tables: sent.append(tables))
    later = time.time() + 3600  # past the failed row's backoff
    monkeypatch.setattr(time, 'time', lambda: later)
    assert store.retry_mirrors() == 1
    assert sent[0]['agent_runs']['status'].to_pylist() == ['done']
    assert store.rows(store.app, 'SELECT * FROM mirror_outbox') == []


def test_restore_never_overwrites_durable_local_runs(store):
    rid = store.begin_run('local', {})
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    remote = store.app.execute('SELECT * FROM agent_runs').to_arrow_table()
    rows = remote.to_pylist()
    rows[0]['status'], rows[0]['final'] = 'running', None
    import pyarrow as pa
    stale = pa.Table.from_pylist(rows, schema=remote.schema)
    store._restore_app_rows('agent_runs', stale)
    assert store.get_run(rid).status == 'done'
    assert store.get_run(rid).final == 'NO_MATCH'
    other = store.begin_run('local only', {})
    store._restore_app_rows('agent_runs', stale)
    assert store.get_run(other) is not None


def test_restore_duplicate_remote_prefers_terminal_run(store):
    rid = store.begin_run('remote', {})
    running = store.app.execute('SELECT * FROM agent_runs').to_arrow_table()
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    complete = store.app.execute('SELECT * FROM agent_runs').to_arrow_table()
    store.app.execute('DELETE FROM agent_runs')
    import pyarrow as pa
    for snapshot in [pa.concat_tables([running, complete]), pa.concat_tables([complete, running])]:
        store._restore_app_rows('agent_runs', snapshot)
        assert store.get_run(rid).final == 'NO_MATCH'
        store.app.execute('DELETE FROM agent_runs')


def test_cache_key_changes_with_model_and_release(store, monkeypatch):
    original = cache.key(store, 'Find a worker', {})
    monkeypatch.setenv('VLM_MODEL', 'different-model')
    assert cache.key(store, 'Find a worker', {}) != original
    original = cache.key(store, 'Find a worker', {})
    monkeypatch.setenv('EVIDENCE_REVISION', 'corrected-release')
    assert cache.key(store, 'Find a worker', {}) != original


def test_cache_expiration_and_missing_source_are_safe(store, monkeypatch):
    rid = store.begin_run('old', {})
    store.cache_put(cache.key(store, 'old', {}), 'old', {}, {'frames': []}, 'duckdb', rid)
    store.app.execute("UPDATE query_cache SET created=TIMESTAMP '2000-01-01'")
    assert cache.replay(store, 'old', {}) is None
    store.cache_put(cache.key(store, 'missing', {}), 'missing', {}, {'frames': []}, 'duckdb', 'r_missing')
    assert cache.replay(store, 'missing', {}) is None


def test_cache_replay_frames_name_only_persisted_steps(store):
    import json
    from app.core.models import AgentRun
    from app.tests.unit.test_store import SVA
    frames = json.loads((SVA / 'fixtures/asks.json').read_text())['asks'][0]['events']
    original = AgentRun.model_validate(json.loads((SVA / 'fixtures/runs/r_fixturewh0001.json').read_text()))
    rid = store.begin_run(original.q, original.context)
    store.finalize_run(rid, 'done', original.claims, final=original.final)
    cache.put(store, original.q, original.context, frames, 'duckdb', rid)
    run, replayed = cache.replay(store, original.q, original.context)
    refs = set()
    def walk(v):
        if isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
        elif isinstance(v, str) and v.startswith(run.run_id + '.'):
            refs.add(v)
    walk(replayed)
    assert refs and refs <= {s.step_id for s in run.steps}
