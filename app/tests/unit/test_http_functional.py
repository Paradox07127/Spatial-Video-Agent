"""HTTP boundary regressions using local files and fake S3 only."""
import asyncio
from threading import RLock

import duckdb
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import health, scenes


class Body:
    def __init__(self, data):
        self.data = data
        self.closed = False

    def iter_chunks(self, size):
        yield self.data

    def close(self):
        self.closed = True


class S3:
    data = bytes(range(64))

    def __init__(self, error=None):
        self.error = error
        self.gets = []
        self.heads = 0
        self.bodies = []

    def head_object(self, **kwargs):
        self.heads += 1
        return {'ContentLength': len(self.data)}

    def get_object(self, **kwargs):
        self.gets.append(kwargs)
        rng = kwargs.get('Range')
        if self.error:
            raise ClientError({'Error': {'Code': self.error, 'Message': 'private upstream details'}}, 'GetObject')
        lo, hi = 0, len(self.data) - 1
        if rng:
            a, b = rng[6:].split('-')
            lo, hi = (max(0, len(self.data) - int(b)), hi) if not a else (int(a), min(int(b) if b else hi, hi))
            if lo >= len(self.data):
                raise ClientError({'Error': {'Code': 'InvalidRange'}}, 'GetObject')
        body = Body(self.data[lo:hi + 1])
        self.bodies.append(body)
        return {'Body': body, 'ContentLength': len(body.data),
                **({'ContentRange': f'bytes {lo}-{hi}/{len(self.data)}'} if rng else {})}


@pytest.fixture
def chunk_client(monkeypatch):
    s3 = S3()
    app = FastAPI()
    app.state.store = SimpleNamespace(s3=lambda: s3)
    app.include_router(scenes.router)
    monkeypatch.setattr(scenes.street, 'chunks', lambda *a: [{'index': 0, 'uri': 's3://team-bucket/chunk.mp4'}])
    return TestClient(app), s3


CHUNK = '/api/street/st-test/chunk/cam/0'


@pytest.mark.parametrize('rng,expected', [(None, bytes(range(64))), ('bytes=0-31', bytes(range(32))),
                                        ('bytes=60-', bytes(range(60, 64))), ('bytes=-4', bytes(range(60, 64))),
                                        ('bytes=60-999', bytes(range(60, 64)))])
def test_video_valid_ranges_close_body_and_need_no_head(chunk_client, rng, expected):
    client, s3 = chunk_client
    r = client.get(CHUNK, headers={'Range': rng} if rng else {})
    assert r.status_code == (206 if rng else 200)
    assert r.content == expected and int(r.headers['content-length']) == len(expected)
    assert r.headers['accept-ranges'] == 'bytes'
    assert len(s3.gets) == 1 and s3.heads == 0
    assert s3.bodies[0].closed


@pytest.mark.parametrize('rng', ['bytes=', 'bytes=abc-def', 'items=0-1', 'bytes=0-1,3-4', 'bytes=--4',
                                 'bytes=' + '9' * 200 + '-'])
def test_video_malformed_and_multiple_ranges_are_400_without_s3(chunk_client, rng):
    client, s3 = chunk_client
    r = client.get(CHUNK, headers={'Range': rng})
    assert r.status_code == 400
    assert not s3.gets and s3.heads == 0


@pytest.mark.parametrize('rng', ['bytes=999-1000', 'bytes=40-20', 'bytes=-0'])
def test_video_unsatisfiable_ranges_have_416_and_total_length(chunk_client, rng):
    client, s3 = chunk_client
    r = client.get(CHUNK, headers={'Range': rng})
    assert r.status_code == 416 and r.headers['content-range'] == 'bytes */64'
    assert r.headers['accept-ranges'] == 'bytes'
    assert s3.heads == 1


@pytest.mark.parametrize('code,status', [('NoSuchKey', 404), ('AccessDenied', 502), ('InternalError', 502)])
def test_video_upstream_errors_do_not_leak_details(chunk_client, code, status):
    client, s3 = chunk_client
    s3.error = code
    r = client.get(CHUNK)
    assert r.status_code == status
    assert 'private upstream' not in r.text and 'team-bucket' not in r.text


@pytest.mark.parametrize('abort', [True, False])
def test_video_closes_upstream_body_when_consumption_stops(chunk_client, abort):
    client, s3 = chunk_client
    request = SimpleNamespace(app=client.app, headers={})
    response = scenes.street_chunk('st-test', 'cam', 0, request)
    body = s3.bodies[0]
    def interrupted(size):
        yield b'first frame'
        raise OSError('upstream connection ended')
    body.iter_chunks = interrupted
    async def consume():
        assert await anext(response.body_iterator) == b'first frame'
        if abort:
            await response.body_iterator.aclose()
        else:
            with pytest.raises(OSError):
                await anext(response.body_iterator)
    asyncio.run(consume())
    assert body.closed


def test_video_range_length_lookup_failure_is_safe_502(chunk_client):
    client, s3 = chunk_client
    def fail(**kwargs):
        raise ClientError({'Error': {'Code': 'AccessDenied', 'Message': 'private details'}}, 'HeadObject')
    s3.head_object = fail
    r = client.get(CHUNK, headers={'Range': 'bytes=999-'})
    assert r.status_code == 502 and 'private details' not in r.text


@pytest.fixture
def health_client(tmp_path):
    root = tmp_path / 'assets'
    scene = root / 'wh-test'
    scene.mkdir(parents=True)
    for rel in ['manifest.json', 'video.mp4', 'entities.json', 'timeline.json', 'events.json']:
        (scene / rel).write_bytes(b'{}')
    lib = tmp_path / 'lib'
    lib.mkdir()
    (lib / 'vendor.js').write_bytes(b'export {};')
    m = {'scene_key': 'wh-test', 'workspace': 'warehouse', 'cams': [{'video': 'video.mp4'}],
         'assets': {'entities': 'entities.json', 'timeline': 'timeline.json', 'events': 'events.json'}}
    app_db = duckdb.connect(':memory:')
    app_db.execute('CREATE TABLE actions(action_id VARCHAR)')
    store = SimpleNamespace(asset_dir=root, backend='duckdb', app=app_db, lock=RLock(),
                            list_scenes=lambda: [m], index_counts=lambda: {'clips': 1})
    def asset_exists(key, rel):
        if store.asset_dir is None:
            pytest.fail('health must not perform S3-backed existence checks')
        return (store.asset_dir / key / rel).is_file()
    store.asset_exists = asset_exists
    store.s3 = lambda: pytest.fail('health must not contact S3')
    app = FastAPI()
    app.state.store = store
    app.state.settings = SimpleNamespace(lib_dir=lib, mode='dev')
    app.state.vendor_manifest = {'files': [{'name': 'vendor.js'}]}
    app.include_router(health.router)
    yield TestClient(app), store, m, root, lib
    app_db.close()


def test_health_retains_fields_when_ready(health_client):
    client, _, _, _, _ = health_client
    r = client.get('/health')
    assert r.status_code == 200
    assert r.json() == {'ok': True, 'mode': 'dev', 'backend': 'duckdb', 'libs_ready': True,
                        'index_rows': {'clips': 1}, 'assets_ok': True}


@pytest.mark.parametrize('missing', ['manifest.json', 'video.mp4', 'entities.json', 'timeline.json', 'events.json'])
def test_health_missing_declared_resource_is_unready(health_client, missing):
    client, _, _, root, _ = health_client
    (root / 'wh-test' / missing).unlink()
    r = client.get('/health')
    assert r.status_code == 503 and r.json()['ok'] is False and r.json()['assets_ok'] is False


def test_health_empty_video_is_unready(health_client):
    client, _, _, root, _ = health_client
    (root / 'wh-test/video.mp4').write_bytes(b'')
    assert client.get('/health').status_code == 503


def test_health_missing_library_is_unready(health_client):
    client, _, _, _, lib = health_client
    (lib / 'vendor.js').unlink()
    r = client.get('/health')
    assert r.status_code == 503 and r.json()['libs_ready'] is False and r.json()['assets_ok'] is True


def test_health_missing_index_is_safe_unready(health_client):
    client, store, _, _, _ = health_client
    def failed_index():
        raise RuntimeError('private database details')
    store.list_scenes = failed_index
    r = client.get('/health')
    assert r.status_code == 503 and r.json()['ok'] is False and r.json()['index_rows'] == {}
    assert 'private database' not in r.text


def test_health_missing_required_directory_is_unready(health_client):
    client, _, m, root, _ = health_client
    m['assets']['ego_dir'] = 'ego'
    assert client.get('/health').status_code == 503
    (root / 'wh-test/ego').mkdir()
    assert client.get('/health').status_code == 200


def test_health_street_on_demand_assets_do_not_fail_readiness(health_client):
    client, _, m, _, _ = health_client
    m.update(workspace='street', assets={'dets_dir': 'dets'}, cams=[{'video': None, 'thumbs': None,
                                                               'chunks': [{'uri': 's3://bucket/video.mp4'}]}])
    assert client.get('/health').status_code == 200


def test_health_local_street_chunk_is_required(health_client):
    client, _, m, _, _ = health_client
    m.update(workspace='street', assets={'dets_dir': 'dets'}, cams=[{'video': None,
                                                               'chunks': [{'uri': 'chunks/0.mp4'}]}])
    assert client.get('/health').status_code == 503


def test_health_remote_mirror_is_not_falsely_verified(health_client):
    client, store, _, _, _ = health_client
    store.asset_dir = None
    r = client.get('/health')
    assert r.status_code == 503 and r.json()['assets_ok'] is False


def test_health_fleet_requires_shared_map_files(health_client):
    client, _, m, root, _ = health_client
    m['workspace'] = 'fleet'
    assert client.get('/health').status_code == 503
    (root / '_map').mkdir()
    for rel in ['basemap.pmtiles', 'buildings.pmtiles', 'style.json']:
        (root / '_map' / rel).write_bytes(b'map')
    assert client.get('/health').status_code == 200
    (root / '_map/buildings.pmtiles').unlink()
    assert client.get('/health').status_code == 503


def test_health_invalid_app_connection_is_unready_while_index_and_assets_remain_ready(health_client):
    client, store, _, _, _ = health_client
    store.app.close()
    response = client.get('/health')
    assert response.status_code == 503
    assert response.json() == {'ok': False, 'mode': 'dev', 'backend': 'duckdb', 'libs_ready': True,
                               'index_rows': {'clips': 1}, 'assets_ok': True}
    assert 'connection' not in response.text.lower()
