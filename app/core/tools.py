"""Agent tools return evidence with the actual input range and execution state."""
from __future__ import annotations

import bisect
import time

from shapely.geometry import Point, Polygon

from app.core import geo, geometry, vlm
from app.core.models import Envelope, Evidence, InputRange, ObservationRef, ToolResult


def result(key, t0, t1, output, *, cam=None, evidence=(), pts_read=0, model_rev=None, gaps=(),
           coverage=None, quality='ok', exec_state='live', error=None, started=None):
    return ToolResult(envelope=Envelope(input_range=InputRange(scene_key=key,cam_id=cam,t0_ms=t0,t1_ms=t1),
                                       pts_read=pts_read,model_rev=model_rev,output=output,coverage=coverage,
                                       gaps=list(gaps),quality=quality,elapsed_ms=round((time.monotonic()-started)*1000) if started else 0,
                                       exec_state=exec_state,error=error),evidence=list(evidence))


def evidence(store, key, cam, t0, t1, kind, method, value, *, observations=(), unit=None, sigma=None, params=None):
    return Evidence.create(scene_key=key,cam_id=cam,t0_ms=t0,t1_ms=t1,observation_ids=list(observations),
                           analysis_rev=store.manifest(key)['analysis_rev'],kind=kind,method=method,
                           value=value,unit=unit,sigma=sigma,params=params)


def search(store, q, filters, *, parsed=None, vss=None):
    from app.api.search import SearchRequest, retrieve
    return retrieve(store,SearchRequest(q=q,**filters),parsed=parsed,vss=vss)[1]


def get_synced_views(store, scene_key, t_ms, *, cam=None):
    from app.api.search import box_rows
    started = time.monotonic()
    m = store.manifest(scene_key)
    cams = [c for c in m['cams'] if cam is None or c['id']==cam]
    frames, obs, gaps = [], [], []
    for c in cams:
        rows = box_rows(store,scene_key,c['id'],t_ms,t_ms+1)
        lane = store.asset_json(scene_key,m['assets']['timeline'])['lanes'][c['id']]
        no_video = any(a<=t_ms<b and state=='no_video' for a,b,state in lane)
        if no_video:
            gaps.append((t_ms,t_ms+1,'no_video'))
            continue
        frames.append({'id':c['id'],'frame_url':store.asset_url(scene_key,f"{c['thumbs']}/{t_ms//1000:05d}.jpg"),
                       'media_pts_ms':t_ms-c['offset_ms'],'boxes':rows})
        obs.extend(ObservationRef(scene_key=scene_key,cam_id=c['id'],local_track_id=r[1],t_ms=r[0]) for r in rows)
    ev = evidence(store,scene_key,cam,t_ms,t_ms+1,'measure','synced_views',{'cams':frames},observations=obs)
    return result(scene_key,t_ms,t_ms+1,{'cams':frames},cam=cam,evidence=[ev],pts_read=len(frames),
                  gaps=gaps,coverage=len(frames)/len(cams) if cams else 0,started=started)


def visibility(store, scene_key, t_ms, target, *, cam=None):
    m = store.manifest(scene_key)
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
    ev = evidence(store,scene_key,cam,t0,t0+1000,'measure','floor_cell_visibility',output,params={'target':target})
    return result(scene_key,t0,t0+1000,output,cam=cam,evidence=[ev],pts_read=1,coverage=1)


def blind_presence(store, scene_key, cam, t0_ms, t1_ms):
    started = time.monotonic()
    assets = store.manifest(scene_key)['assets']
    data = store.asset_json(scene_key,assets['visibility'])
    skeleton = store.asset_json(scene_key,assets['skeleton'])
    frame = round(1000/skeleton['fps'])
    seconds = range(t0_ms//1000,-(-t1_ms//1000))
    missing = {s for s in seconds if not 0<=s-data['t0_s']<len(data['per_sec'])}
    sampled = {t//1000 for p in skeleton['persons'].values() for t in p['t_ms'] if t0_ms<=t<t1_ms}
    # a second without skeleton samples was not checked: the pose layer may have lost a person who is still there
    unseen = {(s,'unknown') for s in seconds if s not in missing and s not in sampled}
    samples = sorted(((t,eid,root) for eid,p in skeleton['persons'].items() for t,root in zip(p['t_ms'],p['root_world'])
                      if t0_ms<=t<t1_ms and t//1000 not in missing),key=lambda x:(x[0],x[1]))
    spans = {}
    for t, eid, root in samples:
        states = geometry.floor_visibility(data,t,root)
        if states[cam] in {'unknown','no_video'}:
            unseen.add((t//1000,states[cam]))
            continue
        if states[cam]=='visible':
            continue
        seen = [c for c,state in states.items() if state=='visible']
        runs = spans.setdefault(eid,[])
        # visibility is a 1 s grid, so a shorter visible gap is a root on a cell edge, not a person leaving the blind area
        if runs and t-runs[-1]['t1_ms']<1000:
            runs[-1]['seen_by'] = [c for c in data['cam_bit_order'] if c in seen or c in runs[-1]['seen_by']]
        else:
            runs.append({'entity_id':eid,'t0_ms':t,'seen_by':seen})
        runs[-1]['t1_ms'] = min(t+frame,t1_ms)
    gaps = sorted([(s,'missing_visibility') for s in missing]+list(unseen))
    gaps = [(max(s*1000,t0_ms),min(s*1000+1000,t1_ms),reason) for s,reason in gaps]
    checked = len(set(seconds)-missing-{s for s,_ in unseen})
    intervals = sorted((r for runs in spans.values() for r in runs),key=lambda r:(r['t0_ms'],r['entity_id']))
    evs = [evidence(store,scene_key,cam,r['t0_ms'],r['t1_ms'],'measure','blind_presence',
                    {'entity_id':r['entity_id'],'cam':cam,'t0_s':r['t0_ms']/1000,'t1_s':r['t1_ms']/1000,'seen_by':r['seen_by']},
                    params={'cam':cam,'target':r['entity_id']}) for r in intervals]
    return result(scene_key,t0_ms,t1_ms,{'cam':cam,'intervals':intervals,'seconds_checked':checked},cam=cam,evidence=evs,
                  pts_read=len(samples),gaps=gaps,coverage=checked/len(seconds) if seconds else 0,started=started)


def measure(store, scene_key, t_ms, a, b, kind, *, cam=None):
    m = store.manifest(scene_key)
    if not m['metric']:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'scene_not_metric'})
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
    if cam and pa.get('seen_by') is not None and (cam not in pa['seen_by'] or cam not in pa['observed_in']):
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'occluded'},cam=cam,gaps=[(t_ms,t_ms+1,'occluded')])
    if kind=='distance':
        value, sigma = geometry.distance(pa,pb); unit = 'm'; method = 'ground_distance'
    elif kind=='in_zone':
        value, sigma, unit, method = Polygon(b).covers(Point(pa['x'],pa['y'])),pa['sigma_m'],None,'ground_in_zone'
    elif kind=='speed' and 'speed' in pa:
        value, sigma, unit, method = pa['speed'],pa['sigma_m'],'m/s','track_speed'
    else:
        return result(scene_key,t_ms,t_ms+1,{'status':'UNRESOLVED','reason':'missing_speed'})
    observations = []
    for entity in store.entities(scene_key):
        if entity.entity_id not in [a.entity_id, getattr(b,'entity_id',None)]:
            continue
        observations.extend(ObservationRef(scene_key=scene_key,cam_id=o['cam_id'],local_track_id=o['local_track_id'],t_ms=t_ms)
                            for o in entity.observations if o['t0_ms']<=t_ms<o['t1_ms'])
    ev = evidence(store,scene_key,cam,t_ms,t_ms+1,'measure',method,value,observations=observations,
                  unit=unit,sigma=sigma,params={'a':a.entity_id,'b':getattr(b,'entity_id',b),'kind':kind})
    return result(scene_key,t_ms,t_ms+1,{'value':value,'sigma':sigma,'method':method},cam=cam,evidence=[ev],pts_read=1,coverage=1)


def closest_approach(store, scenes, t_range=None, zone=None):
    """The smallest person-forklift ground distance at the skeleton sample times, reading each track asset once;
    with a zone, only people standing inside it count."""
    started = time.monotonic()
    area = Polygon(zone) if zone else None
    best, seen, pts = None, set(), 0
    for m in scenes:
        key, assets = m['scene_key'], m['assets']
        t0, t1 = (max(m['t0_ms'],t_range[0]),min(m['t1_ms'],t_range[1])) if t_range else (m['t0_ms'],m['t1_ms'])
        persons = store.asset_json(key,assets['skeleton'])['persons'] if 'skeleton' in assets else {}
        tracks = store.asset_json(key,assets['forklift'])['forklifts'] if 'forklift' in assets else []
        samples = [(t,eid,root,s) for eid,p in persons.items() for t,root,s in zip(p['t_ms'],p['root_world'],p['root_sigma_m'])
                   if t0<=t<t1 and root and (area is None or area.covers(Point(root[0],root[1])))]
        tracks = [(f['entity_id'],f['track'],[r['t_ms'] for r in f['track']]) for f in tracks
                  if any(t0<=r['t_ms']<t1 for r in f['track'])]
        if samples:
            seen.add('person')
        if tracks:
            seen.add('forklift')
        for t, eid, root, s in samples:
            for fid, track, times in tracks:
                if not times[0]<=t<=times[-1]:
                    continue
                # the sample store.moment picks: nearest in time, the earlier one on a tie
                i = bisect.bisect_left(times,t)
                i = i-1 if i>0 and t-times[i-1]<=times[i]-t else i
                value, _ = geometry.distance({'x':root[0],'y':root[1],'sigma_m':s},track[i])
                pts += 1
                if best is None or (value,t)<(best['value'],best['t_ms']):
                    best = {'scene_key':key,'t_ms':t,'a':eid,'b':fid,'value':value}
    missing = [k for k in ('person','forklift') if k not in seen]
    key = best['scene_key'] if best else scenes[0]['scene_key'] if scenes else 'all'
    t0, t1 = t_range or (min((m['t0_ms'] for m in scenes),default=0),max((m['t1_ms'] for m in scenes),default=0))
    evs = [evidence(store,key,None,best['t_ms'],best['t_ms']+1,'measure','closest_approach',{**best,'t_s':best['t_ms']/1000},
                    params={'t_range':t_range,'zone':zone})] if best else []
    return result(key,t0,t1,{'closest':best,'missing':missing},evidence=evs,pts_read=pts,started=started)


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
    return result(key,t,t+1,{'scene_key':key,'t_ms':t,'side':side,'building':value},cam=cam['id'],evidence=[ev],pts_read=1,started=started)


def speed_limit(store, scene_key, t_ms):
    out = geo.speed_limit(store,scene_key,t_ms)
    if 'speed_kmh' not in out:
        return result(scene_key,t_ms,t_ms+1,out,gaps=[(t_ms,t_ms+1,out['reason'])])
    t = out['t_ms']
    evs = [evidence(store,scene_key,None,t,t+1,'measure','obd_speed',out['speed_kmh'],unit='km/h'),
           evidence(store,scene_key,None,t,t+1,'map','osm_maxspeed',out['value'],unit='km/h')]
    return result(scene_key,t,t+1,out,evidence=evs,pts_read=1)


def speeding_candidates(store, scenes, t_range=None, k=3, passes=None):
    started = time.monotonic()
    best = []
    for m in scenes:
        if not m['cams'] or 'trip' not in m['assets']:
            continue
        points = [f['properties'] for f in store.asset_json(m['scene_key'],m['assets']['trip'])['features'][1:]]
        limited = [p for p in points if p['maxspeed_kmh'] is not None and p['state'] in {'observed','interpolated'}
                   and (not t_range or t_range[0]<=p['t_ms']<t_range[1])
                   and (passes is None or any(q['trip_id']==m['scene_key'] and q['t0_ms']<=p['t_ms']<q['t1_ms'] for q in passes))]
        if limited:
            p = max(limited,key=lambda p:p['speed']*3.6-p['maxspeed_kmh'])
            best.append({'scene_key':m['scene_key'],'t_ms':p['t_ms'],'speed_kmh':p['speed']*3.6,'limit_kmh':p['maxspeed_kmh'],
                         'over_kmh':p['speed']*3.6-p['maxspeed_kmh']})
    best = sorted(best,key=lambda c:-c['over_kmh'])[:k]
    evs = [evidence(store,c['scene_key'],None,c['t_ms'],c['t_ms']+1,'measure','obd_over_limit',c['over_kmh'],unit='km/h') for c in best]
    return result(best[0]['scene_key'] if best else 'all',min((c['t_ms'] for c in best),default=0),max((c['t_ms']+1 for c in best),default=0),
                  {'candidates':best},evidence=evs,pts_read=len(best),started=started)


def events(store, scene_key, t0_ms, t1_ms, zone=None):
    found = store.events_in(scene_key,t0_ms,t1_ms)
    if zone:
        polygon = Polygon(zone)
        found = [e for e in found if any(p.entity_id in e.actor_ids and polygon.covers(Point(p.root_world))
                                        for p in store.moment(scene_key,e.t0_ms).persons)]
    evs = [evidence(store,scene_key,None,e.t0_ms,e.t1_ms,'measure','event_'+e.source,e.model_dump(),
                    params={'event_id':e.event_id}) for e in found]
    return result(scene_key,t0_ms,t1_ms,[e.model_dump() for e in found],evidence=evs,pts_read=len(found))


def cosmos(store, task, scene_key, t_ms, args, *, run=None, circuit=None):
    started = time.monotonic()
    circuit = circuit if circuit is not None else {'failures':0}
    prefix = task=='predict_next'
    m = store.manifest(scene_key)
    t0, t1 = max(0,t_ms-(2000 if prefix else 3000)), t_ms if prefix else min(m['t1_ms'],t_ms+3000)
    try:
        if circuit['failures']>=2:
            raise vlm.VLMUnavailable('circuit_open')
        record = getattr(vlm,task)(scene_key,**args,run=run)
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
    return result(scene_key,t0,t1,{'record':record.model_dump(mode='json')},cam=record.cam_id,evidence=[ev],
                  pts_read=len(record.input_frame_refs),model_rev=record.model_rev,started=started)


def situation(store, scene_key, t_ms, actor_refs, *, run=None, circuit=None):
    return cosmos(store,'situation',scene_key,t_ms,{'t_ms':t_ms,'actor_refs':actor_refs},run=run,circuit=circuit)


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
    return result(req.scene_key,0,0,action.model_dump())
