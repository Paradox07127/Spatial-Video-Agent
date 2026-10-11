import re
from types import SimpleNamespace

import pytest

from app.api import agent
from app.api.search import SearchRequest, retrieve
from app.core import embed, geo, llm, vlm
from app.tests.unit.a5_fakes import fixture_store, install_trace, install_vlm
from fixtures.gen_fixture import latlon

EMBED_TEXT = embed.embed_text
from pipeline.common.schema_check import check_sse_sequence

ROM_Q = 'Near ROM, did any pedestrian cross in front of the car?'
BUILDING_Q = 'At Queen and Spadina, what is the building on the right?'
# ROM 95 m to the right of the fixture trip at s=10 m: passed at bearing about +90 deg, before the GPS gap
LM_LAT, LM_LON = latlon(10.0, -95.0)
PLACES = [
    {'place_id':'pl_queen_spadina','kind':'junction','name':'Queen St W & Spadina Ave',
     'aliases':['Queen St W and Spadina Ave','Spadina Ave & Queen St W'],'lat':latlon(55.0)[0],'lon':latlon(55.0)[1]},
    {'place_id':'pl_spadina_avenue','kind':'road','name':'Spadina Avenue','aliases':['Spadina Ave','Spadina'],'lat':43.65,'lon':-79.397},
    {'place_id':'pl_queen_street_west','kind':'road','name':'Queen Street West','aliases':['Queen St W','Queen'],'lat':43.649,'lon':-79.4},
    {'place_id':'pl_spadina_avenue_college_street','kind':'junction','name':'Spadina Avenue & College Street',
     'aliases':['Spadina Ave and College St','College St & Spadina Ave'],'lat':43.658,'lon':-79.4},
    {'place_id':'pl_lm_w4942687','kind':'landmark','name':'Royal Ontario Museum','aliases':['ROM'],'lat':LM_LAT,'lon':LM_LON},
]


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = fixture_store(tmp_path)
    install_trace(monkeypatch)
    install_vlm(monkeypatch)
    monkeypatch.setattr(embed, 'embed_text', lambda q: [0.0]*256)
    original = s.rows
    def rows(db, sql, params=()):
        if 'FROM places' in sql:
            return [p for p in PLACES if not params or p['place_id']==params[0]]
        return original(db, sql, params)
    monkeypatch.setattr(s, 'rows', rows)
    return s


def ask(store, q):
    frames = list(agent.ask_frames(store, agent.AskRequest(q=q, context={'workspace':'fleet'})))
    assert check_sse_sequence(frames)==[]
    return frames


def answer_of(frames):
    return next(f['data'] for f in frames if f['event']=='answer')


def calls(frames):
    return [f['data'] for f in frames if f['event']=='tool_call']


def test_place_filter_keeps_clips_overlapping_a_pass(store):
    _, r = retrieve(store, SearchRequest(q=ROM_Q, workspace='fleet'))
    moments = r.envelope.output['moments']
    assert moments and {m['scene_key'] for m in moments}=={'fl-pie-fixture-01'}
    assert [c['clip_id'] for c in r.envelope.output['expanded_ranges']]==['fl-pie-fixture-01:dashcam:00000']
    passes = next(c for c in calls(ask(store, ROM_Q)) if c['name']=='find_passes')
    assert (passes['args']['r_m'], passes['args']['fov_deg'])==(100, 360)


def test_place_pass_bounds_the_evidence_not_only_the_clips(store, monkeypatch):
    original = store.events_in
    # the ROM pass is 0-4.5 s; a crossing at 7 s lies in the overlapping clip's expanded range only
    def events_in(*a):
        return [e.model_copy(update={'t0_ms':7000,'t1_ms':8000}) if e.type=='ped_crossing' else e for e in original(*a)]
    monkeypatch.setattr(store, 'events_in', events_in)
    _, r = retrieve(store, SearchRequest(q=ROM_Q, workspace='fleet'))
    assert r.envelope.output['moments']==[]
    frames=ask(store,ROM_Q)
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert any(p['operator']=='requested_action:crossing' and p['status']=='unknown'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])


def test_place_moments_lie_inside_the_pass(store):
    _, r = retrieve(store, SearchRequest(q='Near ROM, was there any hard braking?', workspace='fleet'))
    assert r.envelope.output['moments']==[]
    _, r = retrieve(store, SearchRequest(q='Near ROM, are there any pedestrians?', workspace='fleet'))
    assert r.envelope.output['moments'] and all(m['t0_ms']<4500 for m in r.envelope.output['moments'])


def test_site_place_search_is_not_limited_to_the_global_vss_top_k(store):
    from app.core.models import MomentCard
    far = MomentCard(moment_id='m_fl-pie-fixture-01_9000',scene_key='fl-pie-fixture-01',t0_ms=9000,t1_ms=14000,cams=['dashcam'],
                     primary_cam='dashcam',actor_refs=[],evidence_ids=[],score=.9,why=['vss'],thumb=None,exec_state='live',backend='vss',
                     clip_id='fl-pie-fixture-01:dashcam:00001')
    # the site index ranks the whole workspace; its top hits lie outside the 0-4.5 s ROM pass
    vss = SimpleNamespace(search=lambda q, k, f: {'moments':[far],'sql_query':None})
    _, r = retrieve(store, SearchRequest(q=ROM_Q, workspace='fleet'), vss=vss)
    assert r.envelope.output['moments'] and all(m['t0_ms']<4500 for m in r.envelope.output['moments'])

def test_place_without_passes_is_out_of_range(store, monkeypatch):
    monkeypatch.setattr(geo, 'find_passes', lambda *a: [])
    _, r = retrieve(store, SearchRequest(q=ROM_Q, workspace='fleet'))
    assert r.envelope.output['abstain']=='OUT_OF_RANGE'
    assert ask(store, ROM_Q)[-2]['data']['final']=='OUT_OF_RANGE'


def test_no_hit_answer_states_the_checked_video(store, monkeypatch):
    original = store.events_in
    monkeypatch.setattr(store, 'events_in', lambda *a: [e for e in original(*a) if e.type!='ped_crossing'])
    frames = ask(store, ROM_Q)
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert any(p['operator']=='requested_action:crossing' and p['status']=='unknown'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])
    answer = answer_of(frames)
    assert answer['text']=='Checked 1 video(s) passing ROM (fl-pie-fixture-01, 0–4.5 s); no evidence that the pedestrian crossed in front of the car. The requested event remains unresolved; this search does not verify absence.', answer['text']
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    assert [(read[r['eid']]['method'],r['field']) for r in answer['measure_refs']]==[
        ('searched_passes','value.count'),('gps_pass','value.t0_s'),('gps_pass','value.t1_s')]
    assert len(answer['cites'])==len(dict.fromkeys(answer['cites']))==2
    seek = [f['data']['args'] for f in frames if f['event']=='ui' and f['data']['cmd']=='seek']
    assert seek==[{'t_ms':0,'scene_key':'fl-pie-fixture-01'}]
    assert agent.trip_name('fl-pie-set03_video_0010')=='set03·0010'


@pytest.mark.parametrize('q,passes,interval', [
    ('Near ROM, from 1 to 3 seconds, did any pedestrian cross in front of the car?', None, '1–3 s'),
    (ROM_Q, [{'trip_id':'fl-pie-fixture-01','t0_ms':8000,'t1_ms':12000}], '8–10 s')])
def test_no_hit_answer_states_the_searched_intervals(store, monkeypatch, q, passes, interval):
    original = store.events_in
    monkeypatch.setattr(store, 'events_in', lambda *a: [e for e in original(*a) if e.type!='ped_crossing'])
    if passes:
        monkeypatch.setattr(geo, 'find_passes', lambda *a: [dict(p) for p in passes])
    frames = ask(store, q)
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert any(p['operator']=='requested_action:crossing' and p['status']=='unknown'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])
    answer = answer_of(frames)
    assert answer['text']==f'Checked 1 video(s) passing ROM (fl-pie-fixture-01, {interval}); no evidence that the pedestrian crossed in front of the car. The requested event remains unresolved; this search does not verify absence.', answer['text']
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    cited = [read[e] for e in answer['cites'] if read[e]['method']=='gps_pass']
    assert [f"{e['value']['t0_s']:g}–{e['value']['t1_s']:g} s" for e in cited]==[interval]


@pytest.mark.parametrize('q,place', [
    ('ROM: did any pedestrian cross in front of the car?', 'ROM'), ('queen and spadina junction: did any pedestrian cross?', 'queen & spadina'),
    ('Was this car speeding?', None), ('Did the OBD speed of P1 and V2 exceed the km/h speed limit?', None), ('Are there any pedestrians in dashcam?', None)])
def test_agent_resolves_place_names_against_the_places_index(store, q, place):
    frames = ask(store, q)
    assert store.get_run(frames[0]['data']['run_id']).steps[0].output['place']==place
    assert [c['args']['name'] for c in calls(frames) if c['name']=='geocode']==([place] if place else [])
    if place:
        assert frames[-2]['data']['final']=='SUPPORTED'


@pytest.mark.parametrize('q,place', [
    (BUILDING_Q, 'Queen & Spadina'), ('Queen St W and Spadina Ave: did any pedestrian cross?', 'Queen St W & Spadina Ave'),
    ('King / Bathurst junction: did any pedestrian cross?', 'King & Bathurst'), ('Queen and Spadina: was there any hard braking?', 'Queen & Spadina'),
    ('That building at Queen & Spadina', 'Queen & Spadina'), (ROM_Q, 'ROM'), ('near Union Station, any brake?', 'Union Station'),
    ('Find P1 and F1 from 2 to 5 seconds', None), ('Link P1 across cameras', None),
    ('queen and spadina junction: did any pedestrian cross?', 'queen & spadina'), ('Did the OBD speed of P1 and V2 exceed the km/h speed limit?', None),
    ('Is there a pedestrian and a car?', None)])
def test_rule_parse_junction_place(q, place):
    assert llm.rule_parse(q, {'workspace':'fleet'}).place==place


@pytest.mark.parametrize('name,place_id', [
    ('Queen and Spadina', 'pl_queen_spadina'), ('Queen & Spadina', 'pl_queen_spadina'),
    ('Spadina Ave and Queen St W', 'pl_queen_spadina'), ('College & Spadina', 'pl_spadina_avenue_college_street'),
    ('ROM', 'pl_lm_w4942687'), ('Spadina Avenue', 'pl_spadina_avenue'), ('King & Bathurst', None),
    ('queen & spadina', 'pl_queen_spadina')])
def test_geocode_two_street_parts_resolve_to_the_junction(name, place_id):
    fake = SimpleNamespace(index=None, rows=lambda db, sql, params=(): PLACES)
    assert (geo.geocode(fake, name) or {}).get('place_id')==place_id


def test_geocode_requires_two_distinct_matching_streets():
    place = dict(place_id='pine_oak', kind='junction', name='Pine Street & Oak Street', aliases=[], lat=0, lon=0)
    fake = SimpleNamespace(index=None, rows=lambda *a: [place])
    assert geo.geocode(fake, 'Elm Street & Oak Street') is None
    assert geo.geocode(fake, 'Pine Street & Pine Street') is None
    for name in ('Oak St and Pine St', 'Oak St / Pine St', 'Oak St at Pine St'):
        assert geo.geocode(fake, name)['place_id']=='pine_oak'
    for suffix, short in [('Avenue','Ave'), ('Road','Rd'), ('Boulevard','Blvd'), ('Drive','Dr'), ('Lane','Ln'), ('Court','Ct')]:
        place['name'] = f'Pine {suffix} & Oak Street'
        assert geo.geocode(fake, f'Oak St & Pine {short}')['score']==1.


def test_geocode_abstains_when_junction_scores_are_close():
    places = [dict(place_id=str(i), kind='junction', name=f'{street} Street & Oak Street', aliases=[], lat=0, lon=0)
              for i, street in enumerate(('Cedar Grove', 'Cedar Groves'))]
    fake = SimpleNamespace(index=None, rows=lambda *a: places)
    assert geo.geocode(fake, 'Cedar Grove St & Oak St') is None


def test_speed_limit_sorts_samples_and_rejects_stale_points():
    samples = [dict(t_ms=t, state='observed', maxspeed_kmh=limit, speed=10.)
               for t, limit in [(1000,50.), (0,40.), (500,30.)]]
    fake = SimpleNamespace(manifest=lambda *a: {'assets':{'trip':'trip.geojson'}},
                           asset_json=lambda *a: {'features':[{'properties':{}}, *[{'properties':s} for s in samples]]})
    assert geo.speed_limit(fake, 'trip', 10000)=={'status':'UNRESOLVED','reason':'missing_speed_limit'}
    assert geo.speed_limit(fake, 'trip', 1250)['value']==50.
    assert geo.speed_limit(fake, 'trip', 1500)['t_ms']==1000
    assert geo.speed_limit(fake, 'trip', 1501)['status']=='UNRESOLVED'
    assert geo.speed_limit(fake, 'trip', -1)['status']=='UNRESOLVED'
    samples[:] = [samples[1]]
    assert geo.speed_limit(fake, 'trip', 10000)['status']=='UNRESOLVED'


def test_building_on_the_right_is_read_from_facades(store, monkeypatch):
    def no_vlm(*a, **kw): raise AssertionError('VLM called')
    monkeypatch.setattr(vlm, 'situation', no_vlm)
    monkeypatch.setattr(llm, 'complete_json', no_vlm)
    frames = ask(store, BUILDING_Q)
    assert 'situation' not in [c['name'] for c in calls(frames)] and 'search' not in [c['name'] for c in calls(frames)]
    assert frames[-2]['data']['final']=='SUPPORTED'
    answer = answer_of(frames)
    assert answer['text']==('The mapped facade on the right at Queen & Spadina has building name Fixture Block B. '
                            'This map association does not verify the camera-visible building name or sign.'), answer['text']
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    assert [(read[e]['kind'],read[e]['method'],read[e]['value']['bid']) for e in answer['cites']]==[('map','osm_facade','w1002')]
    ui = [f['data'] for f in frames if f['event']=='ui']
    assert ui[0]['cmd']=='seek' and ui[0]['args']['scene_key']=='fl-pie-fixture-01' and abs(ui[0]['args']['t_ms']-9550)<=300
    assert (ui[1]['cmd'],ui[1]['args'])==('highlight',{'ids':['w1002'],'mode':'outline'})


def test_building_reads_only_video_passes_inside_the_asked_time(store):
    frames = ask(store, 'At Queen and Spadina, from 0 to 4 seconds, what is the building on the right?')
    [facade] = [c for c in calls(frames) if c['name']=='facade']
    assert [(p['trip_id'],p['t0_ms'],p['t1_ms']) for p in facade['args']['passes']]==[('fl-pie-fixture-01',0,4000)]
    assert all(f['data']['args']['t_ms']<4000 for f in frames if f['event']=='ui' and f['data']['cmd']=='seek')
    frames = ask(store, 'At Queen and Spadina, from 5 to 6 seconds, what is the building on the right?')
    assert 'facade' not in [c['name'] for c in calls(frames)]
    assert (frames[-2]['data']['final'],frames[-2]['data']['stop_reason'])==('OUT_OF_RANGE','abstained')


def test_building_hit_is_the_facade_moment(store):
    frames = ask(store, BUILDING_Q)
    events = [f['event'] for f in frames]
    assert events.count('hits')==1 and events.index('hits')<events.index('claim')
    [moment] = next(f['data']['moments'] for f in frames if f['event']=='hits')
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    seek = next(f['data']['args'] for f in frames if f['event']=='ui' and f['data']['cmd']=='seek')
    assert (moment['scene_key'],moment['t0_ms'],moment['cams'],moment['primary_cam'],moment['why'])==(
        'fl-pie-fixture-01',seek['t_ms'],['dashcam'],'dashcam',['osm_facade'])
    assert [read[e]['method'] for e in moment['evidence_ids']]==['osm_facade']
    assert moment['thumb']==f"/api/assets/fl-pie-fixture-01/thumbs/dashcam/{seek['t_ms']//1000:05d}.jpg"
    assert [m.moment_id for m in store.get_run(frames[0]['data']['run_id']).hits]==[moment['moment_id']]


def test_speeding_near_a_place_only_checks_points_inside_its_passes(store, monkeypatch):
    original = store.asset_json
    # ROM is passed during 0-4.5 s; 9.5 s is outside that pass and over its limit by more
    limits = {2000:20., 9500:1.}
    def asset_json(scene_key, relpath):
        doc = original(scene_key, relpath)
        if relpath=='trip.geojson':
            for f in doc['features'][1:]:
                f['properties']['maxspeed_kmh'] = limits.get(f['properties']['t_ms']) if scene_key=='fl-pie-fixture-01' else None
        return doc
    monkeypatch.setattr(store, 'asset_json', asset_json)
    frames = ask(store, 'Was the car speeding near ROM?')
    assert [c['args']['t_ms'] for c in calls(frames) if c['name']=='speed_limit']==[2000]
    assert frames[-2]['data']['final']=='SUPPORTED'


def test_building_label_falls_back_to_poi_then_address(store, monkeypatch):
    original = store.asset_json
    def unnamed(pois, addr):
        def asset_json(key, relpath):
            doc = original(key, relpath)
            if relpath=='buildings.json':
                for b in doc['buildings'].values():
                    b.update(name=None, pois=pois, addr=addr)
            return doc
        return asset_json
    monkeypatch.setattr(store, 'asset_json', unnamed([{'name':'Fixture Cafe','kind':'cafe'}], '388 Queen St W'))
    frames = ask(store, BUILDING_Q)
    assert answer_of(frames)['text']==('The mapped facade on the right at Queen & Spadina has POI label Fixture Cafe. '
                                       'This map association does not verify the camera-visible building name or sign.')
    mapped = next(e for f in frames if f['event']=='tool_result' for e in f['data']['evidence'] if e['method']=='osm_facade')
    assert mapped['kind']=='map' and mapped['value']['name'] is None
    assert mapped['value']['pois']==[{'name':'Fixture Cafe','kind':'cafe'}] and mapped['value']['addr']=='388 Queen St W'
    monkeypatch.setattr(store, 'asset_json', unnamed([], '388 Queen St W'))
    frames = ask(store, BUILDING_Q)
    assert answer_of(frames)['text']==('The mapped facade on the right at Queen & Spadina has address 388 Queen St W. '
                                       'This map association does not verify the camera-visible building name or sign.')
    mapped = next(e for f in frames if f['event']=='tool_result' for e in f['data']['evidence'] if e['method']=='osm_facade')
    assert mapped['kind']=='map' and mapped['value']['name'] is None
    assert mapped['value']['pois']==[] and mapped['value']['addr']=='388 Queen St W'


def test_building_without_a_facade_on_that_side_is_unresolved(store):
    frames = ask(store, 'At Queen and Spadina, what is the building on the left?')
    assert frames[-2]['data']['final']=='UNRESOLVED'
    text = answer_of(frames)['text']
    assert 'on the left' in text and 'uncited number' not in text and re.search('No', text)


def test_an_event_that_began_before_the_window_still_counts(store):
    # the fixture crossing runs 3-8 s; asking about 4-4.5 s inside the 0-4.5 s ROM pass must find it
    _, r = retrieve(store, SearchRequest(q='Near ROM, from 4 to 4.5 seconds, did any pedestrian cross in front of the car?', workspace='fleet'))
    moments = r.envelope.output['moments']
    assert moments and all(4000<=m['t0_ms']<4500 for m in moments), moments


def test_search_without_an_embedding_service_is_unranked(store, monkeypatch):
    monkeypatch.setattr(embed, 'embed_text', EMBED_TEXT)
    monkeypatch.delenv('EMBED_URL', raising=False)
    _, r = retrieve(store, SearchRequest(q='Near ROM, are there any pedestrians?', workspace='fleet'))
    assert r.envelope.output['moments']
