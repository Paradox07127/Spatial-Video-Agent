"""L1 street scenes: one 2D camera, 30 s VSS chunks, 5 s segments with a caption and per-frame YOLO COCO boxes (no track ids).

A chunk uri is a scene-relative asset path in fixtures and `s3://` on site; on site boxes come from the VSS sidecar of each segment.
Street moments never read boxes_dir or entities: street scenes have neither.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from statistics import median
from collections import Counter
from pathlib import Path, PurePosixPath
from contextlib import closing

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


def _publish(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=path.suffix, delete=False) as f:
        tmp = Path(f.name)
        try:
            f.write(data)
            f.close()
            os.replace(tmp,path)
        finally:
            tmp.unlink(missing_ok=True)


def availability_gaps(t0, t1, available, missing=()):
    edges = sorted({t0,t1,*(max(t0,min(t1,t)) for a,b in available for t in (a,b)),
                    *(max(t0,min(t1,t)) for a,b,_ in missing for t in (a,b))})
    gaps = []
    for a,b in zip(edges,edges[1:]):
        if a==b or any(lo<=a and b<=hi for lo,hi in available):
            continue
        reason = next((reason for lo,hi,reason in missing if lo<=a<b<=hi),'no_frames')
        if gaps and gaps[-1][1]==a and gaps[-1][2]==reason:
            gaps[-1][1] = b
        else:
            gaps.append([a,b,reason])
    return gaps


def _sidecar(store, source: str) -> dict:
    """The segment's YOLO sidecar read from S3, as {source, video_shape, frame_count, frames [{time_sec, rows [label, x1, y1, x2, y2, conf]}]}."""
    path = _cache(store, 'detections', _digest(source) + '.json')
    if not path.is_file():
        raw = live_measure.fetch_sidecar(store.s3(), live_measure.sidecar_uri(source))
        doc = {'source': raw.get('source', 'yolo11_coco'), 'fps':raw['fps'], 'video_shape': raw['video_shape'][:2],
               'frame_count': raw.get('frame_count', len(raw['frames'])),
               'frames': [{'time_sec': f['frame_index'] / raw['fps'], 'rows': [[d['label'], *d['bbox'], d['confidence']] for d in f['detections']]}
                          for f in raw['frames']]}
        _publish(path,json.dumps(doc).encode())
    return json.loads(path.read_text())


def detections(store, scene_key: str, cam: str, t0_ms: int, t1_ms: int) -> dict:
    """{cam_id, rows [[t_ms, label, x1, y1, x2, y2, conf]], source, gaps [[t0_ms, t1_ms, reason]], frames_read} in [t0_ms, t1_ms).

    A site sidecar whose video_shape or frame_count does not match the chunk gives a gap instead of boxes."""
    m = street_manifest(store, scene_key)
    c = camera(m, cam)
    rows, sources, gaps, stamps, available = [], [], [], set(), []
    if any(ch['uri'].startswith('s3://') for ch in c['chunks']):
        segments = store.search_clips(SearchFilters(workspace='street', scene_keys=[scene_key], cam_ids=[cam], t_range=(t0_ms, t1_ms)), None, None)
        for seg in sorted(segments, key=lambda s: s.t0_ms):
            lo, hi = max(t0_ms,seg.t0_ms), min(t1_ms,seg.t1_ms)
            if lo>=hi:
                continue
            if not seg.vss_source:
                gaps.append([lo, hi, 'no_detections'])
                continue
            try:
                doc = _sidecar(store, seg.vss_source)
            except (BotoCoreError, ClientError):
                gaps.append([lo, hi, 'sidecar_unavailable'])
                continue
            # one frame of slack for the rounding of a segment's end
            expected = round((seg.t1_ms - seg.t0_ms) * m['fps'] / 1000)
            if list(doc['video_shape']) != [c['h'], c['w']] or abs(doc['frame_count'] - expected) > 1:
                gaps.append([lo, hi, 'detections_mismatch'])
                continue
            sources.append(doc['source'])
            for f in doc['frames']:
                t = seg.t0_ms + round(f['time_sec'] * 1000)
                end = seg.t0_ms+round((f['time_sec']+1/doc.get('fps',m['fps']))*1000)
                available.append((max(lo,t),min(hi,end)))
                if lo <= t < hi and t not in stamps:
                    stamps.add(t)
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
            times = sorted({r[0] for r in doc['rows']})
            step = min((b-a for a,b in zip(times,times[1:])),default=round(1000/m['fps']))
            for j,t in enumerate(times):
                end = times[j+1] if j+1<len(times) and times[j+1]-t<=step+1 else t+step
                if t<hi and end>lo:
                    available.append((max(lo,t),min(hi,end)))
            stamps.update(r[0] for r in inside)
            rows.extend(inside)
    gaps = availability_gaps(t0_ms,t1_ms,[(a,b) for a,b in available if a<b],gaps)
    return {'cam_id': cam, 'rows': rows, 'source': sources[0] if sources else None, 'gaps': gaps, 'frames_read': len(stamps), 'frame_times':sorted(stamps)}


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


def _download_object(store,bucket,key,path):
    try:
        body = store.s3().get_object(Bucket=bucket,Key=key)['Body']
    except ClientError as exc:
        if exc.response['Error']['Code'] in ('404','NoSuchKey','NotFound'):
            raise FileNotFoundError('Media asset not found') from exc
        raise
    with closing(body):
        _publish(path,body.read())


def local_chunk(store, scene_key: str, chunk: dict) -> Path:
    """The chunk file: the fixture asset, or the S3 object downloaded once into RUNS_DIR."""
    uri = chunk['uri']
    if not uri.startswith('s3://'):
        if store.asset_dir is not None:
            return store.local_asset(f'{scene_key}/{uri}')
        if PurePosixPath(uri).is_absolute() or '..' in PurePosixPath(uri).parts:
            raise FileNotFoundError('Chunk not found')
        uri = f's3://{store.settings.asset_bucket}/sva/{scene_key}/{uri}'
    path = _cache(store, 'chunks', _digest(uri+('|' + os.environ['EVIDENCE_REVISION'] if os.environ.get('EVIDENCE_REVISION') else '')) + '.mp4')
    if not path.is_file():
        bucket, key = uri[5:].split('/', 1)
        _download_object(store,bucket,key,path)
    return path


def thumb(store, scene_key: str, cam: str, t_ms: int) -> Path:
    """JPEG of the first frame in second t_ms // 1000: the scene thumbnail when the scene has them, else decoded from the
    chunk and cached under RUNS_DIR."""
    c = camera(street_manifest(store, scene_key), cam)
    s = t_ms // 1000
    if c['thumbs']:
        rel = f"{scene_key}/{c['thumbs']}/{s:05d}.jpg"
        if store.asset_dir is not None:
            return store.local_asset(rel)
        path = _cache(store,'thumbs',_digest(rel+'|'+os.environ.get('EVIDENCE_REVISION',''))+'.jpg')
        if not path.is_file():
            _download_object(store,store.settings.asset_bucket,'sva/'+rel,path)
        return path
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
        ok, data = cv2.imencode('.jpg',cv2.resize(bgr, (ref['thumb_w'], ref['thumb_h'])))
        if not ok:
            raise OSError('Could not encode thumbnail')
        _publish(path,data.tobytes())
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


def people_statistics(store, scene_key: str, cam: str, t0_ms: int, t1_ms: int, mode: str):
    """Measure a full window, never a retrieved top-k subset.

    Count is unique ByteTrack trajectories observed in this camera, not identities or
    line crossings. Peak is the largest simultaneous person-box count, grouped by
    second; ties use the earliest observed frame. Missing frames keep the claim unknown.
    """
    from app.core import tools
    from app.core.deadline import remaining
    remaining(30)
    dets = detections(store,scene_key,cam,t0_ms,t1_ms)
    m = street_manifest(store,scene_key)
    by_time = {t:[] for t in dets.get('frame_times',[])}
    for t,label,x1,y1,x2,y2,conf in dets['rows']:
        remaining(30)
        if label=='person':
            by_time.setdefault(t,[]).append({'label':label,'bbox':[x1,y1,x2,y2],'confidence':conf})
    frames = [(t,{'detections':rows}) for t,rows in sorted(by_time.items())]
    missing = sum(b-a for a,b,_ in dets['gaps'])
    coverage = max(0,(t1_ms-t0_ms-missing)/(t1_ms-t0_ms)) if t1_ms>t0_ms else 0
    value = {'t0_s':t0_ms/1000,'t1_s':t1_ms/1000,'source':dets['source'],
             'frames_read':dets['frames_read'],'coverage':coverage}
    if mode=='tracked_count':
        # Restart after missing input; linking tracks over a gap would assert continuity.
        blocks = []
        for f in frames:
            remaining(30)
            if not blocks or any(blocks[-1][-1][0]<b and a<f[0] for a,b,_ in dets['gaps']):
                blocks.append([])
            blocks[-1].append(f)
        steps = [b[0]-a[0] for block in blocks for a,b in zip(block,block[1:]) if b[0]>a[0]]
        sampled_fps = max(1,1000/median(steps)) if steps else m['fps']
        value['tracker_fps'] = sampled_fps
        value['count'] = sum(len({r[1] for r in live_measure.track(block,sampled_fps,{'person'})}) for block in blocks)
        value['definition'] = 'ByteTrack trajectories observed in the counting window; not verified line crossings or unique identities.'
        method = 'bytetrack_people_count'
    elif mode=='peak_count':
        per_second = {}
        for t,f in frames:
            remaining(30)
            second = t//1000
            n = len(f['detections'])
            if second not in per_second or n>per_second[second]['count']:
                per_second[second] = {'t_s':t/1000,'count':n}
        peak = max(per_second.values(),key=lambda row:(row['count'],-row['t_s']),default={'count':0,'t_s':None})
        value.update(peak)
        value['per_second'] = [{'second':s,**row} for s,row in sorted(per_second.items())]
        value['definition'] = 'Maximum simultaneous person detections per second; earliest peak frame.'
        method = 'yolo_people_peak'
    else:
        raise ValueError('Unknown people statistic')
    ev = tools.evidence(store,scene_key,cam,t0_ms,t1_ms,'measure',method,value,params={'mode':mode})
    return tools.result(scene_key,t0_ms,t1_ms,value,cam=cam,evidence=[ev],pts_read=dets['frames_read'],
                        coverage=coverage,gaps=dets['gaps'])
