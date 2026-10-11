import json
import os
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np
import pytest

from app.api import search
from app.api.search import SearchRequest, retrieve
from app.core import embed, street
from app.tests.unit.a5_fakes import FIXTURES, L1_CAM, L1_KEY, build_l1, fixture_l1_store


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(embed, 'embed_text', lambda q: [1.0] * 256)
    return fixture_l1_store(tmp_path)


def street_guards(monkeypatch, store):
    """box_rows and store.entities raise for a street scene."""
    box_rows, entities = search.box_rows, store.entities
    def guarded(fn):
        def call(s, key, *a):
            if key.startswith('st-'):
                raise AssertionError(f'{fn.__name__} called for street scene {key}')
            return fn(s, key, *a)
        return call
    monkeypatch.setattr(search, 'box_rows', guarded(box_rows))
    monkeypatch.setattr(store, 'entities', lambda key: guarded(lambda s, k: entities(k))(store, key))


def test_rrf_fuses_the_caption_bm25_rank():
    from app.core.models import Clip
    clip = Clip(clip_id='c', scene_id='st/x/y', scene_key='st-x-y', workspace='street', cam_id='cam', t0_ms=0, t1_ms=5000, rank_text=1)
    assert street.rrf(clip.model_copy(update={'rank_bm25': 2})) == pytest.approx(1/61 + 1/62)


def test_street_moments_are_2d_segments_and_never_read_boxes_or_entities(store, monkeypatch):
    street_guards(monkeypatch, store)
    for request in [SearchRequest(q='Find a person', scope='corpus'), SearchRequest(q='Find a person', workspace='street'),
                    SearchRequest(q='Find a person', workspace='street', scene_key=L1_KEY)]:
        _, r = retrieve(store, request, vss=SimpleNamespace())
        cards = [m for m in r.envelope.output['moments'] if m['scene_key'] == L1_KEY]
        assert [(m['t0_ms'], m['t1_ms']) for m in cards] in ([(0, 5000), (5000, 10000)], [(5000, 10000), (0, 5000)])
        evidence = {e.eid: e for e in r.evidence}
        for m in cards:
            assert m['actor_refs'] == [] and m['cams'] == [L1_CAM] and m['clip_id']
            assert (m['workspace'], m['evidence_level']) == ('street', 'L1')
            methods = [evidence[eid].method for eid in m['evidence_ids']]
            assert sorted(methods) == ['caption_embed1_rrf', 'yolo_coco_2d']
            boxes = next(evidence[eid] for eid in m['evidence_ids'] if evidence[eid].method == 'yolo_coco_2d')
            assert boxes.kind == 'measure' and boxes.cam_id == L1_CAM and boxes.observation_ids == []
            assert boxes.value == {'counts': {'person': 1} if m['t0_ms'] == 0 else {'car': 2, 'person': 1},
                                   'frames_read': 50, 'source': 'yolo11_coco'}



def test_street_card_thumb_is_the_decode_route_without_a_thumbs_directory(store, monkeypatch):
    # site street cameras have thumbs=None; the thumb route decodes the frame from the chunk
    manifest = store.manifest
    monkeypatch.setattr(store, 'manifest', lambda k: {**manifest(k), 'cams': [{**c, 'thumbs': None} for c in manifest(k)['cams']]}
                        if k == L1_KEY else manifest(k))
    _, r = retrieve(store, SearchRequest(q='Find a person', workspace='street', scene_key=L1_KEY), vss=SimpleNamespace())
    cards = r.envelope.output['moments']
    assert cards and all(m['thumb'] == f"/api/street/{L1_KEY}/thumb?cam={L1_CAM}&t={m['t0_ms']}" for m in cards)


def test_street_guard_spy_fires(store, monkeypatch):
    street_guards(monkeypatch, store)
    with pytest.raises(AssertionError, match='street scene'):
        search.box_rows(store, L1_KEY, L1_CAM, 0, 5000)
    with pytest.raises(AssertionError, match='street scene'):
        store.entities(L1_KEY)


def test_corpus_returns_every_level_in_order_with_a_quota(store, monkeypatch):
    _, r = retrieve(store, SearchRequest(q='Find a person', scope='corpus'))
    moments = r.envelope.output['moments']
    assert {m['workspace'] for m in moments} == {'warehouse', 'fleet', 'street'}
    order = {'L3': 0, 'L2': 1, 'L1': 2}
    assert [(order[m['evidence_level']], -m['score']) for m in moments] == sorted((order[m['evidence_level']], -m['score']) for m in moments)
    assert {(m['workspace'], m['evidence_level']) for m in moments} == {('warehouse', 'L3'), ('fleet', 'L2'), ('street', 'L1')}
    _, r = retrieve(store, SearchRequest(q='Find a person', scope='corpus', k=1))
    assert [m['evidence_level'] for m in r.envelope.output['moments']] == ['L3', 'L2', 'L1']
    monkeypatch.setattr(search, 'LEVEL_QUOTA', 1)
    _, r = retrieve(store, SearchRequest(q='Find a person', scope='corpus'))
    assert [m['evidence_level'] for m in r.envelope.output['moments']] == ['L3', 'L2', 'L1']
    # corpus ignores the page's workspace and scene
    _, scoped = retrieve(store, SearchRequest(q='Find a person', scope='corpus', workspace='fleet', scene_key='fl-pie-fixture-01', k=1))
    assert scoped.envelope.output['moments'] == r.envelope.output['moments']


def test_scope_absent_equals_scene_scope_on_existing_asks(store):
    for ask in json.loads((FIXTURES / 'asks.json').read_text())['asks']:
        outputs = []
        for extra in [{}, {'scope': 'scene'}]:
            parsed, r = retrieve(store, SearchRequest(q=ask['q'], **ask['context'], **extra))
            outputs.append((parsed.model_dump(), r.envelope.output, [e.model_dump() for e in r.evidence], r.envelope.input_range))
            assert all('workspace' not in m and 'evidence_level' not in m and m['scene_key'] != L1_KEY for m in r.envelope.output['moments'])
        assert outputs[0] == outputs[1], ask['ask_id']


def test_entity_and_zone_questions_skip_street(store):
    _, r = retrieve(store, SearchRequest(q='Find P1', scope='corpus'))
    assert all(m['scene_key'] != L1_KEY for m in r.envelope.output['moments'])
    parsed, r = retrieve(store, SearchRequest(q='Find anyone riding a bicycle', workspace='street'))
    assert parsed.template == 'find' and r.envelope.output['moments'] == []
    _, r = retrieve(store, SearchRequest(q='Find a car', workspace='street'))
    assert len(r.envelope.output['moments']) == 2


def sidecar(frame_count, shape=(360, 640)):
    """Raw S3 sidecar as VSS writes it (gzip JSON next to the segment)."""
    return {'source': 'yolo11_coco', 'video_shape': [*shape, 3], 'fps': 10.0, 'frame_count': frame_count,
            'frames': [{'frame_index': i, 'detections': [{'label': 'person', 'confidence': 0.9, 'bbox': [10, 20, 30, 40]}]}
                       for i in range(frame_count)]}


def s3_with(docs, gets):
    """Fake S3 client: get_object(Bucket, Key) -> gzip JSON of docs(key), or ClientError when docs(key) is None."""
    import gzip
    from botocore.exceptions import ClientError
    def get_object(Bucket, Key):
        gets.append((Bucket, Key))
        doc = docs(Key)
        if doc is None:
            raise ClientError({'Error': {'Code': 'NoSuchKey'}}, 'GetObject')
        return {'Body': SimpleNamespace(read=lambda: gzip.compress(json.dumps(doc).encode()))}
    return lambda: SimpleNamespace(get_object=get_object)


def test_site_detections_read_the_s3_sidecar_check_it_and_cache_it(tmp_path):
    store = fixture_l1_store(tmp_path, site=True)
    gets = []
    store.s3 = s3_with(lambda key: sidecar(50) if key.endswith('segment_001_of_002.json.gz') else sidecar(150), gets)
    d = street.detections(store, L1_KEY, L1_CAM, 0, 10000)
    assert d['gaps'] == [[5000, 10000, 'detections_mismatch']]
    assert len(d['rows']) == 50 and d['rows'][1] == [100, 'person', 10, 20, 30, 40, 0.9] and d['frames_read'] == 50
    assert d['source'] == 'yolo11_coco' and len(gets) == 2
    assert all(b == 'team-38-vss-chunks-segments' and k.startswith('detections/') and k.endswith('.json.gz') for b, k in gets)
    assert street.detections(store, L1_KEY, L1_CAM, 0, 5000)['rows'] == d['rows'] and len(gets) == 2
    (tmp_path / 'other').mkdir()
    other = fixture_l1_store(tmp_path / 'other', site=True)
    other.s3 = s3_with(lambda key: sidecar(50, shape=(1080, 1920)), [])
    assert street.detections(other, L1_KEY, L1_CAM, 0, 5000)['gaps'] == [[0, 5000, 'detections_mismatch']]
    (tmp_path / 'missing').mkdir()
    missing = fixture_l1_store(tmp_path / 'missing', site=True)
    missing.s3 = s3_with(lambda key: None, [])
    assert street.detections(missing, L1_KEY, L1_CAM, 0, 5000)['gaps'] == [[0, 5000, 'sidecar_unavailable']]


def test_site_thumb_is_decoded_from_the_s3_chunk_once(tmp_path):
    store = fixture_l1_store(tmp_path, site=True)
    mp4 = build_l1(tmp_path / 'media') / f'chunks/{L1_CAM}/0000.mp4'
    gets = []
    def get_object(Bucket, Key):
        gets.append((Bucket, Key))
        return {'Body': __import__('io').BytesIO(mp4.read_bytes())}
    store.s3 = lambda: SimpleNamespace(get_object=get_object)
    path = street.thumb(store, L1_KEY, L1_CAM, 3500)
    image = cv2.imread(str(path))
    assert image.shape == (180, 320, 3) and path.is_relative_to(store.runs_dir)
    assert gets == [('team-38-vss-chunks', 'team-38/20261009_000000_clip01_chunk_0000.mp4')]
    assert street.thumb(store, L1_KEY, L1_CAM, 3900) == path and len(gets) == 1
    assert abs(float(np.mean(image)) - float(np.mean(cv2.imread(str(street.thumb(store, L1_KEY, L1_CAM, 0)))))) > 5
    with pytest.raises(ValueError):
        street.thumb(store, L1_KEY, L1_CAM, 10000)


def test_street_caches_are_keyed_by_the_evidence_release(tmp_path, monkeypatch):
    store = fixture_l1_store(tmp_path, site=True)
    gets = []
    store.s3 = s3_with(lambda key: sidecar(50), gets)
    monkeypatch.delenv('EVIDENCE_REVISION', raising=False)
    street._sidecar(store, 's3://b/seg.mp4')
    street._sidecar(store, 's3://b/seg.mp4')
    assert len(gets) == 1
    monkeypatch.setenv('EVIDENCE_REVISION', 'r2')
    street._sidecar(store, 's3://b/seg.mp4')
    assert len(gets) == 2
    monkeypatch.setattr(street, 'local_chunk', lambda *a: Path('video'))
    monkeypatch.setattr(street.cv2, 'VideoCapture', lambda *a: SimpleNamespace(set=lambda *a: None, release=lambda: None,
                                                                               read=lambda: (True, np.zeros((360, 640, 3), np.uint8))))
    first = street.thumb(store, L1_KEY, L1_CAM, 0)
    monkeypatch.setenv('EVIDENCE_REVISION', 'r3')
    assert street.thumb(store, L1_KEY, L1_CAM, 0) != first


def test_street_cache_quota_evicts_least_recently_used(tmp_path, monkeypatch):
    store = SimpleNamespace(runs_dir=tmp_path)
    monkeypatch.setattr(street, 'CACHE_QUOTA', 25)
    paths = [street._cache(store, 'chunks', f'{i}.mp4') for i in range(3)]
    for i, p in enumerate(paths[:2]):
        street._publish(store, p, b'x' * 10)
        os.utime(p, (i, i))
    assert street._cached(paths[0])
    street._publish(store, paths[2], b'x' * 10)
    assert [p.is_file() for p in paths] == [True, False, True]
