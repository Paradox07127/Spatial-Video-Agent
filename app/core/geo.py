"""Places and trip evidence; D2 right/forward becomes ego forward/left."""
from __future__ import annotations

import math
import re

from rapidfuzz.fuzz import WRatio, partial_ratio

NEAR_M = 100  # "near" a place: any bearing within this radius
JOIN = r'\s*(?:&|/|\band\b)\s*'


def d2_to_ego(right, forward):
    return forward, -right


def geocode(store, name):
    places = store.rows(store.index, 'SELECT place_id, kind, name, aliases, lat, lon FROM places')
    parts = [p.casefold() for p in re.split(JOIN, name, flags=re.I) if p]
    if len(parts) == 2:
        # a junction name holding both streets; the whole-name ratio only breaks ties
        scored = [(max((min(partial_ratio(a, n.casefold()) for a in parts), WRatio(name.casefold(), n.casefold()))
                       for n in [p['name'], *(p['aliases'] or [])]), p) for p in places if p['name'] and p['kind'] == 'junction']
    else:
        scored = [((max(WRatio(name.casefold(), n.casefold()) for n in [p['name'], *(p['aliases'] or [])]),), p) for p in places if p['name']]
    if not scored:
        return None
    (score, *_), place = max(scored, key=lambda p: p[0])
    if score < 80:
        return None
    return {k:place[k] for k in ['place_id','lat','lon','kind']} | {'score': score/100}


def find_passes(store, place_id, r_m, fov_deg):
    places = store.rows(store.index, 'SELECT lat, lon FROM places WHERE place_id=?', [place_id])
    if not places:
        return []
    place = places[0]
    points = store.rows(store.index, 'SELECT * FROM gps ORDER BY trip_id,t_ms')
    passes = []
    for p in points:
        if p['state'] not in {'observed','interpolated'}:
            continue
        east = math.radians(place['lon']-p['lon'])*6371000*math.cos(math.radians(p['lat']))
        north = math.radians(place['lat']-p['lat'])*6371000
        relative = (math.degrees(math.atan2(east,north))-p['heading']+180)%360-180
        if math.hypot(east,north)>r_m or abs(relative)>fov_deg/2:
            continue
        if passes and passes[-1]['trip_id']==p['trip_id'] and p['t_ms']<=passes[-1]['t1_ms']:
            passes[-1]['t1_ms']=p['t_ms']+500
        else:
            passes.append({'trip_id':p['trip_id'],'t0_ms':p['t_ms'],'t1_ms':p['t_ms']+500})
    return passes


def trim_passes(store, passes, t_range=None):
    """Passes clipped to their trip's time span and to t_range; empty ones are dropped."""
    trimmed = []
    for p in passes:
        m = store.manifest(p['trip_id'])
        t0, t1 = max(p['t0_ms'], m['t0_ms']), min(p['t1_ms'], m['t1_ms'])
        if t_range:
            t0, t1 = max(t0, t_range[0]), min(t1, t_range[1])
        if t0 < t1:
            trimmed.append({**p, 't0_ms':t0, 't1_ms':t1})
    return trimmed


def named_place(store, q):
    """A standalone name in q that the places index matches almost exactly; ids such as P1 or ceiling_00 never qualify."""
    for name in re.findall(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z.']*(?:\s+[A-Za-z][A-Za-z.']*)*(?![A-Za-z0-9_])", q):
        place = geocode(store, name)
        if place and place['score'] >= .95:
            return name
    return None


def ego_frame(store, scene_key, t_ms):
    manifest = store.manifest(scene_key)
    chunk = f"{t_ms//10000:05d}.json"
    frames = store.asset_json(scene_key, manifest['assets']['ego_dir']+'/'+chunk)['frames']
    return next((f for f in frames if f['t_ms']==t_ms), None)


def closest_frame(store, passes, lat, lon):
    """(scene_key, ego frame) with a GPS fix nearest to lat/lon inside the passes."""
    best = None
    for p in passes:
        m = store.manifest(p['trip_id'])
        t0, t1 = max(p['t0_ms'], m['t0_ms']), min(p['t1_ms'], m['t1_ms'])
        for chunk in range(t0//10000, (t1-1)//10000+1):
            for f in store.asset_json(p['trip_id'], f"{m['assets']['ego_dir']}/{chunk:05d}.json")['frames']:
                if not t0<=f['t_ms']<t1 or f['ego']['state'] not in {'observed','interpolated'}:
                    continue
                east = math.radians(lon-f['ego']['lon'])*6371000*math.cos(math.radians(f['ego']['lat']))
                north = math.radians(lat-f['ego']['lat'])*6371000
                if best is None or math.hypot(east,north)<best[0]:
                    best = (math.hypot(east,north), p['trip_id'], f)
    return best[1:] if best else (None, None)


def facades(store, scene_key, t_ms):
    frame = ego_frame(store, scene_key, t_ms)
    if frame is None:
        return []
    buildings = store.asset_json(scene_key, store.manifest(scene_key)['assets']['buildings'])['buildings']
    return [{**buildings[b['bid']], **b} for b in frame['buildings']]


def speed_limit(store, scene_key, t_ms):
    trip = store.asset_json(scene_key, store.manifest(scene_key)['assets']['trip'])
    samples = [f['properties'] for f in trip['features'][1:] if f['properties']['t_ms']<=t_ms]
    point = samples[-1] if samples else None
    if point is None or point['state'] not in {'observed','interpolated'}:
        return {'status':'UNRESOLVED','reason':'missing_speed_limit'}
    status = {'status':'SUPPORTED'} if point['maxspeed_kmh'] is not None else {'status':'UNRESOLVED','reason':'missing_speed_limit'}
    return {**status,'value':point['maxspeed_kmh'],'speed_kmh':point['speed']*3.6,'unit':'km/h','t_ms':point['t_ms']}
