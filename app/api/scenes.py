import re

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, model_validator
import cv2
import numpy as np
from shapely.geometry import Point, Polygon
from starlette.concurrency import run_in_threadpool

from app.core import street
from app.core.models import CosmosRecord, Moment, capabilities, level_of, tag_of
from app.core.store import nearest

router = APIRouter()


class ZoneRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)
    cam: str | None = None
    polygon_px: list[tuple[float, float]] | None = None
    polygon_world: list[tuple[float, float]] | None = None
    t_range: tuple[int, int] | None = None

    @model_validator(mode='after')
    def check(self):
        if any(p is not None and len(p) < 3 for p in (self.polygon_px, self.polygon_world)):
            raise ValueError('Polygon needs at least three points')
        if self.polygon_world is not None and not ((shape := Polygon(self.polygon_world)).is_valid and shape.area > 0):
            raise ValueError('Invalid polygon')
        if self.t_range is not None and self.t_range[0] >= self.t_range[1]:
            raise ValueError('t_range must be increasing')
        return self


@router.get('/api/workspaces')
def workspaces(request: Request):
    store = request.app.state.store
    grouped = []
    for workspace, title in [('warehouse', 'Warehouse'), ('fleet', 'Fleet'), ('street', 'Street')]:
        scenes = []
        for m in store.list_scenes(workspace):
            key = m['scene_key']
            thumb = None
            if m['cams']:
                cam = m['cams'][0]
                if cam['thumbs']:
                    path = f"{cam['thumbs']}/00000.jpg"
                    if store.asset_exists(key, path):
                        thumb = store.asset_url(key, path)
                elif cam.get('chunks'):
                    thumb = f"/api/street/{key}/thumb?cam={cam['id']}&t={m['t0_ms']}"
            scenes.append({k: m[k] for k in ('scene_id', 'scene_key', 'title', 't0_ms', 't1_ms')} | {'thumb': thumb, 'n_events': len(store.events_in(key, m['t0_ms'], m['t1_ms'])),
                                                                                                  'tag': tag_of(m), 'evidence_level': level_of(m), 'caps': capabilities(m),
                                                                                                  'n_cams': len(m['cams'])})
        grouped.append({'id': workspace, 'title': title, 'scenes': scenes})
    return grouped


@router.get('/api/street/{scene_key}/segments')
def street_segments(scene_key: str, request: Request):
    return street.segments(request.app.state.store, scene_key)


@router.get('/api/street/{scene_key}/detections')
def street_detections(scene_key: str, request: Request, cam: str, t0: int, t1: int):
    d = street.detections(request.app.state.store, scene_key, cam, t0, t1)
    return {k: d[k] for k in ('cam_id', 'rows', 'source', 'gaps')}


@router.get('/api/street/{scene_key}/chunks')
def street_chunks(scene_key: str, request: Request, cam: str):
    return [{'index': c['index'], 't0_ms': c['t0_ms'], 't1_ms': c['t1_ms'], 'url': f"/api/street/{scene_key}/chunk/{cam}/{c['index']}"}
            for c in street.chunks(request.app.state.store, scene_key, cam)]


@router.get('/api/street/{scene_key}/chunk/{cam}/{index}')
def street_chunk(scene_key: str, cam: str, index: int, request: Request):
    store = request.app.state.store
    found = [c for c in street.chunks(store, scene_key, cam) if c['index'] == index]
    if not found:
        raise FileNotFoundError('Chunk not found')
    uri = found[0]['uri']
    if not uri.startswith('s3://'):
        return FileResponse(store.local_asset(f'{scene_key}/{uri}'), media_type='video/mp4')
    # the team chunk bucket is not reachable from the browser, so the app proxies it and passes Range through
    from botocore.exceptions import BotoCoreError, ClientError
    bucket, key = uri[5:].split('/', 1)
    params = {'Bucket': bucket, 'Key': key}
    range_header = request.headers.get('range')
    impossible = False
    if range_header is not None:
        # This proxy supports one byte range. Reject malformed/multipart ranges before contacting S3.
        match = re.fullmatch(r'bytes=([0-9]*)-([0-9]*)', range_header.strip()) if len(range_header) <= 128 else None
        if match is None or not any(match.groups()):
            raise HTTPException(400, 'Provide a single byte range')
        start, end = match.groups()
        impossible = (not start and int(end) == 0) or (bool(start and end) and int(end) < int(start))
        params['Range'] = range_header.strip()
    s3 = store.s3()

    def upstream_failure(exc):
        if isinstance(exc, ClientError) and exc.response.get('Error', {}).get('Code') in ('404', 'NoSuchKey', 'NotFound'):
            return HTTPException(404, 'Chunk not found')
        return HTTPException(502, 'Video source unavailable')

    def range_failure():
        # Normal playback needs only GET. Ask for length only after a range cannot be satisfied.
        try:
            size = s3.head_object(Bucket=bucket, Key=key)['ContentLength']
        except (BotoCoreError, ClientError) as exc:
            raise upstream_failure(exc) from None
        return HTTPException(416, 'Requested range is not satisfiable',
                             headers={'Accept-Ranges': 'bytes', 'Content-Range': f'bytes */{size}'})

    if impossible:
        raise range_failure()
    try:
        obj = s3.get_object(**params)
    except ClientError as exc:
        if range_header is not None and (exc.response.get('Error', {}).get('Code') in ('InvalidRange', 'RequestedRangeNotSatisfiable')
                                         or exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 416):
            raise range_failure() from None
        raise upstream_failure(exc) from None
    except BotoCoreError as exc:
        raise upstream_failure(exc) from None
    headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(obj['ContentLength'])}
    if obj.get('ContentRange'):
        headers['Content-Range'] = obj['ContentRange']
    async def stream():
        body = obj['Body']
        try:
            chunks = iter(body.iter_chunks(1 << 16))
            while (chunk := await run_in_threadpool(next, chunks, None)) is not None:
                yield chunk
        finally:
            # Close the S3 socket on completion, iterator failure or a disconnected browser.
            if hasattr(body, 'close'):
                body.close()

    return StreamingResponse(stream(), status_code=206 if 'Range' in params else 200,
                             media_type='video/mp4', headers=headers)


@router.get('/api/street/{scene_key}/thumb')
def street_thumb(scene_key: str, request: Request, cam: str, t: int):
    return FileResponse(street.thumb(request.app.state.store, scene_key, cam, t), media_type='image/jpeg')


@router.get('/api/scene/{scene_key}/manifest')
def manifest(scene_key: str, request: Request):
    return request.app.state.store.manifest(scene_key)


@router.get('/api/scene/{scene_key}/timeline')
def timeline(scene_key: str, request: Request):
    store = request.app.state.store
    m = store.manifest(scene_key)
    return {**store.asset_json(scene_key, m['assets']['timeline']), 'events': store.asset_json(scene_key, m['assets']['events'])['events']}


@router.get('/api/scene/{scene_key}/entities')
def entities(scene_key: str, request: Request):
    store = request.app.state.store
    return store.asset_json(scene_key, store.manifest(scene_key)['assets']['entities'])


@router.get('/api/scene/{scene_key}/moment', response_model=Moment)
def moment(scene_key: str, request: Request, t: int):
    return request.app.state.store.moment(scene_key, t)


@router.get('/api/model-results', response_model=list[CosmosRecord])
def model_results(scene_key: str, request: Request, t_ms: int, window_ms: int = 3000):
    store = request.app.state.store
    path = store.manifest(scene_key)['assets'].get('cosmos')
    return [r for r in store.asset_json(scene_key, path)['records'] if abs(r['t_ms'] - t_ms) <= window_ms] if path else []


def actor_points(store, scene_key, t_ms, actor_ids, t_range=None):
    m = store.manifest(scene_key)
    # entity_id -> [x, y]; an actor seen by two sources keeps the first
    points = {}
    tolerance=1000/m['fps']/2+1
    if 'tracks' in m['assets']:
        for track in store.asset_json(scene_key, m['assets']['tracks'])['tracks']:
            if track['entity_id'] in actor_ids and track['t0_ms'] <= t_ms < track['t1_ms']:
                pts = [p for p in track['pts'] if not t_range or t_range[0]<=p[0]<t_range[1]]
                i = nearest([p[0] for p in pts], t_ms)
                if i is not None and abs(pts[i][0]-t_ms)<=tolerance:
                    points.setdefault(track['entity_id'], pts[i][1:3])
    if 'forklift' in m['assets']:
        for f in store.asset_json(scene_key, m['assets']['forklift'])['forklifts']:
            if f['entity_id'] in actor_ids:
                pts = [p for p in f['track'] if not t_range or t_range[0]<=p['t_ms']<t_range[1]]
                i = nearest([r['t_ms'] for r in pts], t_ms)
                if i is not None and abs(pts[i]['t_ms']-t_ms)<=tolerance:
                    p = pts[i]
                    points.setdefault(f['entity_id'], [p['x'], p['y']])
    if 'ego_dir' in m['assets']:
        frames = store.asset_json(scene_key, f"{m['assets']['ego_dir']}/{t_ms // 10000:05d}.json")['frames']
        frames = [f for f in frames if not t_range or t_range[0]<=f['t_ms']<t_range[1]]
        i = nearest([f['t_ms'] for f in frames], t_ms)
        if i is not None and abs(frames[i]['t_ms']-t_ms)<=tolerance:
            for p in frames[i]['peds'] + frames[i]['vehs']:
                if p['entity_id'] in actor_ids:
                    points.setdefault(p['entity_id'], [p['x'], p['y']])
    return points


def event_zone_time(store, scene_key, event, t0, t1, polygon):
    lo, hi = max(t0,event.t0_ms), min(t1,event.t1_ms)
    if lo>=hi:
        return None
    m = store.manifest(scene_key)
    times = set()
    if 'tracks' in m['assets']:
        times.update(p[0] for track in store.asset_json(scene_key,m['assets']['tracks'])['tracks']
                     if track['entity_id'] in event.actor_ids for p in track['pts'] if lo<=p[0]<hi)
    if 'forklift' in m['assets']:
        times.update(p['t_ms'] for f in store.asset_json(scene_key,m['assets']['forklift'])['forklifts']
                     if f['entity_id'] in event.actor_ids for p in f['track'] if lo<=p['t_ms']<hi)
    if 'ego_dir' in m['assets']:
        for chunk in range(lo//10000,(hi-1)//10000+1):
            try:
                frames = store.asset_json(scene_key,f"{m['assets']['ego_dir']}/{chunk:05d}.json")['frames']
            except FileNotFoundError:
                continue
            times.update(f['t_ms'] for f in frames if lo<=f['t_ms']<hi)
    for t in sorted(times):
        if any(polygon.covers(Point(p)) for p in actor_points(store,scene_key,t,event.actor_ids,(lo,hi)).values()):
            return t
    return None


@router.post('/api/scene/{scene_key}/zone')
def zone(scene_key: str, body: ZoneRequest, request: Request):
    store = request.app.state.store
    m = store.manifest(scene_key)
    if (body.polygon_world is None) == (body.polygon_px is None):
        raise ValueError('Provide polygon_world or cam and polygon_px')
    polygon = body.polygon_world
    if body.polygon_px is not None:
        cam = next((c for c in m['cams'] if c['id'] == body.cam), None)
        if cam is None:
            raise ValueError('Unknown camera')
        if 'K' not in cam or 'cam2world' not in cam:
            raise ValueError('Camera has no calibration for ground projection')
        fx, fy, cx, cy = cam['K']
        K = np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1]], dtype=float)
        coords = np.array(body.polygon_px, dtype=float).reshape(-1, 1, 2)
        if cam['model'] == 'fisheye':
            rays = cv2.fisheye.undistortPoints(coords, K, np.asarray(cam['dist'], dtype=float)).reshape(-1, 2)
        else:
            rays = np.array([[(u - cx) / fx, (v - cy) / fy] for u, v in body.polygon_px])
        matrix = np.asarray(cam['cam2world']).reshape(4, 4, order='F')
        origin = matrix[:3, 3]
        polygon = []
        for x, y in rays:
            direction = matrix[:3, :3] @ [x, -y, -1]
            if abs(direction[2]) < 1e-12 or -origin[2] / direction[2] <= 0:
                raise ValueError('Camera ray does not intersect the ground in front')
            p = origin + direction * (-origin[2] / direction[2])
            polygon.append(p[:2].tolist())
    if len(polygon) < 3:
        raise ValueError('Polygon needs at least three points')
    shape = Polygon(polygon)
    if not shape.is_valid or shape.area == 0:
        raise ValueError('Invalid polygon')
    cells = []
    if 'ground' in m:
        g = m['ground']
        for iy in range(g['ny']):
            for ix in range(g['nx']):
                p = Point(g['origin'][0] + (ix + 0.5) * g['cell_m'], g['origin'][1] + (iy + 0.5) * g['cell_m'])
                if shape.covers(p):
                    cells.append([ix, iy])
    t0, t1 = body.t_range or (m['t0_ms'], m['t1_ms'])
    events = []
    for event in store.events_in(scene_key, t0, t1):
        t = event_zone_time(store,scene_key,event,t0,t1,shape)
        if t is not None:
            value = event.model_dump(mode='json')
            value['params'] = {**value['params'],'zone_t_ms':t}
            events.append(value)
    return {'polygon_world': polygon, 'cells': cells, 'events_in_zone': events}
