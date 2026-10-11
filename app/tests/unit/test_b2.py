import os
import io
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app.api import agent, scenes, search
from app.core import live_measure, street, tools, vlm
from app.core.models import EntityRef, Moment, PersonAtMoment
from app.core.store import Store
from app.tests.unit.a5_fakes import L1_CAM, L1_KEY, fixture_l1_store, fixture_store
from app.tests.unit.test_agent import ask, corpus, of, setup
from app.tests.unit.test_live_measure import MANIFEST, ROW, SIDECAR, store_with
from app.tests.unit.test_street import s3_with, sidecar
from pipeline.common.schema_check import check_sse_sequence

WH, FL = 'wh-sdg-fixture-01', 'fl-pie-fixture-01'


def test_b2_sources_filter_before_ranking_and_level_quota(corpus, monkeypatch):
    manifest, clips = corpus.manifest, corpus.search_clips
    monkeypatch.setattr(corpus,'manifest',lambda k: {**manifest(k),'metric':False} if k==WH else manifest(k))
    monkeypatch.setattr(corpus,'list_scenes',lambda workspace=None: [corpus.manifest(k) for k in (WH,FL,L1_KEY)
                                                                  if workspace is None or corpus.manifest(k)['workspace']==workspace])
    def ranked(filters, *a):
        found=clips(filters,*a)
        for c in found:
            c.rank_text=50 if c.scene_key==WH else 1
        return found
    monkeypatch.setattr(corpus,'search_clips',ranked)
    monkeypatch.setattr(search,'LEVEL_QUOTA',1)
    frames=ask(corpus,'Find a person',{'workspace':'warehouse','scope':'corpus','sources':['warehouse']})
    assert [m['scene_key'] for h in of(frames,'hits') for m in h['moments']]==[WH]
    filters=next(f['args']['filters'] for f in of(frames,'tool_call') if f['name']=='search')
    assert filters['sources']==['warehouse']


@pytest.mark.parametrize('workspace,key,q,reason',[('street',L1_KEY,'Where is the person?','no_geometry'),
                                                  ('fleet','fl-pie-fixture-02','Where is the car?',None)])
def test_b2_location_followup_dispatches_before_geometry(corpus, workspace, key, q, reason):
    frames=ask(corpus,q,{'workspace':workspace,'scene_key':key,'t_ms':1000})
    assert of(frames,'status')[-1]['stop_reason']!='error'
    assert not any(f['name']=='get_synced_views' for f in of(frames,'tool_call'))
    if reason:
        assert of(frames,'claim')[-1]['explain']['abstain']['reason']==reason
    else:
        assert of(frames,'status')[-1]['final']=='SUPPORTED'
        assert any(e['kind']=='map' for r in of(frames,'tool_result') for e in r['evidence'])


def test_b2_visibility_optional_and_distance_continues(setup, monkeypatch):
    _,s=setup
    original=s.manifest
    monkeypatch.setattr(s,'manifest',lambda k: {**original(k),'assets':{a:v for a,v in original(k)['assets'].items() if a!='visibility'}})
    monkeypatch.setattr(s,'moment',lambda k,t: Store.moment(s,k,t))
    r=tools.visibility(s,WH,4600,'P1')
    assert r.envelope.output['reason']=='no_visibility'
    a=EntityRef(scene_key=WH,entity_id='P1',kind='person')
    b=EntityRef(scene_key=WH,entity_id='F1',kind='forklift')
    assert tools.measure(s,WH,4600,a,b,'distance',cam='ceiling_04').evidence


def test_b2_nonmetric_closest_scan_and_live_measure(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    m={**s.manifest(WH),'metric':False}
    r=tools.closest_approach(s,[m])
    assert r.envelope.output['closest'] is None and not r.evidence
    assert r.envelope.output['reason']=='scene_not_metric'
    assert r.envelope.output['scene_coverage'][WH]['reason']=='scene_not_metric'
    out=live_measure.measure(store_with(SIDECAR,{**MANIFEST,'metric':False}),'wh-test','cam_a',20000,20200,rows=[ROW])
    assert out['value_m'] is None and out['reason']=='scene_not_metric' and out['n_frames']==0


def test_b2_street_requested_range_bounds_cards_detections_and_localization(corpus):
    frames=ask(corpus,'Find a person and car',{'workspace':'street','scene_key':L1_KEY,'t_range':[0,1000]})
    cards=[m for h in of(frames,'hits') for m in h['moments']]
    assert cards and all((m['t0_ms'],m['t1_ms'])==(0,1000) for m in cards)
    for r in of(frames,'tool_result'):
        for e in r['evidence']:
            assert 0<=e['t0_ms']<=e['t1_ms']<=1000
            if e['method']=='yolo_coco_2d':
                assert not e['value']['counts'].get('car')
    loc=next(c['args'] for c in of(frames,'tool_call') if c['name']=='localize')
    assert (loc['t0_ms'],loc['t1_ms'])==(0,1000)


@pytest.mark.parametrize('mode',['unrelated','distant','before_event'])
def test_b2_event_timestamp_requires_participant_in_event_window(tmp_path, monkeypatch, mode):
    s=fixture_store(tmp_path)
    original=s.asset_json
    def doc(k,p):
        d=original(k,p)
        if p=='events.json' and mode=='distant':
            for e in d['events']:
                if e['type']=='dodge':
                    e['t1_ms']=9000
        if p.startswith('boxes/'):
            entities=s.entities(k)
            entity=next(e for e in entities if e.entity_id==('P2' if mode=='unrelated' else 'P1'))
            t=4600 if mode=='unrelated' else 8000 if mode=='distant' else 4500
            obs=next(o for o in entity.observations if o['cam_id']==p.split('/')[1] and o['t0_ms']<=t<o['t1_ms'])
            return {'rows':[[t,obs['local_track_id'],'person',0,0,10,10,.9]]}
        return d
    monkeypatch.setattr(s,'asset_json',doc)
    _,r=search.retrieve(s,search.SearchRequest(q='When did the worker dodge the forklift?',workspace='warehouse',scene_key=WH))
    assert r.envelope.output['moments']==[]
    assert any(g[2]=='no_participant_sample' for g in r.envelope.gaps)


def test_b2_zone_filter_binds_event_participants(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.moment
    def moment(k,t):
        m=original(k,t)
        return m.model_copy(update={'persons':[p.model_copy(update={'root_world':(20.,20.) if p.entity_id=='P1' else (0.,0.)}) for p in m.persons],
                                    'forklift':[f.model_copy(update={'x':20.,'y':20.}) for f in m.forklift]})
    monkeypatch.setattr(s,'moment',moment)
    _,r=search.retrieve(s,search.SearchRequest(q='When did the worker dodge the forklift?',workspace='warehouse',scene_key=WH,
                                             zone=[(-1,-1),(1,-1),(1,1),(-1,1)]))
    assert r.envelope.output['moments']==[]


def test_b2_primary_camera_comes_from_requested_observation(tmp_path):
    s=fixture_store(tmp_path)
    _,r=search.retrieve(s,search.SearchRequest(q='Find P1 in eye_00',workspace='warehouse',scene_key=WH))
    assert r.envelope.output['moments']
    assert all(m['primary_cam']=='eye_00' and m['cams']==['eye_00'] for m in r.envelope.output['moments'])
    assert all(e.cam_id in {None,'eye_00'} for e in r.evidence)


def test_b2_missing_thumbnails_are_null_with_gaps(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.manifest
    def manifest(k):
        m=original(k)
        return {**m,'cams':[{**c,'thumbs':None if i==0 else 'alternate'} for i,c in enumerate(m['cams'])]}
    monkeypatch.setattr(s,'manifest',manifest)
    r=tools.get_synced_views(s,WH,4600)
    assert r.envelope.output['cams'] and all(c['frame_url'] is None for c in r.envelope.output['cams'])
    assert any(g[2]=='missing_thumbnail' for g in r.envelope.gaps)
    _,r=search.retrieve(s,search.SearchRequest(q='Find a worker',workspace='warehouse',scene_key=WH))
    assert all(m.get('thumb') is None for m in r.envelope.output['moments'])
    assert any(g[2]=='missing_thumbnail' for g in r.envelope.gaps)


@pytest.mark.parametrize('artifact',['sidecar','chunk','thumb'])
def test_b2_concurrent_street_cache_publication(tmp_path, monkeypatch, artifact):
    s=fixture_l1_store(tmp_path,site=True)
    source='s3://bucket/segment.mp4'
    gate=Barrier(2)
    paths=[]
    replace, path_replace=os.replace,Path.replace
    def publish(src,dst):
        paths.append(str(src)); gate.wait(timeout=10)
        return replace(src,dst)
    def old_publish(src,dst):
        paths.append(str(src)); gate.wait(timeout=10)
        return path_replace(src,dst)
    monkeypatch.setattr(os,'replace',publish)
    monkeypatch.setattr(Path,'replace',old_publish)
    if artifact=='sidecar':
        s.s3=lambda:SimpleNamespace()
        monkeypatch.setattr(live_measure,'fetch_sidecar',lambda *a: sidecar(50))
        fn=lambda: street._sidecar(s,source)
    elif artifact=='chunk':
        s.s3=lambda: SimpleNamespace(get_object=lambda **k: {'Body':io.BytesIO(b'video')})
        fn=lambda: street.local_chunk(s,L1_KEY,{'uri':source})
    else:
        monkeypatch.setattr(street,'local_chunk',lambda *a: Path('video'))
        monkeypatch.setattr(street.cv2,'VideoCapture',lambda *a: SimpleNamespace(set=lambda *a:None,release=lambda:None,
                                                                             read=lambda:(True,np.zeros((360,640,3),np.uint8))))
        fn=lambda: street.thumb(s,L1_KEY,L1_CAM,0)
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(fn) for _ in range(2)]
        results=[f.result() for f in futures]
    assert len(set(paths))==2 and results[0]==results[1]
    assert not any(Path(p).exists() for p in paths)


def test_b2_vlm_cache_publishes_atomically(tmp_path, monkeypatch):
    from app.tests.unit.test_vlm import _published_c1
    from app.core.models import CosmosRecord
    record=CosmosRecord.model_validate(_published_c1(4600))
    monkeypatch.setattr(vlm,'_cache_dir',lambda:tmp_path/'cache')
    monkeypatch.setattr(vlm,'_cached',lambda *a:None)
    monkeypatch.setattr(vlm,'_live',lambda *a:record)
    monkeypatch.setattr(vlm,'_cache_identity',lambda key:{'model':record.model_rev,'endpoint':'fixture','source':'fixture'})
    monkeypatch.setattr(vlm,'_rev',lambda *a:'test')
    writes=[]
    original=Path.write_text
    def write(p,*a,**k):
        writes.append(p)
        assert p.parent.name!='cache' or p.suffix!='.json', 'published cache written directly'
        return original(p,*a,**k)
    monkeypatch.setattr(Path,'write_text',write)
    r=vlm._call('C1',WH,None,4600,[],(1600,7600),[],'',{},None,None)
    assert r==record and len(list((tmp_path/'cache').glob('*.json')))==1


@pytest.mark.parametrize('mode',['unavailable','unindexed','empty_frames','partial_frames'])
def test_b2_detection_coverage_clips_gaps_and_counts_available_frames(tmp_path, monkeypatch, mode):
    s=fixture_l1_store(tmp_path,site=True)
    s.s3=s3_with(lambda key:None,[])
    if mode=='unindexed':
        monkeypatch.setattr(s,'search_clips',lambda *a:[])
    elif mode in {'empty_frames','partial_frames'}:
        raw=sidecar(50)
        if mode=='empty_frames':
            raw['frames']=[]
        else:
            raw['frames']=raw['frames'][:2]
        monkeypatch.setattr(live_measure,'fetch_sidecar',lambda *a:raw)
    r=tools.detections_2d(s,L1_KEY,L1_CAM,0,500,{'person':['person']})
    assert r.envelope.coverage==(0.4 if mode=='partial_frames' else 0)
    assert r.envelope.gaps and all(0<=a<b<=500 for a,b,_ in r.envelope.gaps)


@pytest.mark.parametrize('q,selection',[('Link P999 between cameras',[]),('Link the person between cameras',['P999'])])
def test_b2_nonexistent_entity_cannot_support_link(setup, monkeypatch, q, selection):
    _,s=setup
    monkeypatch.setattr(s,'events_in',lambda *a:[])
    frames=ask(s,q,{'workspace':'warehouse','scene_key':WH,'selection':selection})
    assert of(frames,'status')[-1]['final'] in {'UNRESOLVED','NO_MATCH'}
    assert all(p['status']!='supported' for c in of(frames,'claim') for p in c['predicates'])


def test_b2_partial_speed_coverage_cannot_negate(setup, monkeypatch):
    from app.tests.unit.test_agent import set_limits
    _,s=setup
    set_limits(s,monkeypatch,lambda k,t:40. if t<2000 else None)
    frames=ask(s,'Was this car speeding?',{'workspace':'fleet','scene_key':FL,'t_range':[0,4000]})
    assert of(frames,'status')[-1]['final']=='UNRESOLVED'
    scan=next(r['envelope'] for c,r in zip(of(frames,'tool_call'),of(frames,'tool_result')) if c['name']=='speeding_candidates')
    assert scan['coverage']<1 and scan['gaps']
    assert any('not checked' in a['text'] for a in of(frames,'answer'))


def test_b2_camera_free_trip_speed_evidence(corpus, monkeypatch):
    from app.tests.unit.test_agent import set_limits
    key='fl-pie-fixture-02'
    set_limits(corpus,monkeypatch,lambda k,t:1. if k==key else None)
    frames=ask(corpus,'Was this car speeding?',{'workspace':'fleet','scene_key':key})
    assert check_sse_sequence(frames)==[]
    assert of(frames,'status')[-1]['final']=='SUPPORTED'
    card=of(frames,'hits')[0]['moments'][0]
    assert card['scene_key']==key and card['cams']==[] and card['primary_cam']=='' and card['thumb'] is None
    assert not any(c['name']=='get_synced_views' for c in of(frames,'tool_call'))


def test_b2_speed_sigma_uses_time_separation(tmp_path):
    s=fixture_store(tmp_path)
    a=EntityRef(scene_key=WH,entity_id='F1',kind='forklift')
    r=tools.measure(s,WH,4600,a,None,'speed')
    track=s.asset_json(WH,'forklift.json')['forklifts'][0]['track']
    i=next(i for i,p in enumerate(track) if p['t_ms']==4600)
    left,right=track[i-1],track[i+1]
    expected=np.hypot(left['sigma_m'],right['sigma_m'])/((right['t_ms']-left['t_ms'])/1000)
    assert r.evidence[0].sigma==pytest.approx(expected)


def test_b2_blind_intervals_split_at_visible_samples(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=tools.geometry.floor_visibility
    monkeypatch.setattr(tools.geometry,'floor_visibility',lambda d,t,p: {**original(d,t,p),'ceiling_00':'visible'} if 4800<=t<5100 else original(d,t,p))
    r=tools.blind_presence(s,WH,'ceiling_00',0,10000)
    assert [(e.t0_ms,e.t1_ms) for e in r.evidence if e.value['entity_id']=='P1']==[(4000,4800),(5100,6000)]


@pytest.mark.parametrize('entry',['tools','zone'])
def test_b2_zone_event_positions_stay_inside_requested_interval(tmp_path, monkeypatch, entry):
    s=fixture_store(tmp_path)
    event=s.events_in(WH,4600,5000)[0].model_copy(update={'t0_ms':1000,'t1_ms':6000,'params':{'t_min_ms':1000}})
    monkeypatch.setattr(s,'events_in',lambda *a:[event])
    def moment(k,t):
        p=PersonAtMoment(entity_id='P1',root_world=(0.,0.) if t<4000 else (20.,20.),root_method='tri',sigma_m=.1,seen_by=[],observed_in=[])
        return Moment(t_ms=t,persons=[p],forklift=[],cams=[],floor_visibility_ref=None)
    monkeypatch.setattr(s,'moment',moment)
    monkeypatch.setattr(scenes,'actor_points',lambda s,k,t,ids,t_range=None:[(0.,0.)] if t<4000 else [(20.,20.)])
    polygon=[(-1,-1),(1,-1),(1,1),(-1,1)]
    if entry=='tools':
        assert tools.events(s,WH,4500,5000,polygon).envelope.output==[]
    else:
        r=scenes.zone(WH,scenes.ZoneRequest(polygon_world=polygon,t_range=(4500,5000)),SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=s))))
        assert r['events_in_zone']==[]


@pytest.mark.parametrize('bad',[{'t_ms':'1000'},{'t_ms':True},{'t_range':[1000,0]},{'t_range':[0,'1000']},
                                {'t_range':[0,1,2]},{'selection':'P1'},{'selection':[1]}, {'zone':[[0,0],[1,1]]},
                                {'zone':[[0,0],[1],[2,2]]},{'scope':'all'},{'sources':['street']},
                                {'scene_key':FL},{'scene_key':'missing'}])
def test_b2_invalid_context_rejected_before_run(setup, bad):
    app,s=setup
    with TestClient(app) as c:
        r=c.post('/api/agent/ask',json={'q':'Find a worker','context':{'workspace':'warehouse',**bad}})
    assert r.status_code==422 and not s.runs



def test_b2_corpus_context_without_workspace_runs(setup):
    # the home page asks over every source and has no workspace
    app,s=setup
    with TestClient(app) as c:
        r=c.post('/api/agent/ask',json={'q':'When does a forklift pass?','context':{'scope':'corpus'}})
        bad=c.post('/api/agent/ask',json={'q':'Find a worker','context':{'scope':'scene'}})
    assert r.status_code==200 and 'event: status' in r.text and s.runs
    assert bad.status_code==422

def test_b2_integer_fisheye_distortion_projects_zone(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.manifest
    def manifest(k):
        m=original(k)
        cam={**m['cams'][0],'model':'fisheye','dist':[0,0,0,0]}
        return {**m,'cams':[cam]}
    monkeypatch.setattr(s,'manifest',manifest)
    monkeypatch.setattr(s,'events_in',lambda *a:[])
    original_undistort=scenes.cv2.fisheye.undistortPoints
    def undistort(coords,K,dist):
        assert np.issubdtype(dist.dtype,np.floating), 'OpenCV distortion coefficients must be floating point'
        return original_undistort(coords,K,dist)
    monkeypatch.setattr(scenes.cv2.fisheye,'undistortPoints',undistort)
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=s)))
    r=scenes.zone(WH,scenes.ZoneRequest(cam='ceiling_00',polygon_px=[(280,200),(320,200),(300,240)]),request)
    assert len(r['polygon_world'])==3 and np.isfinite(r['polygon_world']).all()


def test_b2_link_requires_requested_camera_observations(setup, monkeypatch):
    _,s=setup
    original=s.entities
    def entities(k):
        return [e.model_copy(update={'observations':[o for o in e.observations if o['cam_id']!='eye_00']})
                if e.entity_id=='P1' else e for e in original(k)]
    monkeypatch.setattr(s,'entities',entities)
    frames=ask(s,'Link P1 between ceiling_04 and eye_00',{'workspace':'warehouse','scene_key':WH})
    assert of(frames,'status')[-1]['stop_reason']!='error'
    assert of(frames,'status')[-1]['final']!='SUPPORTED'


@pytest.mark.parametrize('q,workspace,key',[('Was this car speeding?','fleet',FL),
    ('At Queen and Spadina, what is the building on the right?','fleet',FL),
    ('What was the distance when the forklift and worker were closest?','warehouse',WH),
    ('Is there anyone in the ceiling_00 blind spot?','warehouse',WH)])
def test_b2_special_cards_do_not_fabricate_thumbnail_urls(setup, monkeypatch, q, workspace, key):
    _,s=setup
    original=s.manifest
    monkeypatch.setattr(s,'manifest',lambda k: {**original(k),'cams':[{**c,'thumbs':None} for c in original(k)['cams']]})
    frames=ask(s,q,{'workspace':workspace,'scene_key':key})
    cards=[m for h in of(frames,'hits') for m in h['moments']]
    assert cards and all('thumb' in m and m['thumb'] is None for m in cards)
    assert any(g[2]=='missing_thumbnail' for r in of(frames,'tool_result') for g in r['envelope']['gaps'])



def test_b2_speeding_preset_names_gps_gap_and_reports_covered_maximum(setup):
    _,s=setup
    frames=ask(s,'Was this car speeding?',{'workspace':'fleet','scene_key':FL})
    assert of(frames,'status')[-1]['final']=='UNRESOLVED'
    claim=of(frames,'claim')[-1]
    assert '4.0-7.0 s (GPS gap)' in claim['explain']['note']
    answer=of(frames,'answer')[0]
    assert 'maximum' in answer['text'].lower() and '28.8 km/h' in answer['text'] and '40 km/h' in answer['text']
    evidence={e['eid']:e for r in of(frames,'tool_result') for e in r['evidence']}
    assert {evidence[r['eid']]['method'] for r in answer['measure_refs']}=={'obd_speed','osm_maxspeed'}
    scan=next(r['envelope'] for c,r in zip(of(frames,'tool_call'),of(frames,'tool_result')) if c['name']=='speeding_candidates')
    assert scan['output']['scene_coverage'][FL]['gaps']==[[4000,7000,'no_gps']]


def test_b2_partial_speed_answer_uses_maximum_speed_with_its_own_limit(setup, monkeypatch):
    _,s=setup
    original=s.asset_json
    def asset(k,path):
        d=original(k,path)
        if path=='trip.geojson':
            for f in d['features'][1:]:
                p=f['properties']
                p['speed'],p['maxspeed_kmh']={0:(1.,100.),500:(8.,200.),1000:(2.,20.)}.get(p['t_ms'],(2.,None))
        return d
    monkeypatch.setattr(s,'asset_json',asset)
    frames=ask(s,'Was this car speeding?',{'workspace':'fleet','scene_key':FL,'t_range':[0,4000]})
    assert of(frames,'status')[-1]['final']=='UNRESOLVED'
    answer=of(frames,'answer')[0]
    assert '28.8 km/h' in answer['text'] and '200 km/h' in answer['text']


def test_b2_speeding_sample_before_gps_gap_remains_evidence(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.asset_json
    def asset(k,path):
        d=original(k,path)
        if path=='trip.geojson':
            for f in d['features'][1:]:
                p=f['properties']
                p['speed']=20. if p['t_ms']==4000 else 1.
        return d
    monkeypatch.setattr(s,'asset_json',asset)
    r=tools.speeding_candidates(s,[s.manifest(FL)])
    assert r.envelope.output['candidates'][0]['over_kmh']==32.
    assert r.envelope.output['scene_coverage'][FL]['gaps']==[[4000,7000,'no_gps']]


def test_b2_link_requires_actual_boxes_in_requested_cameras(setup, monkeypatch):
    _,s=setup
    original=s.asset_json
    def asset(k,path):
        d=original(k,path)
        if path.startswith('boxes/eye_00/'):
            d['rows']=[r for r in d['rows'] if r[1] not in {11,14}]
        return d
    monkeypatch.setattr(s,'asset_json',asset)
    frames=ask(s,'Link P1 between ceiling_04 and eye_00',{'workspace':'warehouse','scene_key':WH})
    assert of(frames,'status')[-1]['final']!='SUPPORTED' and of(frames,'status')[-1]['stop_reason']!='error'


@pytest.mark.parametrize('q',['Link P1 between ceiling_04 and eye_00',
                             f'Link P1 between clips {WH}:ceiling_04:00000 and {WH}:eye_00:00000'])
def test_b2_verified_link_is_supported(setup, q):
    _,s=setup
    frames=ask(s,q,{'workspace':'warehouse','scene_key':WH})
    assert of(frames,'status')[-1]['final']=='SUPPORTED'


def test_b2_link_requires_requested_clip_observations(setup):
    _,s=setup
    frames=ask(s,f'Link P1 between clips {WH}:ceiling_04:00000 and {WH}:eye_00:00099',{'workspace':'warehouse','scene_key':WH})
    assert of(frames,'status')[-1]['final']=='UNRESOLVED'


@pytest.mark.parametrize('q,workspace,expected',[
    ('When did the worker start to dodge the forklift, and how far was it?','warehouse','SUPPORTED'),
    ('How close did the forklift come to a person?','warehouse','SUPPORTED'),
    ('Was anyone where ceiling_00 cannot see in these 10 seconds?','warehouse','SUPPORTED'),
    ('At Queen and Spadina, what is the building on the right?','fleet','SUPPORTED'),
    ('At Avenue Road and Cumberland Street, what is the building on the right?','fleet','OUT_OF_RANGE'),
    ('When does a pedestrian cross the street?','street','UNRESOLVED'),
    ('How many people walk past in this clip?','street','UNRESOLVED'),
    ('How many people in this clip?','street','SUPPORTED'),
    ('When is the street busiest in this clip?','street','SUPPORTED')])
def test_b2_confirmed_presets_have_evidence_scoped_status(corpus, q, workspace, expected):
    frames=ask(corpus,q,{'workspace':workspace,**({'scene_key':L1_KEY} if workspace=='street' else {})})
    assert of(frames,'status')[-1]['final']==expected
