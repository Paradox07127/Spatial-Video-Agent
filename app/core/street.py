"""L1 street scenes: one 2D camera, 30 s VSS chunks, 5 s segments with a caption and per-frame YOLO COCO boxes (no track ids).

A chunk uri is a scene-relative asset path in fixtures and `s3://` on site; on site boxes come from the VSS sidecar of each segment.
Street moments never read boxes_dir or entities: street scenes have neither.
"""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path

import cv2
from botocore.exceptions import BotoCoreError, ClientError

from app.core import live_measure
from app.core.models import Clip, Evidence, MomentCard, SearchFilters, level_of


def street_manifest(store, scene_key: str) -> dict:
    m = store.manifest(scene_key)
    if m['workspace'] != 'street':
        raise FileNotFoundError('Street scene not found')
    return m


def camera(m: dict, cam: str) -> dict:
    c = next((c for c in m['cams'] if c['id'] == cam), None)
    if c is None:
        raise FileNotFoundError('Camera not found')
    return c


def chunks(store, scene_key: str, cam: str) -> list[dict]:
    """[{index, t0_ms, t1_ms, uri}] in scene time, sorted and contiguous."""
    return [{'index': i, **c} for i, c in enumerate(camera(street_manifest(store, scene_key), cam)['chunks'])]


def chunk_at(store, scene_key: str, cam: str, t_ms: int) -> dict:
    found = next((c for c in chunks(store, scene_key, cam) if c['t0_ms'] <= t_ms < c['t1_ms']), None)
    if found is None:
        raise ValueError('Time outside scene')
    return found


def rrf(clip: Clip) -> float:
    return sum(1 / (60 + r) for r in [clip.rank_text, clip.rank_visual, clip.rank_vss_text, clip.rank_vss_visual] if r is not None)


def max_per_frame(rows: list) -> dict[str, int]:
    """{label: most boxes of that label in one frame}, the meaning of VSS object_counts."""
    counts = {}
    for (_, label), n in Counter((r[0], r[1]) for r in rows).items():
        counts[label] = max(counts.get(label, 0), n)
    return dict(sorted(counts.items()))


def _cache(store, *parts: str) -> Path:
    return Path(store.runs_dir) / 'street' / Path(*parts)


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:32]


def _sidecar(store, source: str) -> dict:
    """The segment's YOLO sidecar read from S3, as {source, video_shape, frame_count, frames [{time_sec, rows [label, x1, y1, x2, y2, conf]}]}."""
    path = _cache(store, 'detections', _digest(source) + '.json')
    if not path.is_file():
        raw = live_measure.fetch_sidecar(store.s3(), live_measure.sidecar_uri(source))
        doc = {'source': raw.get('source', 'yolo11_coco'), 'video_shape': raw['video_shape'][:2],
               'frame_count': raw.get('frame_count', len(raw['frames'])),
               'frames': [{'time_sec': f['frame_index'] / raw['fps'], 'rows': [[d['label'], *d['bbox'], d['confidence']] for d in f['detections']]}
                          for f in raw['frames']]}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_text(json.dumps(doc))
        tmp.replace(path)
    return json.loads(path.read_text())


def detections(store, scene_key: str, cam: str, t0_ms: int, t1_ms: int) -> dict:
    """{cam_id, rows [[t_ms, label, x1, y1, x2, y2, conf]], source, gaps [[t0_ms, t1_ms, reason]], frames_read} in [t0_ms, t1_ms).

    A site sidecar whose video_shape or frame_count does not match the chunk gives a gap instead of boxes."""
    m = street_manifest(store, scene_key)
    c = camera(m, cam)
    rows, sources, gaps, frames = [], [], [], 0
    if any(ch['uri'].startswith('s3://') for ch in c['chunks']):
        segments = store.search_clips(SearchFilters(workspace='street', scene_keys=[scene_key], cam_ids=[cam], t_range=(t0_ms, t1_ms)), None, None)
        for seg in sorted(segments, key=lambda s: s.t0_ms):
            if not seg.vss_source:
                gaps.append([seg.t0_ms, seg.t1_ms, 'no_detections'])
                continue
            try:
                doc = _sidecar(store, seg.vss_source)
            except (BotoCoreError, ClientError):
                gaps.append([seg.t0_ms, seg.t1_ms, 'sidecar_unavailable'])
                continue
            # one frame of slack for the rounding of a segment's end
            expected = round((seg.t1_ms - seg.t0_ms) * m['fps'] / 1000)
            if list(doc['video_shape']) != [c['h'], c['w']] or abs(doc['frame_count'] - expected) > 1:
                gaps.append([seg.t0_ms, seg.t1_ms, 'detections_mismatch'])
                continue
            sources.append(doc['source'])
            for f in doc['frames']:
                t = seg.t0_ms + round(f['time_sec'] * 1000)
                if t0_ms <= t < t1_ms:
                    frames += 1
                    rows.extend([t, *r] for r in f['rows'])
    else:
        for i, ch in enumerate(c['chunks']):
            lo, hi = max(t0_ms, ch['t0_ms']), min(t1_ms, ch['t1_ms'])
            if lo >= hi:
                continue
            try:
                doc = store.asset_json(scene_key, f"{m['assets']['dets_dir']}/{cam}/{i:04d}.json")
            except FileNotFoundError:
                gaps.append([lo, hi, 'no_detections'])
                continue
            sources.append(doc['source'])
            inside = [r for r in doc['rows'] if lo <= r[0] < hi]
            frames += len({r[0] for r in inside})
            rows.extend(inside)
    return {'cam_id': cam, 'rows': rows, 'source': sources[0] if sources else None, 'gaps': gaps, 'frames_read': frames}


def counts_of(store, clip: Clip) -> dict | None:
    """Segment counts: the live VSS object_counts; a fixture counts its dets files; None when nothing was counted."""
    live = (store.live or {}).get('counts', {}).get(clip.clip_id)
    if live is not None or (clip.video_uri or '').startswith('s3://'):
        return live
    return max_per_frame(detections(store, clip.scene_key, clip.cam_id, clip.t0_ms, clip.t1_ms)['rows'])


def segments(store, scene_key: str) -> list[dict]:
    street_manifest(store, scene_key)
    clips = store.search_clips(SearchFilters(workspace='street', scene_keys=[scene_key]), None, None)
    return [{'clip_id': c.clip_id, 'cam_id': c.cam_id, 't0_ms': c.t0_ms, 't1_ms': c.t1_ms, 'caption': c.caption,
             'counts': counts_of(store, c), 'source': c.vss_source} for c in clips]


def local_chunk(store, scene_key: str, chunk: dict) -> Path:
    """The chunk file: the fixture asset, or the S3 object downloaded once into RUNS_DIR."""
    uri = chunk['uri']
    if not uri.startswith('s3://'):
        return store.local_asset(f'{scene_key}/{uri}')
    path = _cache(store, 'chunks', _digest(uri) + '.mp4')
    if not path.is_file():
        bucket, key = uri[5:].split('/', 1)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(store.s3().get_object(Bucket=bucket, Key=key)['Body'].read())
        tmp.replace(path)
    return path


def thumb(store, scene_key: str, cam: str, t_ms: int) -> Path:
    """JPEG of the first frame in second t_ms // 1000: the scene thumbnail when the scene has them, else decoded from the
    chunk and cached under RUNS_DIR."""
    c = camera(street_manifest(store, scene_key), cam)
    s = t_ms // 1000
    if c['thumbs']:
        return store.local_asset(f"{scene_key}/{c['thumbs']}/{s:05d}.jpg")
    path = _cache(store, 'thumbs', scene_key, cam, f'{s:05d}.jpg')
    if not path.is_file():
        chunk = chunk_at(store, scene_key, cam, s * 1000)
        cap = cv2.VideoCapture(str(local_chunk(store, scene_key, chunk)))
        cap.set(cv2.CAP_PROP_POS_MSEC, s * 1000 - chunk['t0_ms'])
        ok, bgr = cap.read()
        cap.release()
        if not ok:
            raise FileNotFoundError('No frame at this time')
        ref = c['frame_ref']
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp.jpg')
        cv2.imwrite(str(tmp), cv2.resize(bgr, (ref['thumb_w'], ref['thumb_h'])))
        tmp.replace(path)
    return path


def moment(store, clip: Clip, *, backend: str, vss=None) -> tuple[MomentCard, list[Evidence]]:
    """One 5 s segment: caption_embed1_rrf (model) + yolo_coco_2d (measure, max boxes per label in one frame)."""
    m = store.manifest(clip.scene_key)
    score = rrf(clip)
    live = {k: v for k, v in [('rank_vss_text', clip.rank_vss_text), ('rank_vss_visual', clip.rank_vss_visual)] if v is not None}
    caption = Evidence.create(scene_key=clip.scene_key, cam_id=clip.cam_id, t0_ms=clip.t0_ms, t1_ms=clip.t1_ms, observation_ids=[],
                              analysis_rev=m['analysis_rev'], kind='model', method='caption_embed1_rrf', params={'clip_id': clip.clip_id},
                              value={'rank_text': clip.rank_text, 'rank_visual': clip.rank_visual, **live, 'rrf': score})
    dets = detections(store, clip.scene_key, clip.cam_id, clip.t0_ms, clip.t1_ms)
    counts = max_per_frame(dets['rows'])
    boxes = Evidence.create(scene_key=clip.scene_key, cam_id=clip.cam_id, t0_ms=clip.t0_ms, t1_ms=clip.t1_ms, observation_ids=[],
                            analysis_rev=m['analysis_rev'], kind='measure', method='yolo_coco_2d', params={'clip_id': clip.clip_id},
                            value={'counts': counts, 'frames_read': dets['frames_read'], 'source': dets['source']})
    seen = ', '.join(f'{label} {n}' for label, n in counts.items())
    card = MomentCard(moment_id=f'm_{clip.scene_key}_{clip.t0_ms}', scene_key=clip.scene_key, t0_ms=clip.t0_ms, t1_ms=clip.t1_ms,
                      cams=[clip.cam_id], primary_cam=clip.cam_id, actor_refs=[], evidence_ids=[caption.eid, boxes.eid], score=score,
                      why=[f'caption rank {clip.rank_text}; visual rank {clip.rank_visual}',
                           f'2D detections in one frame: {seen}' if seen else 'no 2D detections read'],
                      thumb=f'/api/street/{clip.scene_key}/thumb?cam={clip.cam_id}&t={clip.t0_ms}', exec_state='live', backend=backend,
                      clip_id=clip.clip_id, workspace='street', evidence_level=level_of(m))
    return card, [caption, boxes]
