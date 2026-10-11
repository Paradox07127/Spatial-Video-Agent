import copy
import gc
import json
import time
import weakref

import httpx
import pytest

from app.core import embed, live_measure
from app.core.deadline import remaining, scope
from app.tests.unit.test_live_measure import FakeStore, MANIFEST, ROW, SIDECAR, SOURCE


def embed_client(seen, vh):
    def respond(req):
        seen.append((req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={'embeddings': [[0.1] * 256]} if vh else {'data': [{'embedding': [0.1] * 256}]})
    return httpx.Client(transport=httpx.MockTransport(respond))


def test_embed_honours_the_cosmos_aliases(monkeypatch):
    for k in ('EMBED_URL', 'EMBED_MODEL', 'EMBED_PROTOCOL'):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv('COSMOS_EMBED1_URL', 'http://embed:8003')
    monkeypatch.setenv('COSMOS_EMBED1_MODEL', 'custom-embed')
    seen = []
    assert len(embed.embed_text('worker', client=embed_client(seen, False))) == 256
    assert seen[0][0] == '/v1/embeddings' and seen[0][1]['model'] == 'custom-embed'


@pytest.mark.parametrize('port,protocol,path', [(8103, 'openai', '/v1/embeddings'), (8003, 'vh', '/embed/text')])
def test_embed_protocol_setting_overrides_the_port(monkeypatch, port, protocol, path):
    monkeypatch.setenv('EMBED_URL', f'http://embed:{port}')
    monkeypatch.setenv('EMBED_PROTOCOL', protocol)
    seen = []
    assert len(embed.embed_text('worker', client=embed_client(seen, protocol == 'vh'))) == 256
    assert seen[0][0] == path


def test_explicit_deadline_cannot_extend_the_inherited_scope():
    with scope(time.monotonic() + 0.1):
        assert remaining(5, time.monotonic() + 100) <= 0.1


def scaled(sidecar, k):
    out = copy.deepcopy(sidecar)
    out['video_shape'] = [round(sidecar['video_shape'][0] * k), round(sidecar['video_shape'][1] * k), *sidecar['video_shape'][2:]]
    for f in out['frames']:
        for d in f['detections']:
            d['bbox'] = [v * k for v in d['bbox']]
    return out


def two_segment_store(later_sidecar):
    later = {**ROW, 'source': SOURCE.replace('chunk_0004', 'chunk_0005'), 't_ms': 20100}
    first = copy.deepcopy(SIDECAR)
    for f in first['frames']:
        f['detections'] = [d for d in f['detections'] if d['label'] != 'truck']
    docs = {live_measure.sidecar_uri(ROW['source']).split('/', 3)[3]: first,
            live_measure.sidecar_uri(later['source']).split('/', 3)[3]: later_sidecar}
    return FakeStore(MANIFEST, docs), [ROW, later]


def test_each_segment_is_projected_with_its_own_video_shape():
    store, rows = two_segment_store(SIDECAR)
    full = live_measure.measure(store, 'wh-test', 'cam_a', 20000, 21000, rows=rows)
    store, rows = two_segment_store(scaled(SIDECAR, 0.5))
    half = live_measure.measure(store, 'wh-test', 'cam_a', 20000, 21000, rows=rows)
    assert full['value_m'] is not None and half['value_m'] == pytest.approx(full['value_m'], abs=1e-3)


@pytest.mark.parametrize('bad', [{'fps': 0}, {'fps': float('nan')}, {'video_shape': [1080]}, {'video_shape': [0, 1920]}])
def test_invalid_sidecar_is_rejected(bad):
    store, rows = two_segment_store({**SIDECAR, **bad})
    with pytest.raises(ValueError):
        live_measure.measure(store, 'wh-test', 'cam_a', 20000, 21000, rows=rows)


def test_s3_client_lives_on_the_store():
    calls = []
    class Counting(FakeStore):
        def s3(self):
            calls.append(1)
            return self.client
    store = Counting(MANIFEST, {live_measure.sidecar_uri(SOURCE).split('/', 3)[3]: SIDECAR})
    for _ in range(2):
        live_measure.measure(store, 'wh-test', 'cam_a', 20000, 20200, rows=[ROW])
    assert len(calls) == 1
    ref = weakref.ref(store)
    del store
    gc.collect()
    assert ref() is None
