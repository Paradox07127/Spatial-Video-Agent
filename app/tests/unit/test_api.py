import json

import pytest
from fastapi.testclient import TestClient

from app.tests.unit.test_store import WH, FL, SVA, index_db
from app.core.models import Moment, CosmosRecord, TriggerResult, Action, Zone
from pipeline.common.schema_check import schema_errors


@pytest.fixture
def client(index_db, tmp_path, monkeypatch):
    for key, value in {'MODE': 'dev', 'DATA_BACKEND': 'duckdb', 'INDEX_DUCKDB': str(index_db), 'APP_DUCKDB': str(tmp_path / 'app.duckdb'), 'RUNS_DIR': str(tmp_path / 'runs'), 'ASSET_DIR': str(SVA / 'fixtures/sva-out')}.items():
        monkeypatch.setenv(key, value)
    from app.main import create_app
    with TestClient(create_app()) as c:
        yield c


def test_site_mode_builds_vss_without_login(index_db, tmp_path, monkeypatch):
    site_map = tmp_path / 'site_map.json'
    row = {'source': 's3://vss/seg.mp4', 'original_video': 'cam.mp4', 'segment_start_sec': 5.0, 'scene_key': WH,
           'cam_id': 'ceiling_04', 't_ms': 5000}
    site_map.write_text(json.dumps({'map': {row['source']: row}}))
    for key, value in {'MODE': 'site', 'DATA_BACKEND': 'duckdb', 'ALLOW_DUCKDB_FALLBACK': '1', 'INDEX_DUCKDB': str(index_db),
                       'APP_DUCKDB': str(tmp_path / 'app.duckdb'), 'RUNS_DIR': str(tmp_path / 'runs'),
                       'VSS_URL': 'http://vss.invalid', 'VSS_USERNAME': 'fake', 'VSS_PASSWORD': 'fake', 'SITE_MAP': str(site_map)}.items():
        monkeypatch.setenv(key, value)
    from app.main import create_app
    with TestClient(create_app()) as c:
        vss = c.app.state.vss
        assert vss is not None and vss.token is None
        assert vss.map_hit({'source': 's3://vss/seg.mp4', 'original_video': 'moved.mp4', 'segment_start_sec': 0.0}) == (WH, 'ceiling_04', 5000)
        assert vss.map_hit({'source': 's3://vss/moved.mp4', 'original_video': 'cam.mp4', 'segment_start_sec': 5.0}) == (WH, 'ceiling_04', 5000)


def test_scene_routes_and_json_contract(client):
    h = client.get('/health').json()
    assert h['ok'] and h['assets_ok'] and h['index_rows']['clips'] == 8
    assert [w['id'] for w in client.get('/api/workspaces').json()] == ['warehouse', 'fleet', 'street']
    for kind in ('manifest', 'entities'):
        r = client.get(f'/api/scene/{WH}/{kind}')
        assert r.status_code == 200 and schema_errors(r.json(), kind + '.json') == []
    timeline = client.get(f'/api/scene/{WH}/timeline').json()
    events = timeline.pop('events')
    assert schema_errors(timeline, 'timeline.json') == []
    source = json.loads((SVA / f'fixtures/sva-out/{WH}/events.json').read_text())
    assert events == source['events']
    m = Moment.model_validate(client.get(f'/api/scene/{WH}/moment?t=4600').json())
    p = next(p for p in m.persons if p.entity_id == 'P1')
    assert 'ceiling_00' not in p.observed_in and 'ceiling_00' not in p.seen_by
    records = client.get(f'/api/model-results?scene_key={WH}&t_ms=4600&window_ms=3000').json()
    assert records
    [CosmosRecord.model_validate(r) for r in records]
    assert client.get(f'/api/scene/{WH}/moment?t=10000').status_code == 422
    assert client.get('/api/scene/missing/manifest').status_code == 404


def test_assets_range_and_paths(client):
    r = client.get(f'/api/assets/{WH}/points.bin', headers={'Range': 'bytes=0-31'})
    assert r.status_code == 206 and len(r.content) == 32 and r.content[:4] == b'SVAP'
    assert r.headers['content-range'].startswith('bytes 0-31/')
    assert client.get(f'/api/assets/{WH}/points.bin', headers={'Range': 'bytes=999999999-'}).status_code == 416
    assert client.get('/api/assets/%2e%2e/CONTRACT.md').status_code == 404


def test_fleet_and_actions(client):
    trips = client.get('/api/fleet/trips').json()
    assert len(trips) == 2 and sum(t['full'] for t in trips) == 1
    frame = client.get(f'/api/fleet/trip/{FL}/moment?t=7000').json()
    place, sigma = frame.pop('place'), frame.pop('gps_sigma_m')
    assert place['place_id'] == 'pl_queen_spadina' and sigma == frame['ego']['sigma_m']
    doc = json.loads((SVA / f'fixtures/sva-out/{FL}/ego/00000.json').read_text())
    doc['frames'] = [frame]
    assert schema_errors(doc, 'ego.json') == []
    building = client.get('/api/fleet/building/w1001').json()
    building.pop('bid'); building.pop('facade_note')
    doc = json.loads((SVA / f'fixtures/sva-out/{FL}/buildings.json').read_text())
    doc['buildings'] = {'w1001': building}
    assert schema_errors(doc, 'buildings.json') == []
    request = {'scene_key': WH, 'event_id': 'E1', 'auto': False, 'actions': ['create_review_task', 'hazard_zone']}
    TriggerResult.model_validate(client.post('/api/actions/trigger', json=request).json())
    result = client.get(f'/api/actions?scene_key={WH}').json()
    [Action.model_validate(a) for a in result['actions']]
    [Zone.model_validate(z) for z in result['zones']]


def test_zone_projection(client):
    polygon = [[-7, -4], [7, -4], [7, 4], [-7, 4]]
    world = client.post(f'/api/scene/{WH}/zone', json={'polygon_world': polygon}).json()
    import numpy as np
    m = client.get(f'/api/scene/{WH}/manifest').json()['cams'][0]
    w2c = np.linalg.inv(np.array(m['cam2world']).reshape(4, 4, order='F'))
    px = []
    fx, fy, cx, cy = m['K']
    for x, y in polygon:
        p = w2c @ [x, y, 0, 1]
        px.append([cx + fx * p[0] / -p[2], cy + fy * p[1] / p[2]])
    projected = client.post(f'/api/scene/{WH}/zone', json={'cam': m['id'], 'polygon_px': px}).json()
    assert np.allclose(projected['polygon_world'], polygon)
    assert world['cells'] == projected['cells'] and world['events_in_zone']


def test_site_asset_redirect(client, monkeypatch):
    from dataclasses import replace
    store = client.app.state.store
    client.app.state.settings = replace(client.app.state.settings, mode='site', data_backend='vastdb', asset_dir=None)
    store.asset_dir = None
    class S3:
        def generate_presigned_url(self, method, Params):
            assert method == 'get_object' and Params['Key'] == f'sva/{WH}/points.bin'
            return 'https://example.test/signed'
    monkeypatch.setattr(store, 's3', lambda: S3())
    r = client.get(f'/api/assets/{WH}/points.bin', follow_redirects=False)
    assert r.status_code == 302 and r.headers['location'] == 'https://example.test/signed'


def test_site_mode_serves_local_asset_dir(index_db, tmp_path, monkeypatch):
    site_map = tmp_path / 'site_map.json'
    site_map.write_text(json.dumps({'map': {}}))
    for key, value in {'MODE': 'site', 'DATA_BACKEND': 'duckdb', 'ALLOW_DUCKDB_FALLBACK': '1', 'INDEX_DUCKDB': str(index_db),
                       'APP_DUCKDB': str(tmp_path / 'app.duckdb'), 'RUNS_DIR': str(tmp_path / 'runs'),
                       'SITE_MAP': str(site_map), 'ASSET_DIR': str(SVA / 'fixtures/sva-out'), 'S3_VERIFY': '0'}.items():
        monkeypatch.setenv(key, value)
    import sys, types
    from app.main import create_app
    with TestClient(create_app()) as c:
        store = c.app.state.store
        clients = []
        monkeypatch.setitem(sys.modules, 'boto3', types.SimpleNamespace(client=lambda *a, **k: clients.append(k)))
        store.s3()
        assert clients[0]['verify'] is False
        monkeypatch.setattr(store, 's3', lambda: pytest.fail('local asset dir went to S3'))
        assert c.app.state.settings.mode == 'site' and store.asset_url(WH, 'points.bin') == f'/api/assets/{WH}/points.bin'
        size = (SVA / f'fixtures/sva-out/{WH}/points.bin').stat().st_size
        r = c.get(f'/api/assets/{WH}/points.bin', follow_redirects=False)
        assert r.status_code == 200 and len(r.content) == size
        r = c.get(f'/api/assets/{WH}/points.bin', headers={'Range': 'bytes=0-31'}, follow_redirects=False)
        assert r.status_code == 206 and len(r.content) == 32


def street_app(tmp_path, monkeypatch, *, site):
    from app.tests.unit.a5_fakes import l1_assets
    index, assets = l1_assets(tmp_path, site=site)
    for key, value in {'MODE': 'dev', 'DATA_BACKEND': 'duckdb', 'INDEX_DUCKDB': str(index), 'APP_DUCKDB': str(tmp_path / 'app.duckdb'),
                       'RUNS_DIR': str(tmp_path / 'runs'), 'ASSET_DIR': str(assets)}.items():
        monkeypatch.setenv(key, value)
    from app.main import create_app
    return TestClient(create_app())


def test_workspaces_have_tags_levels_and_caps_including_street(tmp_path, monkeypatch):
    from app.tests.unit.a5_fakes import L1_KEY
    with street_app(tmp_path, monkeypatch, site=False) as c:
        groups = {w['id']: w['scenes'] for w in c.get('/api/workspaces').json()}
        assert len(groups['street']) == 1
        assert groups['street'][0]['tag'] == 'fixed'
        assert groups['street'][0]['caps']['distance']['reason'] == 'no_geometry'
        levels = {s['scene_key']: s['evidence_level'] for g in groups.values() for s in g}
        assert levels == {WH: 'L3', FL: 'L2', 'fl-pie-fixture-02': 'L2', L1_KEY: 'L1'}
        for scene in (s for g in groups.values() for s in g):
            assert {'distance', 'ground_position', 'map', 'blind_spot', 'tracks', 'speed', 'boxes_2d', 'caption', 'cosmos'} == set(scene['caps'])


def test_street_routes_on_the_l1_fixture(tmp_path, monkeypatch):
    from app.tests.unit.a5_fakes import L1_CAM, L1_KEY
    with street_app(tmp_path, monkeypatch, site=False) as c:
        segments = c.get(f'/api/street/{L1_KEY}/segments').json()
        assert [(s['t0_ms'], s['t1_ms'], s['counts']) for s in segments] == [(0, 5000, {'person': 1}), (5000, 10000, {'car': 2, 'person': 1})]
        assert set(segments[0]) == {'clip_id', 'cam_id', 't0_ms', 't1_ms', 'caption', 'counts', 'source'} and segments[1]['caption'] is None
        d = c.get(f'/api/street/{L1_KEY}/detections?cam={L1_CAM}&t0=4900&t1=5100').json()
        assert d == {'cam_id': L1_CAM, 'source': 'yolo11_coco', 'gaps': [],
                     'rows': [[4900, 'person', 138, 100, 178, 220, 0.9], [5000, 'person', 140, 100, 180, 220, 0.9],
                              [5000, 'car', 300, 200, 400, 260, 0.8], [5000, 'car', 420, 200, 520, 260, 0.7]]}
        chunks = c.get(f'/api/street/{L1_KEY}/chunks?cam={L1_CAM}').json()
        assert chunks == [{'index': 0, 't0_ms': 0, 't1_ms': 10000, 'url': f'/api/street/{L1_KEY}/chunk/{L1_CAM}/0'}]
        data = (tmp_path / 'sva-out' / L1_KEY / f'chunks/{L1_CAM}/0000.mp4').read_bytes()
        r = c.get(chunks[0]['url'])
        assert r.status_code == 200 and r.content == data
        r = c.get(chunks[0]['url'], headers={'Range': 'bytes=100-299', 'Accept-Encoding': 'gzip'})
        assert r.status_code == 206 and r.content == data[100:300] and r.headers['content-range'] == f'bytes 100-299/{len(data)}'
        r = c.get(f'/api/street/{L1_KEY}/thumb?cam={L1_CAM}&t=2500')
        assert r.status_code == 200 and r.headers['content-type'] == 'image/jpeg' and r.content[:2] == b'\xff\xd8'
        assert c.get(f'/api/street/{L1_KEY}/chunk/{L1_CAM}/1').status_code == 404
        assert c.get(f'/api/street/{L1_KEY}/chunks?cam=missing').status_code == 404
        assert c.get(f'/api/street/{WH}/segments').status_code == 404


def test_street_chunk_proxies_s3_with_range(tmp_path, monkeypatch):
    import types
    from app.tests.unit.a5_fakes import L1_CAM, L1_KEY
    data = bytes(range(256)) * 40
    seen = []
    def get_object(Bucket, Key, Range=None):
        if Key.startswith('detections/'):
            from botocore.exceptions import ClientError
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        seen.append((Bucket, Key, Range))
        lo, hi = (int(x) for x in Range[6:].split('-')) if Range else (0, len(data) - 1)
        part = data[lo:hi + 1]
        return {'Body': types.SimpleNamespace(iter_chunks=lambda size: [part[i:i + size] for i in range(0, len(part), size)]),
                'ContentLength': len(part), **({'ContentRange': f'bytes {lo}-{hi}/{len(data)}'} if Range else {})}
    with street_app(tmp_path, monkeypatch, site=True) as c:
        monkeypatch.setattr(c.app.state.store, 's3', lambda: types.SimpleNamespace(get_object=get_object))
        r = c.get(f'/api/street/{L1_KEY}/chunk/{L1_CAM}/0', headers={'Range': 'bytes=1000-1999', 'Accept-Encoding': 'gzip'})
        assert r.status_code == 206 and r.content == data[1000:2000]
        assert r.headers['content-range'] == f'bytes 1000-1999/{len(data)}' and 'content-encoding' not in r.headers
        assert seen == [('team-38-vss-chunks', 'team-38/20261009_000000_clip01_chunk_0000.mp4', 'bytes=1000-1999')]
        r = c.get(f'/api/street/{L1_KEY}/chunk/{L1_CAM}/0')
        assert r.status_code == 200 and r.content == data
        d = c.get(f'/api/street/{L1_KEY}/detections?cam={L1_CAM}&t0=0&t1=10000').json()
        assert d['rows'] == [] and d['gaps'] == [[0, 10000, 'sidecar_unavailable']]


def test_static_files_are_revalidated(client):
    for path in ['/app.js', '/warehouse/page.js']:
        r = client.get(path)
        assert r.status_code == 200 and r.headers['cache-control'] == 'no-cache'
        assert client.get(path, headers={'if-none-match': r.headers['etag']}).status_code == 304


def test_index_versions_entry_assets_by_static_hash(client, tmp_path, monkeypatch):
    import hashlib
    import shutil
    import app.main as main

    def expected(root):
        h = hashlib.sha256()
        for p in sorted(p for p in root.rglob('*') if p.suffix in ('.js', '.css', '.html') and p.relative_to(root).parts[0] != 'lib'):
            h.update(str(p.relative_to(root)).encode() + b'\0' + p.read_bytes())
        return h.hexdigest()[:12]

    def served(c):
        r = c.get('/')
        assert r.status_code == 200 and r.headers['cache-control'] == 'no-cache'
        assert c.get('/index.html').text == r.text
        return r.text

    v = expected(main.STATIC_DIR)
    html = served(client)
    assert f'href="styles.css?v={v}"' in html and f'src="app.js?v={v}"' in html
    r = client.get(f'/app.js?v={v}')
    assert r.status_code == 200 and r.content == (main.STATIC_DIR / 'app.js').read_bytes()

    static = tmp_path / 'static'
    shutil.copytree(main.STATIC_DIR, static, ignore=shutil.ignore_patterns('lib'))
    monkeypatch.setattr(main, 'STATIC_DIR', static)
    (static / 'kernel/extra.js').write_text('export {}\n')
    with TestClient(main.create_app()) as c:
        changed = expected(static)
        assert changed != v and f'src="app.js?v={changed}"' in served(c)


def test_json_is_gzipped_and_binary_ranges_are_not(client):
    gz = {'Accept-Encoding': 'gzip'}
    r = client.get(f'/api/assets/{FL}/trip.geojson', headers=gz)
    assert r.status_code == 200 and r.headers['content-encoding'] == 'gzip'
    assert client.get(f'/api/scene/{WH}/manifest', headers=gz).headers['content-encoding'] == 'gzip'
    # pmtiles reads its header with a 16 KiB range and needs the raw bytes and their length, also when the range covers the file
    for rng in ['bytes=0-16383', 'bytes=0-999999']:
        r = client.get('/api/assets/_map/buildings.pmtiles', headers={**gz, 'Range': rng})
        assert 'content-encoding' not in r.headers and int(r.headers['content-length']) == len(r.content)
        assert r.content[:7] == b'PMTiles'
