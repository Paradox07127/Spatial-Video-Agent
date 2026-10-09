import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.core.models import AgentRun, Clip, Event, Moment, SearchFilters
from app.core.store import Store
from pipeline.common.schema_check import schema_errors

SVA = Path(__file__).resolve().parents[3]
WH = 'wh-sdg-fixture-01'
FL = 'fl-pie-fixture-01'


@pytest.fixture(scope='session')
def index_db(tmp_path_factory):
    root = tmp_path_factory.mktemp('a1-index')
    db = root / 'index.duckdb'
    subprocess.run([sys.executable, str(SVA / 'fixtures/gen_fixture.py'), '--no-media', '--out', str(root), '--index', str(db)], check=True, capture_output=True)
    return db


@pytest.fixture
def store(index_db, tmp_path, monkeypatch):
    monkeypatch.setenv('RUNS_DIR', str(tmp_path / 'runs'))
    monkeypatch.delenv('WANDB_API_KEY', raising=False)
    monkeypatch.delenv('WEAVE_PROJECT', raising=False)
    s = Store(index_path=index_db, app_path=tmp_path / 'app.duckdb', asset_dir=SVA / 'fixtures/sva-out')
    yield s
    s.close()


def test_moment_visibility_and_missing_video(store):
    m = Moment.model_validate(store.moment(WH, 4600))
    p = next(p for p in m.persons if p.entity_id == 'P1')
    assert 'ceiling_00' not in p.observed_in
    assert 'ceiling_00' not in p.seen_by
    assert {'ceiling_04', 'eye_00'} <= set(p.seen_by)
    assert p.xyz17 and len(p.xyz17) == 17
    assert next(c for c in store.moment(WH, 6500).cams if c.id == 'eye_00').frame_url is None


def test_search_intersection_and_two_branch_union(store):
    rows = store.index.execute('SELECT clip_id,v_txt,v_vis FROM clips WHERE scene_key=? ORDER BY clip_id', [WH]).fetchall()
    result = store.search_clips(SearchFilters(workspace='warehouse', scene_keys=[WH], t_range=(4900, 5100), k=1), list(rows[0][1]), list(rows[-1][2]))
    assert len(result) == 2
    assert {c.clip_id for c in result} == {rows[0][0], rows[-1][0]}
    assert any(c.rank_text == 1 for c in result) and any(c.rank_visual == 1 for c in result)
    for c in result:
        Clip.model_validate(c.model_dump())
        assert c.t0_ms < 5100 and c.t1_ms > 4900
    assert all(c.t0_ms == 5000 for c in store.search_clips(SearchFilters(workspace='warehouse', t_range=(5000, 5001)), None, None))
    assert store.search_clips(SearchFilters(workspace='warehouse', place_id='absent'), None, None) == []


def test_unranked_search_returns_every_filtered_clip(store):
    n = store.index.execute("SELECT count(*) FROM clips WHERE workspace='warehouse'").fetchone()[0]
    assert n > 1 and len(store.search_clips(SearchFilters(workspace='warehouse', k=1), None, None)) == n


def test_geo_reads_index_through_store(store, tmp_path, monkeypatch):
    from app.core import tools
    monkeypatch.setenv('INDEX_DUCKDB', str(tmp_path / 'absent.duckdb'))
    place = tools.geocode(store, 'Queen and Spadina').envelope.output
    assert place['place_id'] == 'pl_queen_spadina'
    assert tools.find_passes(store, place['place_id'], 100, 120).envelope.output


def test_events_and_restart(store):
    events = store.events_in(WH, 5900, 6000)
    assert not events
    assert Event.model_validate(store.events_in(WH, 4300, 4301)[0]).event_id == WH + ':E1'
    rid = store.begin_run('q', {'scene_key': WH})
    fixture = AgentRun.model_validate(json.loads((SVA / 'fixtures/runs/r_fixturewh0001.json').read_text()))
    step = fixture.steps[0].model_copy(update={'step_id': rid + '.s0'})
    store.append_step(rid, step)
    store.add_evidence(rid, fixture.evidence)
    store.set_hits(rid, fixture.hits)
    claims = [c.model_copy(update={'run_id': rid}) for c in fixture.claims]
    store.finalize_run(rid, 'done', claims, final=fixture.final)
    path, index, assets = store.app_path, store.index_path, store.asset_dir
    store.close()
    other = Store(index_path=index, app_path=path, asset_dir=assets)
    try:
        run = other.get_run(rid)
        assert run.steps == [step] and run.evidence == fixture.evidence and run.hits == fixture.hits and run.claims == claims
        assert schema_errors(run.model_dump(mode='json'), 'agent_run.json') == []
        assert len(other.list_runs(WH)) == 1
    finally:
        other.close()


def test_vastdb_load_write_count_and_fallback(index_db, tmp_path, monkeypatch):
    import duckdb
    import pyarrow as pa
    import sys
    from types import SimpleNamespace
    from contextlib import contextmanager
    from app.core.store import INDEX_TABLES, APP_KEYS
    source = duckdb.connect(str(index_db), read_only=True)
    tables = {n: source.execute(f'SELECT * FROM {n}').to_arrow_table() for n in INDEX_TABLES}
    source.close()
    class Column:
        def __init__(self, key):
            self.key = key
        def isin(self, values):
            return self.key, values
    class Table:
        def __init__(self, name):
            self.name = name
        def __getitem__(self, key):
            return Column(key)
        def select(self, columns=None, predicate=None, internal_row_id=False):
            selects.append((self.name, predicate is not None))
            rows = tables[self.name]
            if internal_row_id:
                rows = rows.append_column('$row_id', pa.array(range(rows.num_rows), type=pa.uint64()))
            if predicate:
                key, values = predicate
                rows = rows.take(pa.array([i for i, v in enumerate(rows[key].to_pylist()) if v in values], type=pa.int64()))
            if columns:
                rows = rows.select(columns + (['$row_id'] if internal_row_id else []))
            return SimpleNamespace(read_all=lambda: rows)
        def insert(self, rows):
            tables[self.name] = pa.concat_tables([tables[self.name], rows])
        def update(self, rows):
            original = tables[self.name].to_pylist()
            for row in rows.to_pylist():
                i = row.pop('$row_id')
                original[i] = row
            tables[self.name] = pa.Table.from_pylist(original, schema=tables[self.name].schema)
    class Schema:
        def table(self, name, fail_if_missing=True):
            if name not in tables:
                assert not fail_if_missing
                return None
            return Table(name)
        def create_table(self, name, columns):
            tables[name] = pa.Table.from_batches([], schema=columns)
            return Table(name)
    schema = Schema()
    selects = []
    @contextmanager
    def transaction():
        yield SimpleNamespace(bucket=lambda name: SimpleNamespace(schema=lambda name: schema))
    monkeypatch.setitem(sys.modules, 'vastdb', SimpleNamespace(connect=lambda **kw: SimpleNamespace(transaction=transaction)))
    s = Store(index_path=None, app_path=tmp_path / 'vdb.duckdb', asset_dir=SVA / 'fixtures/sva-out', backend='vastdb')
    selects.clear()
    rid = s.begin_run('q', {'scene_key': WH})
    s.finalize_run(rid, 'done', [], final='NO_MATCH')
    assert tables['agent_runs'].num_rows == 1
    # a single-row mirror reads back its own key, never the whole key column
    assert selects and all(by_key for _, by_key in selects)
    assert s.write_vastdb({'agent_runs': tables['agent_runs']}) == {'agent_runs': 1}
    assert tables['agent_runs']['status'].to_pylist() == ['done']
    assert s.search_clips(SearchFilters(workspace='warehouse'), None, None)
    insert = Table.insert
    monkeypatch.setattr(Table, 'insert', lambda self, rows: None)
    with pytest.raises(RuntimeError, match='row count'):
        s.cache_put('hash', 'q', {}, {'frames': []}, 'vastdb', rid)
    monkeypatch.setattr(Table, 'insert', insert)
    s.close()
    # two app instances on the same VastDB can insert the same key; a restart must still load
    tables['agent_runs'] = pa.concat_tables([tables['agent_runs'], tables['agent_runs']])
    restarted = Store(index_path=None, app_path=tmp_path / 'restart.duckdb', asset_dir=SVA / 'fixtures/sva-out', backend='vastdb')
    assert restarted.get_run(rid).final == 'NO_MATCH'
    restarted.close()
    def fail(**kw):
        raise RuntimeError('offline')
    monkeypatch.setitem(sys.modules, 'vastdb', SimpleNamespace(connect=fail))
    with pytest.raises(RuntimeError):
        Store(index_path=index_db, app_path=tmp_path / 'fail.duckdb', asset_dir=None, backend='vastdb')
    fallback = Store(index_path=index_db, app_path=tmp_path / 'fallback.duckdb', asset_dir=None, backend='vastdb', allow_duckdb_fallback=True)
    assert fallback.backend == 'duckdb' and fallback.index_counts()['clips'] == 8
    fallback.close()


def test_geocode_skips_places_without_a_name():
    from types import SimpleNamespace
    from app.core import geo
    rows = [{'place_id': 'w123', 'kind': None, 'name': None, 'aliases': [], 'lat': 43.6, 'lon': -79.4},
            {'place_id': 'pl_rom', 'kind': 'landmark', 'name': 'Royal Ontario Museum', 'aliases': ['ROM'], 'lat': 43.6677, 'lon': -79.3948}]
    store = SimpleNamespace(index=None, rows=lambda db, sql, params=(): rows)
    assert geo.geocode(store, 'ROM')['place_id'] == 'pl_rom'


def test_search_clips_zero_query_vector_is_unranked(store):
    every = store.search_clips(SearchFilters(workspace='warehouse', k=1), None, None)
    assert len(store.search_clips(SearchFilters(workspace='warehouse', k=1), [0.0]*256, [0.0]*256)) == len(every) > 1


def test_live_vss_rows_join_the_rrf_of_their_mapped_clip(store, monkeypatch):
    import pyarrow as pa
    from contextlib import contextmanager
    from types import SimpleNamespace
    rows = store.index.execute('SELECT clip_id,v_txt FROM clips WHERE scene_key=? ORDER BY clip_id', [WH]).fetchall()
    q = list(rows[0][1])
    top = store.search_clips(SearchFilters(workspace='warehouse'), q, None)
    ours_only, both = top[0], top[-1]
    assert ours_only.clip_id == rows[0][0] and both.rank_text > 1
    mapped = {'source': 's3://seg/a.mp4', 'original_video': 's3://chunk/a.mp4', 'segment_start_sec': 0.0, 'scene_key': WH,
              'cam_id': both.cam_id, 't_ms': both.t0_ms, 'clip_id': both.clip_id}
    gone = {**mapped, 'source': 's3://seg/b.mp4', 'segment_start_sec': 5.0, 'clip_id': WH + ':ceiling_00:00009'}
    live = pa.table({'source': ['s3://seg/moved.mp4', 's3://seg/b.mp4', 's3://seg/c.mp4'],
                     'original_video': ['s3://chunk/a.mp4', 's3://chunk/a.mp4', 's3://chunk/c.mp4'],
                     'segment_start_sec': [0.0, 5.0, 0.0], 'segment_end_sec': [5.0, 10.0, 5.0],
                     'reasoning_content': ['a', 'b', 'c'], 'object_counts': ['{"person": 1}', None, None],
                     'camera_id': ['x', 'x', 'y'], 'location': ['w', 'w', 'w'],
                     'vectors': [q, q, q], 'vectors_visual': [None, q, q]})
    seen = {}
    def select(columns):
        seen['columns'] = columns
        return SimpleNamespace(read_all=lambda: live.select(columns))
    @contextmanager
    def transaction():
        yield SimpleNamespace(bucket=lambda b: SimpleNamespace(schema=lambda s: SimpleNamespace(table=lambda t: SimpleNamespace(select=select))))
    def connect(**kw):
        seen['connect'] = kw
        return SimpleNamespace(transaction=transaction)
    monkeypatch.setitem(sys.modules, 'vastdb', SimpleNamespace(connect=connect))
    store.load_vss_collection(({mapped['source']: mapped, gone['source']: gone},
                               {(mapped['original_video'], 0.0): mapped, (gone['original_video'], 5.0): gone}))
    assert seen['connect']['ssl_verify'] is False and 'vectors_visual' in seen['columns']
    assert store.index_counts()['vss_collection'] == 1
    ranked = store.search_clips(SearchFilters(workspace='warehouse'), q, None)
    first = ranked[0]
    assert first.clip_id == both.clip_id and first.rank_vss_text == 1 and first.sim_vss_text == pytest.approx(1)
    assert next(c for c in ranked if c.clip_id == ours_only.clip_id).rank_vss_text is None
    others = sorted({c.cam_id for c in top} - {both.cam_id})
    assert all(c.rank_vss_text is None for c in store.search_clips(SearchFilters(workspace='warehouse', cam_ids=others), q, None))
    assert store.search_clips(SearchFilters(workspace='warehouse'), q, q)[0].rank_vss_visual is None
    def offline(**kw):
        raise RuntimeError('offline')
    monkeypatch.setitem(sys.modules, 'vastdb', SimpleNamespace(connect=offline))
    store.load_vss_collection(({}, {}))
    assert 'vss_collection' not in store.index_counts()
    assert store.search_clips(SearchFilters(workspace='warehouse'), q, None)[0].clip_id == ours_only.clip_id


def test_run_survives_vastdb_mirror_failure(store):
    def reject(tables):
        raise RuntimeError('TabularValueTooLarge')
    store.backend = 'vastdb'
    store.write_vastdb = lambda tables: None
    rid = store.begin_run('q', {'scene_key': WH})
    store.write_vastdb = reject
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    assert store.get_run(rid).status == 'done'


def test_live_caption_fills_a_null_street_caption_and_keeps_counts(tmp_path, monkeypatch):
    import pyarrow as pa
    from contextlib import contextmanager
    from types import SimpleNamespace
    from app.core import street
    from app.tests.unit.a5_fakes import L1_KEY, l1_assets
    monkeypatch.setenv('RUNS_DIR', str(tmp_path / 'runs'))
    index, assets = l1_assets(tmp_path)
    s = Store(index_path=index, app_path=tmp_path / 'app.duckdb', asset_dir=assets)
    clips = s.search_clips(SearchFilters(workspace='street'), None, None)
    kept, filled = clips
    assert kept.caption and filled.caption is None
    hits = [{'source': c.vss_source, 'original_video': c.video_uri, 'segment_start_sec': c.t0_ms / 1000, 'scene_key': L1_KEY,
             'cam_id': c.cam_id, 't_ms': c.t0_ms, 'clip_id': c.clip_id} for c in clips]
    live = pa.table({'source': [h['source'] for h in hits], 'original_video': [h['original_video'] for h in hits],
                     'segment_start_sec': [0.0, 5.0], 'segment_end_sec': [5.0, 10.0],
                     'reasoning_content': ['live caption a', 'live caption b'],
                     'object_counts': ['{"person": 3}', '{"car": 4, "person": 2}'], 'camera_id': ['c', 'c'], 'location': ['l', 'l'],
                     'vectors': [[1.0] * 256] * 2, 'vectors_visual': [None, None]})
    @contextmanager
    def transaction():
        table = SimpleNamespace(select=lambda columns: SimpleNamespace(read_all=lambda: live.select(columns)))
        yield SimpleNamespace(bucket=lambda b: SimpleNamespace(schema=lambda x: SimpleNamespace(table=lambda t: table)))
    monkeypatch.setitem(sys.modules, 'vastdb', SimpleNamespace(connect=lambda **kw: SimpleNamespace(transaction=transaction)))
    s.load_vss_collection(({h['source']: h for h in hits}, {}))
    assert s.index_counts()['vss_collection'] == 2
    for ranked in (s.search_clips(SearchFilters(workspace='street'), None, None), s.search_clips(SearchFilters(workspace='street'), [1.0] * 256, None)):
        captions = {c.clip_id: c.caption for c in ranked}
        assert captions == {kept.clip_id: kept.caption, filled.clip_id: 'live caption b'}
    segments = street.segments(s, L1_KEY)
    assert [(x['caption'], x['counts']) for x in segments] == [(kept.caption, {'person': 3}), ('live caption b', {'car': 4, 'person': 2})]
    s.close()


def test_scene_reads_are_parsed_once(store, monkeypatch):
    reads = []
    rows, asset_json = store.rows, store.asset_json
    monkeypatch.setattr(store, 'rows', lambda db, sql, params=(): reads.append(sql) or rows(db, sql, params))
    monkeypatch.setattr(store, 'asset_json', lambda key, rel: reads.append(rel) or asset_json(key, rel))
    first = (store.manifest(WH), store.entities(WH), store.events_in(WH, 4300, 4301))
    n = len(reads)
    assert n and (store.manifest(WH), store.entities(WH), store.events_in(WH, 4300, 4301)) == first and len(reads) == n
    assert not store.events_in(WH, 5900, 6000) and len(reads) == n


def test_run_mirrors_at_begin_and_finalize_outside_the_lock(store):
    log = []
    store.backend = 'vastdb'
    store.write_vastdb = lambda tables: log.append((tables['agent_runs']['status'].to_pylist(), store.lock._is_owned()))
    fixture = AgentRun.model_validate(json.loads((SVA / 'fixtures/runs/r_fixturewh0001.json').read_text()))
    rid = store.begin_run('q', {'scene_key': WH})
    step = fixture.steps[0].model_copy(update={'step_id': rid + '.s0'})
    store.append_step(rid, step)
    store.update_step(rid, step)
    store.add_evidence(rid, fixture.evidence)
    store.set_hits(rid, fixture.hits)
    store.finalize_run(rid, 'done', [], final='NO_MATCH')
    assert log == [(['running'], False), (['done'], False)]
    assert store.get_run(rid).evidence == fixture.evidence
