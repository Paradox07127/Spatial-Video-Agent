import json
import sys
from types import SimpleNamespace

import pytest

from app.core import trace
from app.tests.unit.test_store import store, index_db
from pipeline.common.schema_check import schema_errors


def test_local_first_blob_and_disabled(store):
    run = trace.start_run(store, 'q', {})
    with trace.traced(run, 'parse', {'large': 'a' * 9000}, state='parse') as t:
        assert store.get_run(run.run_id).steps[0].exec_state == 'waiting'
        t.output({'large': 'b' * 9000})
        t.evidence(['e_' + 'a' * 16])
    assert run.read_eids == {'e_' + 'a' * 16}
    trace.finish_run(run, 'done', final='UNRESOLVED', claims=[])
    saved = store.get_run(run.run_id)
    s = saved.steps[0]
    assert s.blob_ref and s.trace_delivery_state == 'disabled'
    assert s.input == {'large': 'a' * 9000} and s.output == {'large': 'b' * 9000}
    assert json.loads(store.read_blob(s.blob_ref)) == {'input': {'large': 'a' * 9000}, 'output': {'large': 'b' * 9000}}
    assert schema_errors(saved.model_dump(mode='json'), 'agent_run.json') == []
    raw = json.loads(store.app.execute('SELECT steps FROM agent_runs').fetchone()[0])[0]
    assert len(json.dumps(raw)) < 8192


def test_weave_failure_keeps_full_run_and_retries(store, monkeypatch):
    monkeypatch.setenv('WANDB_API_KEY', 'test-only')
    monkeypatch.setenv('WEAVE_PROJECT', 'test/project')
    def fail(*args, **kwargs):
        raise RuntimeError('offline')
    monkeypatch.setitem(sys.modules, 'weave', SimpleNamespace(init=fail))
    run = trace.start_run(store, 'q', {})
    with trace.traced(run, 'parse', {'q': 'q'}, state='parse') as t:
        t.output({'parsed': True})
    trace.finish_run(run, 'done', final='NO_MATCH', claims=[])
    saved = store.get_run(run.run_id)
    assert saved.status == 'done' and saved.steps[0].output == {'parsed': True}
    assert saved.steps[0].trace_delivery_state == 'weave_failed'
    assert (store.runs_dir / 'outbox.jsonl').is_file()
    calls = []
    class Client:
        def create_call(self, name, inputs, parent=None):
            call = SimpleNamespace(id=str(len(calls)), ui_url='https://example.test/call')
            calls.append((name, inputs, parent, call))
            return call
        def finish_call(self, call, output=None, exception=None):
            pass
        def flush(self):
            pass
    monkeypatch.setitem(sys.modules, 'weave', SimpleNamespace(init=lambda *a, **k: Client()))
    assert trace.flush_outbox() == 1
    assert calls[0][0] == 'sva.ask' and calls[1][2] is calls[0][3]
    assert store.get_run(run.run_id).steps[0].trace_delivery_state == 'weave_ok'


def test_exception_and_nested_calls(store, monkeypatch):
    run = trace.start_run(store, 'q', {})
    with pytest.raises(ValueError):
        with trace.traced(run, 'parse', {}, state='parse') as parent:
            with trace.traced(run, 'tool.search', {}, state='retrieve', parent_step_id=parent.step_id):
                raise ValueError('bad')
    trace.finish_run(run, 'aborted', final=None, claims=[])
    saved = store.get_run(run.run_id)
    assert saved.status == 'aborted'
    assert all(s.exec_state == 'failed' for s in saved.steps)
    assert saved.steps[1].parent_step_id == saved.steps[0].step_id


def test_cache_replay_new_run(store):
    from app.core.cache import put, replay, qhash
    from app.tests.unit.test_store import SVA
    frames = json.loads((SVA / 'fixtures/asks.json').read_text())['asks'][0]['events']
    from app.core.models import AgentRun
    original = AgentRun.model_validate(json.loads((SVA / 'fixtures/runs/r_fixturewh0001.json').read_text()))
    rid = store.begin_run(original.q, original.context)
    store.add_evidence(rid, original.evidence)
    store.set_hits(rid, original.hits)
    store.finalize_run(rid, 'done', original.claims, final=original.final)
    put(store, '  Hello  WORLD ', {'b': 2, 'a': 1}, frames, 'duckdb', rid)
    assert qhash('hello world', {'a': 1, 'b': 2}) == qhash('  Hello  WORLD ', {'b': 2, 'a': 1})
    run, replayed = replay(store, 'hello world', {'a': 1, 'b': 2})
    assert run.run_id != rid and run.source_run_id == rid
    assert run.steps[0].name == 'cache_replay'
    assert all(f['data']['run_id'] == run.run_id for f in replayed)
    assert next(f for f in replayed if f['event'] == 'status')['data']['cached'] is True
    from pipeline.common.schema_check import check_sse_sequence
    assert check_sse_sequence(replayed) == []


def test_cache_misses_after_index_rebuild(store, monkeypatch):
    from app.core.cache import put, replay
    rid = store.begin_run('q', {})
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    put(store, 'q', {}, [], 'duckdb', rid)
    assert replay(store, 'q', {}) is not None
    counts = store.index_counts()
    monkeypatch.setattr(store, 'index_counts', lambda: {**counts, 'events': counts['events'] + 1})
    assert replay(store, 'q', {}) is None

def test_cache_misses_after_code_change(store, monkeypatch):
    from app.core import cache
    rid = store.begin_run('q', {})
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    cache.put(store, 'q', {}, [], 'duckdb', rid)
    assert cache.replay(store, 'q', {}) is not None
    monkeypatch.setattr(cache, 'CODE_REV', 'other')
    assert cache.replay(store, 'q', {}) is None

def test_blob_run_survives_restart(store):
    from app.core.store import Store
    run = trace.start_run(store, 'q', {})
    with trace.traced(run, 'parse', {}, state='parse') as t:
        t.output({'raw': 'x' * 9000})
    trace.finish_run(run, 'done', final='NO_MATCH', claims=[])
    path, index, assets = store.app_path, store.index_path, store.asset_dir
    store.close()
    other = Store(index_path=index, app_path=path, asset_dir=assets)
    try:
        saved = other.get_run(run.run_id)
        assert saved.status == 'done' and len(saved.steps) == 1
        assert json.loads(other.read_blob(saved.steps[0].blob_ref))['output']['raw'] == 'x' * 9000
    finally:
        other.close()


def test_weave_child_failure_does_not_change_local_result(store, monkeypatch):
    monkeypatch.setenv('WANDB_API_KEY', 'test-only')
    monkeypatch.setenv('WEAVE_PROJECT', 'test/project')
    class Client:
        def create_call(self, name, inputs, parent=None):
            if name != 'sva.ask':
                raise RuntimeError('child unavailable')
            return SimpleNamespace(id='root', ui_url='https://example.test/call')
        def finish_call(self, *args, **kw):
            pass
        def flush(self):
            pass
    monkeypatch.setitem(sys.modules, 'weave', SimpleNamespace(init=lambda *a, **k: Client()))
    run = trace.start_run(store, 'q', {})
    with trace.traced(run, 'parse', {}, state='parse') as t:
        t.output({'ok': True})
    trace.finish_run(run, 'done', final='NO_MATCH', claims=[])
    saved = store.get_run(run.run_id)
    assert saved.steps[0].output == {'ok': True} and saved.status == 'done'
    assert saved.steps[0].trace_delivery_state == 'weave_failed'


def _weave_delivery(store, monkeypatch, flush):
    import threading, time
    monkeypatch.setenv('WANDB_API_KEY', 'test-only')
    monkeypatch.setenv('WEAVE_PROJECT', 'test/project')
    class Client:
        def create_call(self, name, inputs, parent=None):
            return SimpleNamespace(id=name, ui_url='https://example.test/call')
        def finish_call(self, *args, **kw):
            pass
    Client.flush = flush
    monkeypatch.setitem(sys.modules, 'weave', SimpleNamespace(init=lambda *a, **k: Client()))
    before = set(threading.enumerate())
    run = trace.start_run(store, 'q', {})
    with trace.traced(run, 'parse', {}, state='parse') as t:
        t.output({'ok': True})
    t0 = time.monotonic()
    url = trace.finish_run(run, 'done', final='NO_MATCH', claims=[])
    returned_s = time.monotonic() - t0
    for th in set(threading.enumerate()) - before:
        th.join(10)
    return run, url, returned_s


def test_finish_run_does_not_wait_for_weave_flush(store, monkeypatch):
    import time
    run, url, returned_s = _weave_delivery(store, monkeypatch, lambda self: time.sleep(3))
    assert returned_s < 0.5
    assert url == 'https://example.test/call'
    saved = store.get_run(run.run_id)
    assert saved.status == 'done' and saved.weave_url == 'https://example.test/call'
    assert [s.trace_delivery_state for s in saved.steps] == ['weave_ok']


def test_finish_run_flush_failure_goes_to_outbox(store, monkeypatch):
    def fail(self):
        raise RuntimeError('offline')
    run, url, returned_s = _weave_delivery(store, monkeypatch, fail)
    saved = store.get_run(run.run_id)
    assert saved.status == 'done' and saved.weave_url is None
    assert [s.trace_delivery_state for s in saved.steps] == ['weave_failed']
    rows = (store.runs_dir / 'outbox.jsonl').read_text().splitlines()
    assert [json.loads(r)['run_id'] for r in rows] == [run.run_id]


def test_site_blob_uploaded_and_read_after_restart(store, monkeypatch):
    from io import BytesIO
    monkeypatch.setenv('MODE', 'site')
    monkeypatch.setenv('ASSET_BUCKET', 'test-bucket')
    store.settings = __import__('app.settings', fromlist=['Settings']).Settings()
    objects = {}
    class S3:
        def put_object(self, Bucket, Key, Body, **kw):
            objects[(Bucket, Key)] = Body
        def get_object(self, Bucket, Key):
            return {'Body': BytesIO(objects[(Bucket, Key)])}
    monkeypatch.setattr(store, 's3', lambda: S3())
    run = trace.start_run(store, 'q', {})
    with trace.traced(run, 'parse', {}, state='parse') as t:
        t.output('z' * 9000)
    trace.finish_run(run, 'done', final='NO_MATCH', claims=[])
    ref = store.get_run(run.run_id).steps[0].blob_ref
    assert ref.uri.startswith('s3://test-bucket/sva/runs/')
    assert json.loads(store.read_blob(ref))['output'] == 'z' * 9000
