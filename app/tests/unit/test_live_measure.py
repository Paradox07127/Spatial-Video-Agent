import copy
import gzip
import io
import json
import math
from pathlib import Path

import numpy as np
import pytest

from app.core import live_measure

DATA = Path(__file__).parent / 'data/live_measure'
SIDECAR = json.loads((DATA / 'sidecar.json').read_text())
MANIFEST = {**json.loads((DATA / 'manifest.json').read_text()),'metric':True}
SOURCE = 's3://team-38-vss-chunks-segments/segments/20261001_055249_wh_test_chunk_0004_segment_005_of_006.mp4'
ROW = {'source': SOURCE, 'scene_key': 'wh-test', 'cam_id': 'cam_a', 't_ms': 20000}
# a forward-looking dashcam 1.5 m above the ground, level: the ego_scene.cam2world basis at zero pitch
DASHCAM = {'id': 'dashcam', 'w': 1920, 'h': 1080, 'K': [1000, 1000, 960, 540], 'model': 'pinhole', 'dist': [],
           'camera_basis': 'opengl', 'cam2world': [0, -1, 0, 0, 0, 0, 1, 0, -1, 0, 0, 0, 0, 0, 1.5, 1]}


class FakeS3:
    def __init__(self, docs):
        self.docs, self.keys = docs, []

    def get_object(self, Bucket, Key):
        self.keys.append((Bucket, Key))
        return {'Body': io.BytesIO(gzip.compress(json.dumps(self.docs[Key]).encode()))}


class FakeStore:
    def __init__(self, manifest, docs):
        self.manifests, self.client = {manifest['scene_key']: manifest}, FakeS3(docs)

    def manifest(self, scene_key):
        return self.manifests[scene_key]

    def s3(self):
        return self.client


def store_with(sidecar, manifest=MANIFEST):
    return FakeStore(manifest, {live_measure.sidecar_uri(SOURCE).split('/', 3)[3]: sidecar})


def test_sidecar_uri_follows_the_segment_source():
    assert live_measure.sidecar_uri(SOURCE) == \
        's3://team-38-vss-chunks-segments/detections/20261001_055249_wh_test_chunk_0004_segment_005_of_006.json.gz'


def test_frame_index_maps_to_scene_time_from_the_row_offset():
    frames = live_measure.window_frames(ROW, SIDECAR, 20000, 20200)
    assert [t for t, _ in frames] == [20000, 20033, 20067, 20100, 20133, 20167]
    assert [f['frame_index'] for _, f in frames] == [0, 1, 2, 3, 4, 5]
    assert live_measure.window_frames(ROW, SIDECAR, 20300, 30000)[0][0] == 20300


def test_tracking_keeps_one_id_per_object_across_frames():
    rows = live_measure.track(live_measure.window_frames(ROW, SIDECAR, 0, 10 ** 9), 30.0, {'person'})
    ids = {}
    for t, tid, label, x1, y1, x2, y2 in rows:
        ids.setdefault('moving' if y2 == 440 else 'static', set()).add(tid)
    assert len(rows) == 20
    assert all(len(v) == 1 for v in ids.values()) and ids['moving'] != ids['static']


def test_ground_projection_of_a_known_point_scales_video_pixels_to_source_pixels():
    cam = MANIFEST['cams'][0]
    xy = live_measure.ground(cam, np.array([[1060.0, 440.0], [1260.0, 440.0]]), (1920, 1080))
    assert xy == pytest.approx(np.array([[1.0, 1.0], [3.0, 1.0]]))


def test_measure_closest_person_forklift_approach_in_the_window():
    store = store_with(SIDECAR)
    out = live_measure.measure(store, 'wh-test', 'cam_a', 20000, 20200, pair=('person', 'forklift'), rows=[ROW])
    assert out['value_m'] == pytest.approx(1.5) and out['t_ms'] == 20167
    assert out['sigma_m'] == pytest.approx(math.hypot(live_measure.BOX_BOTTOM_SIGMA_M, live_measure.BOX_BOTTOM_SIGMA_M), abs=0.01)
    assert len(out['track_ids']) == 2 and out['n_frames'] == 6 and out['n_boxes'] == 18
    assert out['source_uris'] == [live_measure.sidecar_uri(SOURCE)]
    assert store.client.keys == [('team-38-vss-chunks-segments', live_measure.sidecar_uri(SOURCE).split('/', 3)[3])]
    assert {'sidecar', 'track', 'project', 'distance', 'total'} <= set(out['timing_ms'])


def test_overlapping_segments_give_each_scene_time_once_in_order():
    later = {**ROW, 'source': SOURCE.replace('chunk_0004', 'chunk_0005'), 't_ms': 20100}
    store = FakeStore(MANIFEST, {live_measure.sidecar_uri(r['source']).split('/', 3)[3]: SIDECAR for r in (ROW, later)})
    out = live_measure.measure(store, 'wh-test', 'cam_a', 20000, 21000, pair=('person', 'forklift'), rows=[later, ROW])
    assert len(out['source_uris']) == 2 and out['n_frames'] == 13 and out['n_boxes'] == 39
    frames = live_measure.merged_frames([ROW, later], [SIDECAR, SIDECAR], 20000, 21000)
    assert [t for t, _ in frames][9:] == [20300, 20333, 20367, 20400]


def test_pair_absent_returns_no_value_with_a_reason():
    sidecar = copy.deepcopy(SIDECAR)
    for f in sidecar['frames']:
        f['detections'] = [d for d in f['detections'] if d['label'] != 'truck']
    out = live_measure.measure(store_with(sidecar), 'wh-test', 'cam_a', 20000, 20200, pair=('person', 'forklift'), rows=[ROW])
    assert out['value_m'] is None and 'forklift' in out['reason']


def test_no_live_segment_in_the_window_returns_no_value():
    out = live_measure.measure(store_with(SIDECAR), 'wh-test', 'cam_a', 40000, 45000, pair=('person', 'forklift'), rows=[ROW])
    assert out['value_m'] is None and out['source_uris'] == [] and 'no live segment' in out['reason']


def test_fleet_measures_the_ego_vehicle_to_the_closest_pedestrian_ahead():
    # feet 300 px below the principal point of a level camera 1.5 m up (fy 1000) lie 1.5 * 1000 / 300 = 5 m ahead
    sidecar = {'video_shape': [1080, 1920], 'fps': 30.0, 'frames': [
        {'frame_index': k, 'detections': [{'label': 'person', 'confidence': 0.9, 'bbox': [940, 600 + 10 * k, 980, 800 + 10 * k]},
                                          {'label': 'person', 'confidence': 0.9, 'bbox': [100, 100, 140, 300]}]}
        for k in range(5)]}
    manifest = {'scene_key': 'fl-test', 'workspace': 'fleet', 'metric':True, 'cams': [DASHCAM]}
    row = {**ROW, 'scene_key': 'fl-test', 'cam_id': 'dashcam', 't_ms': 0}
    out = live_measure.measure(store_with(sidecar, manifest), 'fl-test', 'dashcam', 0, 5000, pair=('person', 'vehicle'), rows=[row])
    assert out['value_m'] == pytest.approx(5.0) and out['t_ms'] == 133
    assert len(out['track_ids']) == 1 and out['sigma_m'] > 0
