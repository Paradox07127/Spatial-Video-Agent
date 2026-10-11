from fastapi import APIRouter, Request

from app.core.store import nearest

router = APIRouter()


def period(times, default):
    # The smallest spacing is the nominal sample period; wider spacings are gaps.
    return min((b - a for a, b in zip(times, times[1:]) if b > a), default=default)


@router.get('/api/fleet/trips')
def trips(request: Request):
    store = request.app.state.store
    return [{'trip_id': m['scene_key'], 'title': m['title'], 't0_ms': m['t0_ms'], 't1_ms': m['t1_ms'],
             'n_events': len(store.events_in(m['scene_key'], m['t0_ms'], m['t1_ms'])),
             'geojson_url': store.asset_url(m['scene_key'], m['assets']['trip']), 'full': bool(m['cams'])}
            for m in store.list_scenes('fleet')]


@router.get('/api/fleet/trip/{trip_id}/moment')
def moment(trip_id: str, request: Request, t: int):
    store = request.app.state.store
    m = store.manifest(trip_id)
    if not m['t0_ms'] <= t < m['t1_ms']:
        raise ValueError('Time outside trip')
    if 'ego_dir' not in m['assets']:
        raise FileNotFoundError('Trip has no ego frames')
    frames = store.asset_json(trip_id, f"{m['assets']['ego_dir']}/{t // 10000:05d}.json")['frames']
    times = [f['t_ms'] for f in frames]
    i = nearest(times, t)
    if i is None or abs(times[i] - t) > period(times, 1000 / m['fps']):
        raise FileNotFoundError('No ego frame')
    frame = frames[i]
    gps = store.rows(store.index, 'SELECT t_ms,place_id,state FROM gps WHERE trip_id=? ORDER BY t_ms', [trip_id])
    gps_times = [g['t_ms'] for g in gps]
    j = nearest(gps_times, frame['t_ms'])
    place = None
    if j is not None and abs(gps_times[j] - frame['t_ms']) <= period(gps_times, 0) and gps[j]['state'] == 'observed' and gps[j]['place_id']:
        places = store.rows(store.index, 'SELECT * FROM places WHERE place_id=?', [gps[j]['place_id']])
        if places:
            import json
            place = places[0]
            if place['geom'] is not None:
                place['geom'] = json.loads(place['geom'])
    return {**frame, 'place': place, 'gps_sigma_m': frame['ego']['sigma_m']}


@router.get('/api/fleet/building/{bid}')
def building(bid: str, request: Request):
    store = request.app.state.store
    for m in store.list_scenes('fleet'):
        if 'buildings' not in m['assets']:
            continue
        buildings = store.asset_json(m['scene_key'], m['assets']['buildings'])['buildings']
        if bid in buildings:
            return {**buildings[bid], 'bid': bid, 'facade_note': None}
    raise FileNotFoundError('Building not found')
