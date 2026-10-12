"""Agent tools return evidence with the actual input range and execution state."""
from __future__ import annotations

import bisect
import hashlib
import json
import logging
import math
import time

from shapely.geometry import Point, Polygon

from app.core import geo, geometry, live_measure, street, vlm
from app.core.deadline import DeadlineExceeded, remaining
from app.core.models import Envelope, Evidence, InputRange, ObservationRef, ToolResult, capabilities
from app.settings import Settings

logger = logging.getLogger(__name__)


def result(key, t0, t1, output, *, cam=None, evidence=(), pts_read=0, model_rev=None, gaps=(),
           coverage=None, quality='ok', exec_state='precomputed', error=None, started=None):
    return ToolResult(envelope=Envelope(input_range=InputRange(scene_key=key,cam_id=cam,t0_ms=t0,t1_ms=t1),
                                       pts_read=pts_read,model_rev=model_rev,output=output,coverage=coverage,
                                       gaps=list(gaps),quality=quality,elapsed_ms=round((time.monotonic()-started)*1000) if started else 0,
                                       exec_state=exec_state,error=error),evidence=list(evidence))


def evidence(store, key, cam, t0, t1, kind, method, value, *, observations=(), unit=None, sigma=None, params=None):
    return Evidence.create(scene_key=key,cam_id=cam,t0_ms=t0,t1_ms=t1,observation_ids=list(observations),
                           analysis_rev=store.manifest(key)['analysis_rev'],kind=kind,method=method,
                           value=value,unit=unit,sigma=sigma,params=params)


def search(store, q, filters, *, parsed=None, vss=None, deadline=None):
    from app.api.search import SearchRequest, retrieve
    from app.core.deadline import scope
    with scope(deadline):
        return retrieve(store,SearchRequest(q=q,**filters),parsed=parsed,vss=vss)[1]


def thumbnail(store, scene_key, cam, t_ms):
    c = next((c for c in store.manifest(scene_key)['cams'] if c['id']==cam),None)
    path = f"{c['thumbs']}/{t_ms//1000:05d}.jpg" if c and c.get('thumbs') else None
    return store.asset_url(scene_key,path) if path and store.asset_exists(scene_key,path) else None


def sync_trusted(manifest, camera):
    residual=camera.get('sync_residual_ms')
    return (camera.get('sync_method') not in {None,'unknown'} and
            isinstance(residual,(int,float)) and math.isfinite(residual) and
            0<=residual<=1000/manifest['fps']/2)


def get_synced_views(store, scene_key, t_ms, *, cam=None):
    from app.api.search import box_rows
    started = time.monotonic()
    m = store.manifest(scene_key)
    cams = [c for c in m['cams'] if cam is None or c['id']==cam]
    frames, obs, gaps = [], [], []
    for c in cams:
        if not sync_trusted(m,c):
            gaps.append((t_ms,t_ms+1,'unsynchronized'))
            continue
        rows = box_rows(store,scene_key,c['id'],t_ms,t_ms+1)
        lane = store.asset_json(scene_key,m['assets']['timeline'])['lanes'][c['id']]
        no_video = any(a<=t_ms<b and state=='no_video' for a,b,state in lane)
        if no_video:
            gaps.append((t_ms,t_ms+1,'no_video'))
            continue
        url = thumbnail(store,scene_key,c['id'],t_ms)
        if url is None:
            gaps.append((t_ms,t_ms+1,'missing_thumbnail'))
        frames.append({'id':c['id'],'frame_url':url,
                       'media_pts_ms':max(0,t_ms//1000*1000-c['offset_ms']) if url else None,'offset_ms':c['offset_ms'],
                       'sync_method':c['sync_method'],'sync_residual_ms':c['sync_residual_ms'],'boxes':rows})
        obs.extend(ObservationRef(scene_key=scene_key,cam_id=c['id'],local_track_id=r[1],t_ms=r[0]) for r in rows)
    ev = evidence(store,scene_key,cam,t_ms,t_ms+1,'measure','synced_views',{'cams':frames},observations=obs)
    return result(scene_key,t_ms,t_ms+1,{'cams':frames},cam=cam,evidence=[ev],pts_read=len(frames),
                  gaps=gaps,coverage=sum(c['frame_url'] is not None for c in frames)/len(cams) if cams else 0,started=started)


def no_capability(m, cap, t_ms):
    """A street scene has no geometry, map or GPS: UNRESOLVED with the capability's reason, never a warehouse or fleet reading."""
    reason = capabilities(m)[cap]['reason']
    return result(m['scene_key'],t_ms,t_ms+1,{'status':'UNRESOLVED','reason':reason},gaps=[(t_ms,t_ms+1,reason)])


def detections_2d(store, scene_key, cam, t0_ms, t1_ms, groups):
    """YOLO COCO boxes of one street camera in [t0_ms, t1_ms): the most boxes of a label in one frame and the read frames in
    which every asked group ({name: [labels]}) has a box."""
    started = time.monotonic()
    dets = street.detections(store,scene_key,cam,t0_ms,t1_ms)
    labels = {}
    for r in dets['rows']:
        labels.setdefault(r[0],set()).add(r[1])
    together = [t for t, seen in labels.items() if groups and all(seen & set(g) for g in groups.values())]
    value = {'counts':street.max_per_frame(dets['rows']),'frames_read':dets['frames_read'],'source':dets['source'],
             'classes':list(groups),'frames_together':len(together)}
    ev = evidence(store,scene_key,cam,t0_ms,t1_ms,'measure','yolo_coco_2d',value,params={'cam':cam,'groups':groups})
    missing = sum(b-a for a, b, _ in dets['gaps'])
    return result(scene_key,t0_ms,t1_ms,value,cam=cam,evidence=[ev],pts_read=dets['frames_read'],gaps=[tuple(g) for g in dets['gaps']],
                  coverage=(t1_ms-t0_ms-missing)/(t1_ms-t0_ms) if t1_ms>t0_ms else 0,
                  exec_state=dets['exec_state'],started=started)


def visibility(store, scene_key, t_ms, target, *, cam=None):
    m = store.manifest(scene_key)
    if not capabilities(m)['blind_spot']['ok']:
        return no_capability(m,'blind_spot',t_ms)
    moment = store.moment(scene_key,t_ms)
    person = next((p for p in moment.persons if p.entity_id==target),None) if isinstance(target,str) else None
    point = person.root_world if person else target if not isinstance(target,str) else None
    if point is None:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_target'},gaps=[(t_ms,t_ms+1,'missing_target')])
    data = store.asset_json(scene_key,m['assets']['visibility'])
    states = geometry.floor_visibility(data,t_ms,point)
    output = {'floor_cell':{'seen_by':[c for c,s in states.items() if s=='visible'],
                            'hidden_from':[c for c,s in states.items() if s=='occluded'], 'states':states}}
    if person:
        output['person'] = {'observed_in':person.observed_in,'geometric':[c for c,s in states.items() if s=='visible']}
    t0 = t_ms//1000*1000
    ev = evidence(store,scene_key,cam,t0,t0+1000,'measure','floor_cell_visibility',output,
                  params={'target':target,'t_ms':t_ms,'point':point})
    sampled = [states.get(cam,'unknown')] if cam else list(states.values())
    gaps = [(t0,t0+1000,s) for s in dict.fromkeys(sampled) if s not in {'visible','occluded'}]
    coverage = sum(s in {'visible','occluded'} for s in sampled)/len(sampled) if sampled else 0
    return result(scene_key,t0,t0+1000,output,cam=cam,evidence=[ev],pts_read=1,coverage=coverage,
                  gaps=gaps,quality='ok' if coverage==1 else 'partial' if coverage else 'unknown')


def blind_presence(store, scene_key, cam, t0_ms, t1_ms, *, selection=None, zone=None):
    started = time.monotonic()
    manifest = store.manifest(scene_key)
    if not capabilities(manifest)['blind_spot']['ok']:
        return no_capability(manifest,'blind_spot',t0_ms)
    assets = manifest['assets']
    data = store.asset_json(scene_key,assets['visibility'])
    skeleton = store.asset_json(scene_key,assets['skeleton'])
    frame = round(1000/skeleton['fps'])
    persons = skeleton['persons']
    selected = set(selection or [])
    targets = {eid:p for eid,p in persons.items() if not selected or eid in selected}
    if selected-set(persons):
        return result(scene_key,t0_ms,t1_ms,{'cam':cam,'intervals':[],'seconds_checked':0,
                      'reason':'selection_not_available'},coverage=0,gaps=[(t0_ms,t1_ms,'unknown')])
    area = Polygon(zone) if zone else None
    samples = sorted(((t,eid,root) for eid,p in targets.items() for t,root in zip(p['t_ms'],p['root_world'])
                      if t0_ms<=t<t1_ms),key=lambda x:(x[0],x[1]))
    spans, interrupted, known, reasons = {}, set(), {eid:[] for eid in targets}, {}
    for t, eid, root in samples:
        remaining(30)
        sec = t//1000
        if not root or not 0<=sec-data['t0_s']<len(data['per_sec']):
            interrupted.add(eid)
            reasons[sec] = 'missing_visibility' if root else 'unknown'
            continue
        states = geometry.floor_visibility(data,t,root)
        state = states.get(cam,'unknown')
        if state in {'unknown','no_video'}:
            interrupted.add(eid)
            reasons[sec] = state
            continue
        end = min(t+frame,t1_ms,(sec+1)*1000)
        known[eid].append((t,end))
        if state=='visible' or (area is not None and not area.covers(Point(root[0],root[1]))):
            interrupted.add(eid)
            continue
        seen = [c for c,state in states.items() if state=='visible']
        runs = spans.setdefault(eid,[])
        if runs and eid not in interrupted and t-runs[-1]['t1_ms']<=1 and runs[-1]['seen_by']==seen:
            pass
        else:
            runs.append({'entity_id':eid,'t0_ms':t,'seen_by':seen})
        runs[-1]['t1_ms'] = end
        interrupted.discard(eid)
    # Coverage is the duration observed for every selected/known person, not the
    # count of seconds containing at least one sample of any person.
    def merge(intervals):
        merged = []
        for a,b in sorted(intervals):
            if merged and a<=merged[-1][1]:
                merged[-1] = (merged[-1][0],max(b,merged[-1][1]))
            else:
                merged.append((a,b))
        return merged
    covered = [(t0_ms,t1_ms)] if targets else []
    for intervals in known.values():
        covered = merge((max(a,c),min(b,d)) for a,b in covered for c,d in merge(intervals) if max(a,c)<min(b,d))
    checked_ms = sum(b-a for a,b in covered)
    gaps = []
    for sec in range(t0_ms//1000,-(-t1_ms//1000)):
        lo,hi = max(t0_ms,sec*1000),min(t1_ms,(sec+1)*1000)
        cursor = lo
        reason = reasons.get(sec,'missing_visibility' if not 0<=sec-data['t0_s']<len(data['per_sec']) else 'unknown')
        for a,b in covered:
            a,b = max(a,lo),min(b,hi)
            if a>=b:
                continue
            if cursor<a:
                gaps.append((cursor,a,reason))
            cursor = max(cursor,b)
        if cursor<hi:
            gaps.append((cursor,hi,reason))
    intervals = sorted((r for runs in spans.values() for r in runs),key=lambda r:(r['t0_ms'],r['entity_id']))
    gaps.extend((r['t0_ms'],r['t1_ms'],'missing_thumbnail') for r in intervals
                if thumbnail(store,scene_key,(r['seen_by'] or [cam])[0],r['t0_ms']) is None)
    evs = [evidence(store,scene_key,cam,r['t0_ms'],r['t1_ms'],'measure','blind_presence',
                    {'entity_id':r['entity_id'],'cam':cam,'t0_s':r['t0_ms']/1000,'t1_s':r['t1_ms']/1000,'seen_by':r['seen_by']},
                    params={'cam':cam,'target':r['entity_id'],'zone':zone}) for r in intervals]
    return result(scene_key,t0_ms,t1_ms,{'cam':cam,'intervals':intervals,'seconds_checked':checked_ms/1000,
                  'checked_ms':checked_ms,'coverage_basis':'all selected/known persons; sampled floor visibility'},cam=cam,evidence=evs,
                  pts_read=len(samples),gaps=gaps,coverage=checked_ms/(t1_ms-t0_ms) if t1_ms>t0_ms else 0,started=started)


def measure(store, scene_key, t_ms, a, b, kind, *, cam=None):
    m = store.manifest(scene_key)
    operands = [a,b] if kind=='distance' else [a]
    if any(not hasattr(e,'entity_id') or e.scene_key!=scene_key or
           not e.entity_id.startswith({'person':'P','forklift':'F','vehicle':'V'}[e.kind]) for e in operands):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'invalid_entity_ref'},cam=cam)
    if m['workspace']=='street':
        return no_capability(m,{'distance':'distance','in_zone':'ground_position'}.get(kind,'speed'),t_ms)
    if not m['metric']:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'scene_not_metric'})
    entities = store.entities(scene_key)
    known = {e.entity_id:e.kind for e in entities}
    if any(known.get(e.entity_id)!=e.kind for e in operands):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'invalid_entity_ref'},cam=cam)
    points = {}
    if m['workspace']=='warehouse':
        moment = store.moment(scene_key,t_ms)
        for p in moment.persons:
            points[p.entity_id] = {'x':p.root_world[0],'y':p.root_world[1],'sigma_m':p.sigma_m,
                                    'seen_by':p.seen_by,'observed_in':p.observed_in}
        for f in moment.forklift:
            points[f.entity_id] = f.model_dump()
    else:
        frame = geo.ego_frame(store,scene_key,t_ms)
        if frame:
            points = {p['entity_id']:p for p in frame['peds']+frame['vehs']}
    pa = points.get(a.entity_id)
    pb = points.get(b.entity_id) if hasattr(b,'entity_id') else None
    if pa is None or (kind=='distance' and pb is None):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_measurement'},gaps=[(t_ms,t_ms+1,'missing_measurement')])
    if cam and capabilities(m)['blind_spot']['ok'] and any(
            p.get('seen_by') is not None and (cam not in p['seen_by'] or cam not in p['observed_in'])
            for p in ([pa,pb] if kind=='distance' else [pa])):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'occluded'},cam=cam,gaps=[(t_ms,t_ms+1,'occluded')])
    if kind=='distance':
        value, sigma = geometry.distance(pa,pb); unit = 'm'; method = 'ground_distance'
    elif kind=='in_zone':
        value, sigma, unit, method = Polygon(b).covers(Point(pa['x'],pa['y'])),pa['sigma_m'],None,'ground_in_zone'
    elif kind=='speed' and 'speed' in pa:
        sigma = pa.get('sigma_speed_m_s')
        if sigma is None and 'forklift' in m['assets']:
            track = next((f['track'] for f in store.asset_json(scene_key,m['assets']['forklift'])['forklifts'] if f['entity_id']==a.entity_id),[])
            i = min(range(len(track)),key=lambda i:abs(track[i]['t_ms']-t_ms)) if track else 0
            left, right = (track[max(0,i-1)],track[min(len(track)-1,i+1)]) if track else (None,None)
            if left and right['t_ms']>left['t_ms']:
                sigma = math.hypot(left['sigma_m'],right['sigma_m'])/((right['t_ms']-left['t_ms'])/1000)
        value, unit, method = pa['speed'],'m/s','track_speed'
    else:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_speed'})
    observations = []
    camera_bound = set()
    from app.api.search import box_rows
    tolerance = round(1000/m['fps']/2)+1
    for entity in entities:
        if entity.entity_id not in [a.entity_id, getattr(b,'entity_id',None)]:
            continue
        for observation in entity.observations:
            camera=next((c for c in m['cams'] if c['id']==observation['cam_id']),None)
            if camera is None or not sync_trusted(m,camera):
                continue
            if not observation['t0_ms']<=t_ms<observation['t1_ms']:
                continue
            # Association intervals alone do not establish an observed frame.
            rows = box_rows(store,scene_key,observation['cam_id'],max(m['t0_ms'],t_ms-tolerance),
                            min(m['t1_ms'],t_ms+tolerance+1))
            matches = [r for r in rows if r[1]==observation['local_track_id'] and abs(r[0]-t_ms)<=tolerance]
            if matches:
                row = min(matches,key=lambda r:abs(r[0]-t_ms))
                observations.append(ObservationRef(scene_key=scene_key,cam_id=observation['cam_id'],
                                    local_track_id=observation['local_track_id'],t_ms=row[0]))
                if observation['cam_id']==cam:
                    camera_bound.add(entity.entity_id)
    if cam and any(e.entity_id not in camera_bound for e in operands):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'occluded'},cam=cam,
                      gaps=[(t_ms,t_ms+1,'occluded')])
    ev = evidence(store,scene_key,cam,t_ms,t_ms+1,'measure',method,value,observations=observations,
                  unit=unit,sigma=sigma,params={'a':a.entity_id,'b':getattr(b,'entity_id',b),'kind':kind,'observation_role':'aligned_detection_not_position_inputs',
                  **({'distance_to':'forklift_center' if b.entity_id.startswith('F') else 'entity_ground_point',
                      'distance_from':'entity_ground_point'} if kind=='distance' else {})})
    return result(scene_key,t_ms,t_ms+1,{'value':value,'sigma':sigma,'method':method,'observation_role':'aligned_detection_not_position_inputs',**({'distance_to':'forklift_center' if b.entity_id.startswith('F') else 'entity_ground_point'} if kind=='distance' else {})},cam=cam,evidence=[ev],pts_read=1,coverage=1)



def ground_position(store, scene_key, t_ms, target, *, zone=None):
    """A valid sampled world ground point; camera visibility is not a position measurement."""
    m=store.manifest(scene_key)
    if m['workspace']!='warehouse' or not m['metric']:
        reason='scene_not_metric' if m['workspace']=='warehouse' else 'no_geometry'
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':reason})
    try:
        moment=store.moment(scene_key,t_ms)
    except FileNotFoundError:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_position_sample'})
    person=next((p for p in moment.persons if p.entity_id==target.entity_id),None)
    vehicle=next((f for f in moment.forklift if f.entity_id==target.entity_id),None)
    point=person.root_world if person else (vehicle.x,vehicle.y) if vehicle else None
    if point is None or not all(math.isfinite(v) for v in point):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_position_sample'})
    if zone and not Polygon(zone).covers(Point(point)):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'outside_requested_zone'})
    sigma=person.sigma_m if person else vehicle.sigma_m
    if not isinstance(sigma,(int,float)) or not math.isfinite(sigma) or sigma<0:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_position_uncertainty'})
    source=person.root_method if person else vehicle.source
    value={'entity_id':target.entity_id,'x_m':point[0],'y_m':point[1],'t_s':t_ms/1000,
           'coordinate_frame':'scene_world_ground','source':source}
    ev=evidence(store,scene_key,None,t_ms,t_ms+1,'measure','ground_position',value,unit='m',sigma=sigma,
                params={'target':target.entity_id,'coordinate_frame':'scene_world_ground','source':source})
    return result(scene_key,t_ms,t_ms+1,value,evidence=[ev],pts_read=1,coverage=None)


def ego_distance(store, scene_key, cam, t0_ms, t1_ms, *, rows):
    """Fleet: ground distance from the ego camera to the closest pedestrian ahead in [t0_ms, t1_ms). Live from the VSS YOLO
    sidecars (live_measure, rows = site-map rows); without a live value, the precomputed ego frames with the live reason."""
    started = time.monotonic()
    m = store.manifest(scene_key)
    cap = capabilities(m)['distance']
    if not cap['ok']:
        return result(scene_key,t0_ms,t1_ms,{'status':'UNRESOLVED','reason':cap['reason']},cam=cam,
                      gaps=[(t0_ms,t1_ms,cap['reason'])],started=started)
    peds, precomputed_gaps = [], []
    ego_dir = m['assets'].get('ego_dir')
    for chunk in range(t0_ms//10000,(t1_ms-1)//10000+1):
        remaining(30)
        try:
            frames = store.asset_json(scene_key,f'{ego_dir}/{chunk:05d}.json')['frames'] if ego_dir else []
        except FileNotFoundError:
            frames = []
            precomputed_gaps.append((max(t0_ms,chunk*10000),min(t1_ms,(chunk+1)*10000),'missing_precomputed_ego'))
        peds.extend((f['t_ms'],p,math.hypot(p['x'],p['y'])) for f in frames if t0_ms<=f['t_ms']<t1_ms
                    for p in f['peds'] if p['x']>0)
    near = min((x for x in peds if x[2]<=live_measure.MAX_RANGE_M),key=lambda x:(x[2],x[0]),default=None)
    precomputed = {'value_m':round(near[2],3),'sigma_m':near[1]['sigma_m'],'t_ms':near[0],'entity_id':near[1]['entity_id']} if near else None
    try:
        live = live_measure.measure(store,scene_key,cam,t0_ms,t1_ms,pair=('person','vehicle'),rows=rows)
    except DeadlineExceeded:
        raise
    except Exception as exc:
        logger.exception('live_measure failed for %s/%s [%s, %s)',scene_key,cam,t0_ms,t1_ms)
        live = {'value_m':None,'reason':f'{type(exc).__name__}: {exc}'}
    output = {'live':live,'precomputed':precomputed,'minimum_basis':'valid observed samples',
              'distance_from':'ego_ground_origin','distance_to':'pedestrian_ground_point',
              'coverage_basis':'full-window coverage not measured','precomputed_gaps':precomputed_gaps}
    if live['value_m'] is not None:
        t = live['t_ms']
        ev = evidence(store,scene_key,cam,t,t+1,'measure','ego_distance',live['value_m'],unit='m',sigma=live['sigma_m'],
                      params={'source':'live_measure','source_uris':live['source_uris']})
        return result(scene_key,t0_ms,t1_ms,{**output,'value':live['value_m'],'sigma':live['sigma_m'],'t_ms':t},cam=cam,
                      evidence=[ev],pts_read=live['n_frames'],coverage=None,exec_state='live',started=started)
    if precomputed is None:
        return result(scene_key,t0_ms,t1_ms,{**output,'status':'UNRESOLVED','reason':live['reason']},cam=cam,
                      gaps=[(t0_ms,t1_ms,'missing_measurement')],started=started)
    t = precomputed['t_ms']
    ev = evidence(store,scene_key,None,t,t+1,'measure','ego_distance',precomputed['value_m'],unit='m',sigma=precomputed['sigma_m'],
                  params={'source':'ego_frames','entity_id':precomputed['entity_id']})
    return result(scene_key,t0_ms,t1_ms,{**output,'value':precomputed['value_m'],'sigma':precomputed['sigma_m'],'t_ms':t,
                                          'reason':live['reason']},cam=cam,evidence=[ev],pts_read=len(peds),coverage=None,started=started)


def bound_ego_distance(store, scene_key, t_range, target, *, zone=None):
    """Minimum and maximum sampled distances to one named published pedestrian; evidence is ordered min, max."""
    m=store.manifest(scene_key)
    lo,hi=t_range
    lo,hi=min(max(lo,m['t0_ms']),m['t1_ms']),max(min(hi,m['t1_ms']),m['t0_ms'])
    if lo>=hi:
        return result(scene_key,lo,lo,{'status':'UNRESOLVED','reason':'missing_bound_ego_sample'},coverage=0)
    if m['workspace']!='fleet' or not m['metric'] or target.kind!='person':
        return result(scene_key,lo,hi,{'status':'UNRESOLVED','reason':'unsupported_bound_ego_measurement'})
    directory=m['assets'].get('ego_dir')
    if not directory:
        return result(scene_key,lo,hi,{'status':'UNRESOLVED','reason':'missing_bound_ego_sample'},coverage=0,
                      gaps=[(lo,hi,'missing_bound_ego_sample')])
    candidates=[]
    area=Polygon(zone) if zone else None
    for chunk in range(lo//10000,(hi-1)//10000+1):
        remaining(30)
        try:
            frames=store.asset_json(scene_key,f'{directory}/{chunk:05d}.json')['frames']
        except FileNotFoundError:
            frames=[]
        for frame in frames:
            if not lo<=frame['t_ms']<hi: continue
            for p in frame['peds']:
                if p['entity_id']!=target.entity_id or area and not area.covers(Point(p['x'],p['y'])): continue
                if not all(isinstance(p.get(k),(int,float)) and math.isfinite(p[k]) for k in ('x','y','sigma_m')) or p['sigma_m']<0: continue
                candidates.append((math.hypot(p['x'],p['y']),frame['t_ms'],p))
    if not candidates:
        return result(scene_key,lo,hi,{'status':'UNRESOLVED','reason':'missing_bound_ego_sample'},coverage=0,
                      gaps=[(lo,hi,'missing_bound_ego_sample')])
    extremes=[min(candidates,key=lambda row:(row[0],row[1])),min(candidates,key=lambda row:(-row[0],row[1]))]
    evs=[]
    for extreme,(distance,tm,p) in zip(('min','max'),extremes):
        evs.append(evidence(store,scene_key,None,tm,tm+1,'measure','bound_ego_distance',{'distance_m':distance,'target_entity_id':target.entity_id,
                    'reference_entity':'ego','coordinate_frame':'ego_ground','t_s':tm/1000},unit='m',sigma=p['sigma_m'],
                    params={'a':target.entity_id,'b':'ego','source':'published_ego_frames','zone':zone,'extreme':extreme,
                            'distance_from':'ego_ground_origin','distance_to':'pedestrian_ground_point'}))
    low,high=evs
    return result(scene_key,lo,hi,{'value':low.value['distance_m'],'sigma':low.sigma,'t_ms':low.t0_ms,'entity_id':target.entity_id,
                  'maximum_value':high.value['distance_m'],'maximum_sigma':high.sigma,'maximum_t_ms':high.t0_ms,
                  'minimum_basis':'valid observed samples','maximum_basis':'valid observed samples',
                  'coverage_basis':'full-window coverage not measured'},evidence=evs,pts_read=len(candidates))


def ego_speed(store, scene_key, t_ms):
    """The recording vehicle's OBD sample, with explicit sample age and absent uncertainty."""
    m=store.manifest(scene_key)
    if m['workspace']!='fleet' or 'trip' not in m['assets']:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'no_ego_speed'})
    trip=store.asset_json(scene_key,m['assets']['trip'])
    points=[f['properties'] for f in trip['features'][1:] if f['properties'].get('t_ms') is not None]
    p=next((p for p in reversed(points) if p['t_ms']<=t_ms<p['t_ms']+500),None)
    speed=p.get('speed') if p else None
    if not p or p.get('state') not in {'observed','interpolated'} or not isinstance(speed,(int,float)) or not math.isfinite(speed):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_ego_speed_sample'},coverage=0)
    ev=evidence(store,scene_key,None,p['t_ms'],p['t_ms']+1,'measure','obd_speed',speed*3.6,unit='km/h',
                params={'entity_id':'ego','sample_age_ms':t_ms-p['t_ms'],'uncertainty':'unavailable'})
    return result(scene_key,t_ms,t_ms+1,{'value':speed*3.6,'t_ms':p['t_ms'],'sigma':None,
                  'unit':'km/h','entity_id':'ego','uncertainty':'unavailable'},evidence=[ev],pts_read=1)


def closest_approach(store, scenes, t_range=None, zone=None, *, selection=None):
    """The smallest person-forklift ground distance at the skeleton sample times, reading each track asset once;
    with a zone, only people standing inside it count."""
    started = time.monotonic()
    area = Polygon(zone) if zone else None
    selected = set(selection or [])
    if any(not e.startswith(('P','F')) for e in selected):
        return result('all',0,0,{'closest':None,'missing':sorted(selected),'scene_coverage':{},'reason':'selection_not_available'},coverage=0)
    best, seen, pts, scene_coverage, gaps = None, set(), 0, {}, []
    for m in scenes:
        remaining(30)
        key, assets = m['scene_key'], m['assets']
        t0, t1 = (max(m['t0_ms'],t_range[0]),min(m['t1_ms'],t_range[1])) if t_range else (m['t0_ms'],m['t1_ms'])
        cap = capabilities(m)['distance']
        if not cap['ok']:
            scene_coverage[key] = {'coverage':0,'reason':cap['reason']}
            if t0<t1:
                gaps.append((t0,t1,cap['reason']))
            continue
        skeleton = store.asset_json(key,assets['skeleton']) if 'skeleton' in assets else {}
        persons = skeleton.get('persons',{})
        frame_ms = round(1000/skeleton.get('fps',m['fps']))
        valid_spans = []
        tracks = store.asset_json(key,assets['forklift'])['forklifts'] if 'forklift' in assets else []
        samples = [(t,eid,root,s) for eid,p in persons.items() for t,root,s in zip(p['t_ms'],p['root_world'],p['root_sigma_m'])
                   if t0<=t<t1 and root and (not {x for x in selected if x.startswith('P')} or eid in selected) and (area is None or area.covers(Point(root[0],root[1])))]
        tracks = [(f['entity_id'],f['track'],[r['t_ms'] for r in f['track']]) for f in tracks
                  if (not {x for x in selected if x.startswith('F')} or f['entity_id'] in selected) and f['track'] and f['track'][0]['t_ms']<t1 and f['track'][-1]['t_ms']>=t0]
        scene_coverage[key] = {'coverage':1 if samples and tracks else 0,'reason':None if samples and tracks else 'missing_tracks'}
        if samples:
            seen.add('person')
        if tracks:
            seen.add('forklift')
        for t, eid, root, s in samples:
            remaining(30)
            for fid, track, times in tracks:
                if not times[0]<=t<=times[-1]:
                    continue
                # the sample store.moment picks: nearest in time, the earlier one on a tie
                i = bisect.bisect_left(times,t)
                i = i-1 if i>0 and t-times[i-1]<=times[i]-t else i
                if abs(times[i]-t)>1000/m['fps']/2+1:
                    gaps.append((t,t+1,'stale_forklift_sample'))
                    continue
                value, _ = geometry.distance({'x':root[0],'y':root[1],'sigma_m':s},track[i])
                valid_spans.append((t,min(t+frame_ms,t1)))
                pts += 1
                if best is None or (value,t)<(best['value'],best['t_ms']):
                    best = {'scene_key':key,'t_ms':t,'a':eid,'b':fid,'value':value}
        merged = []
        for lo,hi in sorted(valid_spans):
            if merged and lo<=merged[-1][1]:
                merged[-1] = (merged[-1][0],max(hi,merged[-1][1]))
            else:
                merged.append((lo,hi))
        coverage = sum(hi-lo for lo,hi in merged)/(t1-t0) if t1>t0 else 0
        scene_coverage[key] = {'coverage':coverage,'reason':None if coverage==1 else 'incomplete_sample_coverage',
                               'basis':'times with a valid person/forklift sampled pair'}
    missing = [k for k in ('person','forklift') if k not in seen]
    key = best['scene_key'] if best else scenes[0]['scene_key'] if scenes else 'all'
    t0, t1 = t_range or (min((m['t0_ms'] for m in scenes),default=0),max((m['t1_ms'] for m in scenes),default=0))
    evs = [evidence(store,key,None,best['t_ms'],best['t_ms']+1,'measure','closest_approach',{**best,'t_s':best['t_ms']/1000,
                    'window_t0_s':max(store.manifest(key)['t0_ms'],t0)/1000,
                    'window_t1_s':min(store.manifest(key)['t1_ms'],t1)/1000,'distance_to':'forklift_center','distance_from':'person_root','minimum_basis':'valid sampled pairs'},
                    params={'t_range':t_range,'zone':zone,'selection':selection,'distance_to':'forklift_center','distance_from':'person_root'})] if best else []
    if best:
        cams = store.manifest(key)['cams']
        if not cams or thumbnail(store,key,cams[0]['id'],best['t_ms']) is None:
            gaps.append((best['t_ms'],best['t_ms']+1,'missing_thumbnail'))
    output = {'closest':best,'missing':missing,'scene_coverage':scene_coverage}
    if scene_coverage and all(c['reason']=='scene_not_metric' for c in scene_coverage.values()):
        output.update(status='UNRESOLVED',reason='scene_not_metric')
    return result(key,t0,t1,output,evidence=evs,gaps=gaps,pts_read=pts,started=started)


def farthest_sample(store, scenes, t_range=None, zone=None, *, selection=None):
    """The largest person-forklift ground distance at the skeleton sample times closest_approach scans; picks a time only."""
    area = Polygon(zone) if zone else None
    selected = set(selection or [])
    best, seen = None, set()
    for m in scenes:
        key, assets = m['scene_key'], m['assets']
        t0, t1 = (max(m['t0_ms'],t_range[0]),min(m['t1_ms'],t_range[1])) if t_range else (m['t0_ms'],m['t1_ms'])
        if not capabilities(m)['distance']['ok']:
            continue
        persons = store.asset_json(key,assets['skeleton']).get('persons',{}) if 'skeleton' in assets else {}
        tracks = store.asset_json(key,assets['forklift'])['forklifts'] if 'forklift' in assets else []
        samples = [(t,eid,root,s) for eid,p in persons.items() for t,root,s in zip(p['t_ms'],p['root_world'],p['root_sigma_m'])
                   if t0<=t<t1 and root and (not {x for x in selected if x.startswith('P')} or eid in selected) and (area is None or area.covers(Point(root[0],root[1])))]
        tracks = [(f['entity_id'],f['track'],[r['t_ms'] for r in f['track']]) for f in tracks
                  if (not {x for x in selected if x.startswith('F')} or f['entity_id'] in selected) and f['track']]
        seen |= {'person'} if samples else set()
        seen |= {'forklift'} if tracks else set()
        for t, eid, root, s in samples:
            remaining(30)
            for fid, track, times in tracks:
                if not times[0]<=t<=times[-1]:
                    continue
                i = bisect.bisect_left(times,t)
                i = i-1 if i>0 and t-times[i-1]<=times[i]-t else i
                if abs(times[i]-t)>1000/m['fps']/2+1:
                    continue
                value, _ = geometry.distance({'x':root[0],'y':root[1],'sigma_m':s},track[i])
                if best is None or (-value,t)<(-best['value'],best['t_ms']):
                    best = {'scene_key':key,'t_ms':t,'a':eid,'b':fid,'value':value}
    key = best['scene_key'] if best else scenes[0]['scene_key'] if scenes else 'all'
    t0, t1 = t_range or (min((m['t0_ms'] for m in scenes),default=0),max((m['t1_ms'] for m in scenes),default=0))
    return result(key,t0,t1,{'farthest':best,'missing':[k for k in ('person','forklift') if k not in seen]})


def geocode(store, name):
    place = geo.geocode(store,name)
    scenes = store.list_scenes('fleet')
    key = scenes[0]['scene_key'] if scenes else 'all'
    evs = [evidence(store,key,None,0,0,'map','geocode',place,params={'name':name})] if place else []
    return result(key,0,0,place,evidence=evs,pts_read=1 if place else 0)


def find_passes(store, place_id, r_m, fov_deg):
    passes = geo.find_passes(store,place_id,r_m,fov_deg)
    evs = [evidence(store,p['trip_id'],None,p['t0_ms'],p['t1_ms'],'map','gps_pass',{**p,'t0_s':p['t0_ms']/1000,'t1_s':p['t1_ms']/1000},
                    params={'place_id':place_id,'r_m':r_m,'fov_deg':fov_deg}) for p in passes]
    return result(passes[0]['trip_id'] if passes else 'all',min((p['t0_ms'] for p in passes),default=0),
                  max((p['t1_ms'] for p in passes),default=0),passes,evidence=evs,pts_read=len(passes))


def searched_passes(store, passes, t_range=None):
    """The pass intervals a place search covered, clipped to each trip's span and t_range, and their count."""
    searched = geo.trim_passes(store,passes,t_range)
    evs = [evidence(store,p['trip_id'],None,p['t0_ms'],p['t1_ms'],'map','gps_pass',{**p,'t0_s':p['t0_ms']/1000,'t1_s':p['t1_ms']/1000})
           for p in searched]
    if searched:
        evs.insert(0,evidence(store,searched[0]['trip_id'],None,min(p['t0_ms'] for p in searched),max(p['t1_ms'] for p in searched),
                              'map','searched_passes',{'count':len(searched)}))
    return result(searched[0]['trip_id'] if searched else 'all',min((p['t0_ms'] for p in searched),default=0),
                  max((p['t1_ms'] for p in searched),default=0),searched,evidence=evs,pts_read=len(searched))


def facade(store, passes, lat, lon, side):
    """The widest facade on the asked side (left/right of the principal point; None = any) where the car is closest."""
    started = time.monotonic()
    key, frame = geo.closest_frame(store,passes,lat,lon)
    if frame is None:
        return result(passes[0]['trip_id'],passes[0]['t0_ms'],passes[0]['t1_ms'],{'t_ms':None,'building':None},
                      gaps=[(passes[0]['t0_ms'],passes[0]['t1_ms'],'missing_ego')],started=started)
    t, cam = frame['t_ms'], store.manifest(key)['cams'][0]
    found = [b for b in geo.facades(store,key,t) if side is None or ((b['u0']+b['u1'])/2>cam['K'][2])==(side=='right')]
    b = max(found,key=lambda b:b['u1']-b['u0'],default=None)
    if b is None:
        return result(key,t,t+1,{'scene_key':key,'t_ms':t,'side':side,'building':None},cam=cam['id'],pts_read=1,started=started)
    label = b.get('name') or next((p['name'] for p in b['pois']),None) or b.get('addr') or 'OSM '+b['osm_id']
    value = {'bid':b['bid'],'label':label,'name':b.get('name'),'addr':b.get('addr'),'pois':b['pois'],'osm_id':b['osm_id'],
             'u0':b['u0'],'u1':b['u1'],'side':side}
    ev = evidence(store,key,cam['id'],t,t+1,'map','osm_facade',value,params={'side':side})
    gaps = [(t,t+1,'missing_thumbnail')] if thumbnail(store,key,cam['id'],t) is None else []
    return result(key,t,t+1,{'scene_key':key,'t_ms':t,'side':side,'building':value},cam=cam['id'],evidence=[ev],pts_read=1,gaps=gaps,started=started)


def gps_position(store, scene_key, t_ms):
    m = store.manifest(scene_key)
    points = [f['properties'] for f in store.asset_json(scene_key,m['assets']['trip'])['features'][1:]] if 'trip' in m['assets'] else []
    point = next((p for p in reversed(points) if p['t_ms']<=t_ms<p['t_ms']+500),None)
    if point is None or point['state'] not in {'observed','interpolated'}:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'no_gps'},gaps=[(t_ms,t_ms+1,'no_gps')],coverage=0)
    value = {k:point[k] for k in ('lat','lon','state','t_ms')}
    ev = evidence(store,scene_key,None,t_ms,t_ms+1,'map','gps_position',value)
    return result(scene_key,t_ms,t_ms+1,value,evidence=[ev],pts_read=1,coverage=1)


def speed_limit(store, scene_key, t_ms):
    m = store.manifest(scene_key)
    if m['workspace']=='street':
        return no_capability(m,'speed',t_ms)
    out = geo.speed_limit(store,scene_key,t_ms)
    if 'speed_kmh' not in out:
        return result(scene_key,t_ms,t_ms+1,out,gaps=[(t_ms,t_ms+1,out['reason'])])
    t = out['t_ms']
    evs = [evidence(store,scene_key,None,t,t+1,'measure','obd_speed',out['speed_kmh'],unit='km/h'),
           evidence(store,scene_key,None,t,t+1,'map','osm_maxspeed',out['value'],unit='km/h')]
    return result(scene_key,t,t+1,out,evidence=evs,pts_read=1)


def speeding_candidates(store, scenes, t_range=None, k=3, passes=None):
    started = time.monotonic()
    best, maxima, gaps, scene_coverage, total, checked, pts = [], [], [], {}, 0, 0, 0
    for m in scenes:
        key = m['scene_key']
        lo, hi = t_range or (m['t0_ms'],m['t1_ms'])
        spans = [(lo,hi)] if passes is None else [(max(lo,p['t0_ms']),min(hi,p['t1_ms'])) for p in passes if p['trip_id']==key]
        spans = [(a,b) for a,b in spans if a<b]
        points = sorted((f['properties'] for f in store.asset_json(key,m['assets']['trip'])['features'][1:]),key=lambda p:p['t_ms']) if 'trip' in m['assets'] else []
        limited, available, missing = [], [], []
        for i,p in enumerate(points):
            a, b = p['t_ms'], min(p['t_ms']+500,points[i+1]['t_ms']) if i+1<len(points) else p['t_ms']+500
            if not any(x<b and a<y for x,y in spans):
                continue
            pts += 1
            next_gap = i+1<len(points) and points[i+1]['state'] not in {'observed','interpolated'}
            reason = ('no_gps' if p['state'] not in {'observed','interpolated'} or p.get('speed') is None else
                      'no_speed_limit' if p['maxspeed_kmh'] is None else None)
            if reason or next_gap:
                missing.append((a,b,reason or 'no_gps'))
            else:
                available.append((a,b))
            if reason is None and any(x<=a<y for x,y in spans):
                limited.append(p)
        scene_gaps = [g for a,b in spans for g in street.availability_gaps(a,b,available,missing)]
        length = sum(b-a for a,b in spans)
        covered = length-sum(b-a for a,b,_ in scene_gaps)
        total += length; checked += covered
        gaps.extend(tuple(g) for g in scene_gaps)
        scene_coverage[key] = {'coverage':covered/length if length else 0,'gaps':scene_gaps}
        if limited:
            for candidates, p in [(best,max(limited,key=lambda p:p['speed']*3.6-p['maxspeed_kmh'])),
                                  (maxima,max(limited,key=lambda p:p['speed']))]:
                candidates.append({'scene_key':key,'t_ms':p['t_ms'],'speed_kmh':p['speed']*3.6,'limit_kmh':p['maxspeed_kmh'],
                                   'over_kmh':p['speed']*3.6-p['maxspeed_kmh']})
    best = (sorted(maxima,key=lambda c:-c['speed_kmh']) if best and all(c['over_kmh']<=0 for c in best) else
            sorted(best,key=lambda c:-c['over_kmh']))[:k]
    for c in best:
        cams = store.manifest(c['scene_key'])['cams']
        if cams and thumbnail(store,c['scene_key'],cams[0]['id'],c['t_ms']) is None:
            gaps.append((c['t_ms'],c['t_ms']+1,'missing_thumbnail'))
    evs = [evidence(store,c['scene_key'],None,c['t_ms'],c['t_ms']+1,'measure','obd_over_limit',c['over_kmh'],unit='km/h') for c in best]
    t0, t1 = t_range or (min((m['t0_ms'] for m in scenes),default=0),max((m['t1_ms'] for m in scenes),default=0))
    return result(best[0]['scene_key'] if best else 'all',t0,t1,{'candidates':best,'scene_coverage':scene_coverage},
                  evidence=evs,pts_read=pts,gaps=gaps,coverage=checked/total if total else 0,started=started)


def events(store, scene_key, t0_ms, t1_ms, zone=None, *, selection=None):
    found = store.events_in(scene_key,t0_ms,t1_ms)
    output = []
    for e in found:
        if selection and not set(selection)<=set(e.actor_ids):
            continue
        value = e.model_dump()
        if e.type=='near_miss':
            value.update(distance_to='forklift_footprint',distance_from='person_root')
        if zone:
            from app.api.scenes import event_zone_time
            t = event_zone_time(store,scene_key,e,t0_ms,t1_ms,Polygon(zone))
            if t is None:
                continue
            value['params'] = {**value['params'],'zone_t_ms':t}
        output.append(value)
    zone_hash=hashlib.sha256(json.dumps(zone,separators=(',',':')).encode()).hexdigest()
    evs = [evidence(store,scene_key,None,max(t0_ms,e['t0_ms']),min(t1_ms,e['t1_ms']),'model' if e['source']=='model' else 'measure','event_'+e['source'],e,
                    params={'event_id':e['event_id'],'zone_hash':zone_hash}) for e in output]
    return result(scene_key,t0_ms,t1_ms,output,evidence=evs,pts_read=len(found))


def cosmos(store, task, scene_key, t_ms, args, *, run=None, circuit=None):
    started = time.monotonic()
    circuit = circuit if circuit is not None else {'failures':0}
    prefix = task=='predict_next'
    m = store.manifest(scene_key)
    t0, t1 = max(0,t_ms-(2000 if prefix else 3000)), t_ms if prefix else min(m['t1_ms'],t_ms+3000)
    if task=='localize':
        t0, t1 = max(m['t0_ms'],args['t0_ms']), min(m['t1_ms'],args['t1_ms'])
    cache = vlm._cache_dir()
    stored = {p.name for p in cache.glob('*.json')}
    try:
        if circuit['failures']>=2:
            raise vlm.VLMUnavailable('circuit_open')
        record = getattr(vlm,task)(scene_key,**args,run=run)
        hard_range = args.get('t_range')
        requested_cam = args.get('cam')
        if hard_range and (not record.input_frame_refs or any(not hard_range[0]<=f.t_ms<hard_range[1] for f in record.input_frame_refs)):
            raise vlm.VLMUnavailable('invalid_model_time_scope')
        if requested_cam is not None and (record.cam_id!=requested_cam or any(f.cam_id!=requested_cam for f in record.input_frame_refs)):
            raise vlm.VLMUnavailable('invalid_model_camera_scope')
        if prefix and (not record.prefix_only or not record.input_frame_refs or
                       any(not t0<=f.t_ms<t_ms for f in record.input_frame_refs) or
                       record.payload['max_frame_ms'] != max(f.t_ms for f in record.input_frame_refs)):
            raise vlm.VLMUnavailable('invalid_prediction_prefix')
        circuit['failures']=0
    except vlm.VLMUnavailable:
        circuit['failures']+=1
        return result(scene_key,t0,t1,{'status':'UNRESOLVED'},gaps=[(t0,t1,'vlm_unavailable')],
                      exec_state='failed',quality='unavailable',error='vlm_unavailable',started=started)
    if record.input_frame_refs:
        t0=min(f.t_ms for f in record.input_frame_refs)
        t1=max(f.t_ms for f in record.input_frame_refs)+1
    ev = evidence(store,scene_key,record.cam_id,t0,t1,'model','cosmos_'+record.task,record.payload,
                  params={'record_id':record.record_id})
    name = record.record_id+'.json'
    state = getattr(record, '_sva_exec_state', 'cached' if name in stored else 'live' if (cache/name).is_file() else 'precomputed')
    return result(scene_key,t0,t1,{'record':record.model_dump(mode='json')},cam=record.cam_id,evidence=[ev],
                  pts_read=len(record.input_frame_refs),model_rev=record.model_rev,
                  exec_state=state,started=started)


def situation(store, scene_key, t_ms, actor_refs, *, cam=None, t_range=None, run=None, circuit=None):
    return cosmos(store,'situation',scene_key,t_ms,{'t_ms':t_ms,'actor_refs':actor_refs,
                  **({'cam':cam} if cam is not None else {}),**({'t_range':t_range} if t_range else {})},run=run,circuit=circuit)


def predict_next(store, scene_key, t_ms, actor_ref, horizon_s=2, *, run=None, circuit=None):
    return cosmos(store,'predict_next',scene_key,t_ms,{'t_ms':t_ms,'actor_ref':actor_ref,'horizon_s':horizon_s},run=run,circuit=circuit)


def ground_objects(store, scene_key, cam, t_ms, vocab, *, run=None, circuit=None):
    return cosmos(store,'ground_objects',scene_key,t_ms,{'cam':cam,'t_ms':t_ms,'vocab':vocab},run=run,circuit=circuit)


def localize(store, scene_key, cam, t0_ms, t1_ms, query, *, run=None, circuit=None):
    return cosmos(store,'localize',scene_key,(t0_ms+t1_ms)//2,{'cam':cam,'t0_ms':t0_ms,'t1_ms':t1_ms,'query':query},run=run,circuit=circuit)


def describe_region(store, scene_key, cam, t_ms, region_ref, *, run=None, circuit=None):
    return cosmos(store,'describe_region',scene_key,t_ms,{'cam':cam,'t_ms':t_ms,'region_ref':region_ref},run=run,circuit=circuit)


def trigger(store, req):
    from app.api.actions import trigger as action_trigger
    action = action_trigger(store,req)
    alert = Settings().mode=='site' and action.delivered and not action.existing
    return result(req.scene_key,0,0,action.model_dump(),exec_state='live' if alert else 'precomputed')
