"""Query-time ground distance from the YOLO boxes VSS stored for the live segments of one camera.

Sidecar boxes -> scene t_ms -> ByteTrack -> box bottom-centres on the ground (manifest camera, geometry.ground_point)
-> closest approach of a person to the other member of the pair.
"""
from __future__ import annotations

import functools
import gzip
import json
import math
import time

import cv2
import numpy as np
import supervision as sv

from app.core.geometry import ground_point
from app.core.deadline import remaining

THREADS = 4
cv2.setNumThreads(THREADS)
SEGMENT_MS = 5000  # every live segment: detection_frame_count 150 at 30 fps
# COCO has no forklift class; pipeline/warehouse/pose2d.py keeps car / bus / truck boxes for the forklift
VEHICLE_LABELS = {'car', 'bus', 'truck'}
BOX_BOTTOM_SIGMA_M = 0.23  # pipeline/warehouse/triangulate.py ROOT_SIGMA['boxbottom']
# pipeline/fleet/ego_scene.py: flat-ground range errors and the range beyond which they exceed half the range
SIGMA_PITCH_RAD = math.radians(1.0)
SIGMA_H_M = 0.05
MAX_RANGE_M = 40.0


def sidecar_uri(source: str) -> str:
    return source.replace('/segments/', '/detections/', 1).rsplit('.mp4', 1)[0] + '.json.gz'


def covering_rows(rows, scene_key: str, cam_id: str, t0_ms: int, t1_ms: int) -> list[dict]:
    """One site-map row per segment start that overlaps [t0_ms, t1_ms), in time order."""
    by_t = {}
    for r in sorted(rows, key=lambda r: r['source']):
        if r['scene_key'] == scene_key and r['cam_id'] == cam_id and r['t_ms'] < t1_ms and r['t_ms'] + SEGMENT_MS > t0_ms:
            by_t.setdefault(r['t_ms'], r)
    return [by_t[t] for t in sorted(by_t)]


def fetch_sidecar(s3, uri: str) -> dict:
    bucket, key = uri.removeprefix('s3://').split('/', 1)
    return json.loads(gzip.decompress(s3.get_object(Bucket=bucket, Key=key)['Body'].read()))


def window_frames(row: dict, sidecar: dict, t0_ms: int, t1_ms: int) -> list[tuple[int, dict]]:
    """(scene t_ms, frame) of the sidecar frames in [t0_ms, t1_ms); frame 0 is at the row's t_ms."""
    out = [(row['t_ms'] + round(f['frame_index'] * 1000 / sidecar['fps']), f) for f in sidecar['frames']]
    return [(t, f) for t, f in out if t0_ms <= t < t1_ms]


def merged_frames(segs: list[dict], sidecars: list[dict], t0_ms: int, t1_ms: int) -> list[tuple[int, dict]]:
    """Window frames of segments in time order; where segments overlap, the earlier one keeps its frames."""
    out = []
    for r, s in zip(segs, sidecars):
        out += [(t, f) for t, f in window_frames(r, s, t0_ms, t1_ms) if not out or t > out[-1][0]]
    return out


def track(frames: list[tuple[int, dict]], fps: float, labels: set[str]) -> list[tuple]:
    """ByteTrack over the boxes whose label is in labels -> rows (t_ms, track_id, label, x1, y1, x2, y2)."""
    names = sorted(labels)
    tracker, out = sv.ByteTrack(frame_rate=round(fps)), []
    for t, f in frames:
        remaining(30)
        ds = [d for d in f['detections'] if d['label'] in labels]
        dets = sv.Detections(xyxy=np.array([d['bbox'] for d in ds], dtype=np.float32).reshape(-1, 4),
                             confidence=np.array([d['confidence'] for d in ds], dtype=np.float32),
                             class_id=np.array([names.index(d['label']) for d in ds], dtype=int))
        o = tracker.update_with_detections(dets)
        out += [(t, int(o.tracker_id[i]), names[o.class_id[i]], *map(float, o.xyxy[i])) for i in range(len(o))]
    return out


def ground(cam: dict, uv: np.ndarray, video_wh: tuple[int, int]) -> np.ndarray:
    """Video pixels (n, 2) -> ground xy (n, 2) in the manifest frame (fleet: ego, x forward, y left); NaN off the ground."""
    sx, sy = cam['w'] / video_wh[0], cam['h'] / video_wh[1]
    return np.array([ground_point(cam, u * sx, v * sy) for u, v in uv], float).reshape(-1, 2)


def fleet_sigma_m(d: float, h: float) -> float:
    """1-sigma flat-ground range error (m) at range d for a camera h m above the ground (ego_scene.sigma_m)."""
    a = math.atan2(h, d)
    return math.hypot(d / h * SIGMA_H_M, h / math.sin(a) ** 2 * SIGMA_PITCH_RAD)


@functools.cache
def _s3(store):
    return store.s3()


def _closest_fleet(people, p_xy, cam):
    d = np.hypot(p_xy[:, 0], p_xy[:, 1])
    ok = np.flatnonzero(np.isfinite(d) & (p_xy[:, 0] > 0) & (d <= MAX_RANGE_M))
    if not len(ok):
        return None
    i = int(ok[np.argmin(d[ok])])
    h = float(np.asarray(cam['cam2world'], float)[14])
    return float(d[i]), fleet_sigma_m(float(d[i]), h), people[i][0], [people[i][1]]


def _closest_pair(people, p_xy, others, o_xy):
    o_at, best = {}, None
    for j, r in enumerate(others):
        if np.isfinite(o_xy[j]).all():
            o_at.setdefault(r[0], []).append(j)
    for i, r in enumerate(people):
        js = o_at.get(r[0])
        if js and np.isfinite(p_xy[i]).all():
            d = np.linalg.norm(o_xy[js] - p_xy[i], axis=1)
            k = int(np.argmin(d))
            if best is None or d[k] < best[0]:
                best = (float(d[k]), math.hypot(BOX_BOTTOM_SIGMA_M, BOX_BOTTOM_SIGMA_M), r[0], [r[1], others[js[k]][1]])
    return best


def measure(store, scene_key: str, cam_id: str, t0_ms: int, t1_ms: int, pair=('person', 'forklift'), *, rows) -> dict:
    """Closest ground approach of the pair on one live camera over [t0_ms, t1_ms).

    rows: site-map rows ({source, scene_key, cam_id, t_ms}), e.g. site_map.load(path)[0].values(); the sidecar of each
    covering row is read from S3 through store.s3(), the camera from store.manifest(scene_key). Warehouse: a person box
    bottom-centre to the bottom-centre of a car / bus / truck box (the detector's forklift) in the same frame. Fleet: the
    ego vehicle (pair[1] 'vehicle') to the closest pedestrian ahead within MAX_RANGE_M.
    Returns {value_m, sigma_m, t_ms, track_ids, n_frames, n_boxes, source_uris, timing_ms}; when no frame holds the
    pair, value_m is None and reason says why.
    """
    start = time.perf_counter()
    timing, mark = {}, [start]

    def lap(name):
        now = time.perf_counter()
        timing[name] = round((now - mark[0]) * 1000, 1)
        mark[0] = now

    manifest = store.manifest(scene_key)
    if not manifest.get('metric',False):
        return {'value_m':None,'reason':'scene_not_metric','n_frames':0,'n_boxes':0,'source_uris':[],
                'scene_coverage':{scene_key:{'coverage':0,'reason':'scene_not_metric'}},'timing_ms':timing}
    cam = next(c for c in manifest['cams'] if c['id'] == cam_id)
    fleet = manifest['workspace'] == 'fleet'
    segs = covering_rows(rows, scene_key, cam_id, t0_ms, t1_ms)
    uris = [sidecar_uri(r['source']) for r in segs]
    lap('rows')
    sidecars = [fetch_sidecar(_s3(store), u) for u in uris]
    lap('sidecar')
    frames = merged_frames(segs, sidecars, t0_ms, t1_ms)
    other = set() if fleet else VEHICLE_LABELS
    result = {'value_m': None, 'n_frames': len(frames), 'source_uris': uris, 'timing_ms': timing,
              'n_boxes': sum(d['label'] in other | {pair[0]} for _, f in frames for d in f['detections'])}
    best = None
    if not segs:
        result['reason'] = f'no live segment of {scene_key}/{cam_id} covers [{t0_ms}, {t1_ms})'
    else:
        fps, (h, w) = sidecars[0]['fps'], sidecars[0]['video_shape'][:2]
        people, others = track(frames, fps, {pair[0]}), track(frames, fps, other)
        lap('track')
        xy = ground(cam, np.array([[(r[3] + r[5]) / 2, r[6]] for r in people + others], float).reshape(-1, 2), (w, h))
        lap('project')
        p_xy, o_xy = xy[:len(people)], xy[len(people):]
        best = _closest_fleet(people, p_xy, cam) if fleet else _closest_pair(people, p_xy, others, o_xy)
        lap('distance')
        if best is None:
            result['reason'] = f'no frame of {scene_key}/{cam_id} in [{t0_ms}, {t1_ms}) has both {pair[0]} and {pair[1]} on the ground'
    if best is not None:
        result.update(value_m=round(best[0], 3), sigma_m=round(best[1], 3), t_ms=best[2], track_ids=best[3])
    timing['total'] = round((time.perf_counter() - start) * 1000, 1)
    return result
