import pytest

from app.core import tools, vlm
from app.core.models import EntityRef, ToolResult
from app.tests.unit.a5_fakes import fixture_store, install_vlm


def test_metric_and_tools(tmp_path, monkeypatch):
    s = fixture_store(tmp_path)
    a = EntityRef(scene_key='wh-sdg-fixture-01', entity_id='P1', kind='person')
    b = EntityRef(scene_key=a.scene_key, entity_id='F1', kind='forklift')
    r = tools.measure(s, a.scene_key, 4600, a, b, 'distance', cam='ceiling_04')
    assert isinstance(r, ToolResult) and r.evidence[0].value > 0
    old = s.manifest
    monkeypatch.setattr(s, 'manifest', lambda k: {**old(k), 'metric': False})
    r = tools.measure(s, a.scene_key, 4600, a, b, 'distance')
    assert r.envelope.output == {'status': 'UNRESOLVED', 'reason': 'scene_not_metric'}


def test_bound_ego_distance_clamps_before_reading_chunks(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.asset_json
    reads=[]
    def asset(key,path):
        if path.startswith('ego/'):
            reads.append(path)
            assert path=='ego/00000.json', 'Read beyond the manifest interval'
        return original(key,path)
    monkeypatch.setattr(s,'asset_json',asset)
    target=EntityRef(scene_key=FL,entity_id='P1',kind='person')
    r=tools.bound_ego_distance(s,FL,(3001,2**53-1),target)
    assert reads==['ego/00000.json']
    assert r.envelope.input_range.t1_ms==10000 and r.envelope.output['t_ms']==7900
    assert r.envelope.output['value']==pytest.approx(5.67,abs=.01)


@pytest.mark.parametrize('interval',[(10000,2**53-1),(0,0)])
def test_bound_ego_distance_empty_interval_reads_no_chunks(tmp_path,monkeypatch,interval):
    s=fixture_store(tmp_path)
    def read(*a): raise AssertionError('Empty interval must not scan')
    monkeypatch.setattr(tools,'remaining',read)
    r=tools.bound_ego_distance(s,FL,interval,EntityRef(scene_key=FL,entity_id='P1',kind='person'))
    assert not r.evidence and r.envelope.output['status']=='UNRESOLVED'


def test_bound_ego_distance_without_directory_does_not_scan(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    original=s.manifest
    monkeypatch.setattr(s,'manifest',lambda k:{**original(k),'assets':{}})
    def scan(*a): raise AssertionError('Missing directory must not scan')
    monkeypatch.setattr(tools,'remaining',scan)
    r=tools.bound_ego_distance(s,FL,(3001,2**53-1),EntityRef(scene_key=FL,entity_id='P1',kind='person'))
    assert not r.evidence and r.envelope.output['status']=='UNRESOLVED'


def test_bound_ego_distance_returns_valid_extremes_with_separate_evidence(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.asset_json
    def asset(key,path):
        if path.startswith('ego/'):
            samples=[(3000,14.,.2),(3500,5.,.1),(4000,14.,.3),(4500,99.,-1.),(5000,float('nan'),.1)]
            return {'frames':[{'t_ms':t,'peds':[{'entity_id':'P1','x':x,'y':0.,'sigma_m':sigma}]} for t,x,sigma in samples]}
        return original(key,path)
    monkeypatch.setattr(s,'asset_json',asset)
    create=tools.evidence
    params=[]
    def evidence(*a,**kw):
        params.append(kw['params'])
        return create(*a,**kw)
    monkeypatch.setattr(tools,'evidence',evidence)
    r=tools.bound_ego_distance(s,FL,(3000,8000),EntityRef(scene_key=FL,entity_id='P1',kind='person'))
    assert len(r.evidence)==2, 'Both minimum and maximum need their own evidence'
    low,high=r.evidence
    assert low.eid!=high.eid and [p['extreme'] for p in params]==['min','max']
    assert all(e.method=='bound_ego_distance' for e in r.evidence)
    assert (low.value['distance_m'],low.sigma,low.t0_ms)==(5.,.1,3500)
    assert (high.value['distance_m'],high.sigma,high.t0_ms)==(14.,.2,3000)
    out=r.envelope.output
    assert (out['value'],out['sigma'],out['t_ms'])==(5.,.1,3500)
    assert (out['maximum_value'],out['maximum_sigma'],out['maximum_t_ms'])==(14.,.2,3000)
    assert out['coverage_basis']=='full-window coverage not measured' and r.envelope.coverage is None


def test_visibility_eids_bind_sample_time_and_point(tmp_path):
    s=fixture_store(tmp_path)
    a=tools.visibility(s,WH,0,'P1').evidence[0]
    b=tools.visibility(s,WH,200,'P1').evidence[0]
    assert a.value!=b.value and a.eid!=b.eid
    c=tools.visibility(s,WH,0,(1.,1.)).evidence[0]
    d=tools.visibility(s,WH,0,(2.,1.)).evidence[0]
    assert c.eid!=d.eid
    assert a.eid==tools.visibility(s,WH,0,'P1').evidence[0].eid


def test_events_eids_bind_zone(tmp_path,monkeypatch):
    from app.api import scenes
    s=fixture_store(tmp_path)
    monkeypatch.setattr(scenes,'event_zone_time',lambda s,k,e,t0,t1,zone:4600 if zone.bounds[0]==0 else 4700)
    a=tools.events(s,WH,4300,5900,[(0,0),(10,0),(10,10),(0,10)])
    b=tools.events(s,WH,4300,5900,[(1,0),(10,0),(10,10),(1,10)])
    assert a.evidence and b.evidence
    assert {e.eid for e in a.evidence}.isdisjoint(e.eid for e in b.evidence)


def test_visibility_unknown_states_reduce_coverage(tmp_path):
    s=fixture_store(tmp_path)
    r=tools.visibility(s,WH,200,(100.,100.))
    assert r.envelope.coverage==0 and r.envelope.quality!='ok'
    assert {g[2] for g in r.envelope.gaps}=={'unknown'}
    r=tools.visibility(s,WH,6500,(1.,1.),cam='eye_00')
    assert r.envelope.coverage==0 and {g[2] for g in r.envelope.gaps}=={'no_video'}


@pytest.mark.parametrize('operand,change',[('a',{'scene_key':'wh-other'}),('b',{'scene_key':'wh-other'}),
                                         ('a',{'kind':'vehicle'}),('b',{'kind':'person'})])
def test_measure_rejects_wrong_scene_and_kind(tmp_path,operand,change):
    s=fixture_store(tmp_path)
    a=EntityRef(scene_key=WH,entity_id='P1',kind='person')
    b=EntityRef(scene_key=WH,entity_id='F1',kind='forklift')
    if operand=='a': a=a.model_copy(update=change)
    else: b=b.model_copy(update=change)
    r=tools.measure(s,WH,5000,a,b,'distance')
    assert not r.evidence and r.envelope.output['status']=='UNRESOLVED'


def test_measure_requires_both_operands_in_requested_camera(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    original=s.asset_json
    def asset(key,path):
        d=original(key,path)
        if path.startswith('boxes/ceiling_04/'):
            d['rows']=[r for r in d['rows'] if r[1]!=6]
        return d
    monkeypatch.setattr(s,'asset_json',asset)
    a=EntityRef(scene_key=WH,entity_id='P1',kind='person')
    b=EntityRef(scene_key=WH,entity_id='F1',kind='forklift')
    r=tools.measure(s,WH,5000,a,b,'distance',cam='ceiling_04')
    assert not r.evidence and r.envelope.output['status']=='UNRESOLVED'
    assert tools.measure(s,WH,5000,a,b,'distance').evidence


def test_ego_fallback_has_no_camera_binding(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    monkeypatch.setattr(tools.live_measure,'measure',lambda *a,**kw:{'value_m':None,'reason':'offline'})
    r=tools.ego_distance(s,FL,'dashcam',3000,4000,rows=[])
    assert r.evidence[0].cam_id is None


def test_synced_thumbnail_reports_its_own_time(tmp_path):
    s=fixture_store(tmp_path)
    r=tools.get_synced_views(s,WH,4600,cam='ceiling_04')
    frame,=r.envelope.output['cams']
    assert frame['frame_url'].endswith('/00004.jpg') and frame['media_pts_ms']==4000-frame['offset_ms']
    assert r.envelope.pts_read==1


def test_ground_position_capability_matches_tool_requirements(tmp_path):
    from app.core.models import capabilities
    s=fixture_store(tmp_path)
    m=s.manifest(WH)
    assert capabilities(m)['ground_position']['ok']
    assert capabilities({**m,'metric':False})['ground_position']['reason']=='scene_not_metric'
    # fleet keeps its metric flag: "no_geometry" text would claim the dashcam has no geometry
    assert capabilities({**s.manifest(FL),'metric':False})['ground_position']['reason']=='scene_not_metric'


def test_c2_prefix_and_circuit(tmp_path, monkeypatch):
    s = fixture_store(tmp_path)
    install_vlm(monkeypatch)
    actor = EntityRef(scene_key='wh-sdg-fixture-01', entity_id='F1', kind='forklift')
    r = tools.predict_next(s, actor.scene_key, 4500, actor)
    record = r.envelope.output['record']
    assert record['payload']['max_frame_ms'] <= 4500
    assert all(2500 <= f['t_ms'] < 4500 for f in record['input_frame_refs'])
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise vlm.VLMUnavailable('offline')
    monkeypatch.setattr(vlm, 'situation', fail)
    circuit = {'failures': 0}
    for _ in range(3):
        r = tools.situation(s, actor.scene_key, 4600, [actor], circuit=circuit)
        assert r.envelope.exec_state == 'failed' and r.envelope.gaps and not r.evidence
    assert len(calls) == 2


def test_every_cosmos_tool_and_invalid_prefix(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    install_vlm(monkeypatch)
    key='wh-sdg-fixture-01'
    actor=EntityRef(scene_key=key,entity_id='F1',kind='forklift')
    results=[tools.situation(s,key,4600,[actor]),tools.ground_objects(s,key,'ceiling_00',4500,['shelf']),
             tools.localize(s,key,'ceiling_04',1500,7500,'worker steps back'),
             tools.describe_region(s,key,'eye_00',4500,'F1')]
    assert all(isinstance(r,ToolResult) and r.evidence and r.envelope.pts_read for r in results)
    original=vlm.predict_next
    def invalid(*a,**kw):
        r=original(*a,**kw)
        return r.model_copy(update={'prefix_only':False})
    monkeypatch.setattr(vlm,'predict_next',invalid)
    assert tools.predict_next(s,key,4500,actor).envelope.exec_state=='failed'


def test_source_gap_observation_and_speed(tmp_path):
    s=fixture_store(tmp_path)
    r=tools.get_synced_views(s,'wh-sdg-fixture-01',6500,cam='eye_00')
    assert r.envelope.pts_read==0 and r.envelope.gaps
    r=tools.visibility(s,'wh-sdg-fixture-01',2100,'P2')
    assert r.envelope.output['person']['observed_in']==['ceiling_00']
    actor=EntityRef(scene_key='wh-sdg-fixture-01',entity_id='F1',kind='forklift')
    assert tools.measure(s,actor.scene_key,4600,actor,None,'speed').evidence[0].unit=='m/s'
    assert tools.events(s,actor.scene_key,4300,5900).evidence


def test_blind_presence_intervals(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    def no_moment(*a): raise AssertionError('moment read per sample')
    monkeypatch.setattr(s,'moment',no_moment)
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_00',0,10000)
    # P2 is occluded from ceiling_00 from frame 3.6 s through frame 9.2 s (10 fps skeleton)
    assert [(e.value['entity_id'],e.t0_ms,e.t1_ms,e.value['seen_by']) for e in r.evidence]==[
        ('P2',3600,6000,['ceiling_04','eye_00']),('P1',4000,6000,['ceiling_04','eye_00']),
        ('P2',6000,7000,['ceiling_04']),('P2',7000,9300,['ceiling_04','eye_00'])]
    assert 'eye_00' not in r.evidence[2].value['seen_by'] and 'eye_00' in r.evidence[3].value['seen_by']
    assert all((e.kind,e.method,e.cam_id)==('measure','blind_presence','ceiling_00') for e in r.evidence)
    assert r.envelope.coverage==1
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_04',4000,10000)
    assert r.evidence==[] and r.envelope.coverage==1


def test_blind_presence_unknown_and_no_video_are_gaps_not_blind(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    key='wh-sdg-fixture-01'
    # eye_00 has no video in second 6
    r=tools.blind_presence(s,key,'eye_00',6000,7000)
    assert r.evidence==[] and r.envelope.coverage<1 and {g[2] for g in r.envelope.gaps}=={'no_video'}
    original=s.asset_json
    def off_grid(scene_key, relpath):
        doc=original(scene_key,relpath)
        if relpath=='skeleton.json':
            doc['persons']['P2']['root_world']=[[100.,100.,0.]]*len(doc['persons']['P2']['t_ms'])
        return doc
    monkeypatch.setattr(s,'asset_json',off_grid)
    r=tools.blind_presence(s,key,'ceiling_00',0,10000)
    assert [e.value['entity_id'] for e in r.evidence]==['P1']
    assert r.envelope.coverage<1 and {g[2] for g in r.envelope.gaps}=={'unknown'}


def without_skeleton_after(store, monkeypatch, t_ms):
    original=store.asset_json
    def cut(scene_key, relpath):
        doc=original(scene_key,relpath)
        if relpath=='skeleton.json':
            for p in doc['persons'].values():
                keep=[i for i,t in enumerate(p['t_ms']) if t<t_ms]
                for k in ('t_ms','root_world','root_sigma_m'):
                    p[k]=[p[k][i] for i in keep]
        return doc
    monkeypatch.setattr(store,'asset_json',cut)


def test_blind_presence_seconds_without_skeleton_samples_are_gaps(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    without_skeleton_after(s,monkeypatch,7000)
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_04',7000,10000)
    assert r.evidence==[] and r.envelope.pts_read==0
    assert r.envelope.coverage==0 and r.envelope.output['seconds_checked']==0
    assert r.envelope.gaps==[(7000,8000,'unknown'),(8000,9000,'unknown'),(9000,10000,'unknown')]
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_04',4000,10000)
    assert r.envelope.coverage==.5 and [g[:2] for g in r.envelope.gaps]==[(7000,8000),(8000,9000),(9000,10000)]


def test_blind_presence_merges_frames_on_a_rounded_15_fps_grid(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    original=s.asset_json
    def fps15(scene_key, relpath):
        doc=original(scene_key,relpath)
        if relpath=='skeleton.json':
            doc['fps']=15
            for p in doc['persons'].values():
                p['t_ms']=[round(t/100*1000/15) for t in p['t_ms']]
        return doc
    monkeypatch.setattr(s,'asset_json',fps15)
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_00',0,10000)
    # Rounded 15 fps stamps must merge throughout each stable witness epoch,
    # while the eye_00 outage at 6 s still splits the continuing blind run.
    assert [(e.value['entity_id'],e.t0_ms,e.t1_ms,e.value['seen_by']) for e in r.evidence]==[
        ('P2',2400,6000,['ceiling_04','eye_00']),('P1',3333,4867,['ceiling_04','eye_00']),
        ('P2',6000,6200,['ceiling_04'])]


def test_street_scene_tools_abstain_with_the_capability_reason(tmp_path):
    from app.tests.unit.test_agent import L1_KEY, corpus_store
    s=corpus_store(tmp_path)
    a=EntityRef(scene_key=L1_KEY,entity_id='P1',kind='person')
    b=EntityRef(scene_key=L1_KEY,entity_id='V1',kind='vehicle')
    for r, reason in [(tools.measure(s,L1_KEY,1000,a,b,'distance'),'no_geometry'),(tools.measure(s,L1_KEY,1000,a,None,'speed'),'no_gps'),
                      (tools.visibility(s,L1_KEY,1000,'P1'),'no_geometry'),(tools.speed_limit(s,L1_KEY,1000),'no_gps')]:
        assert r.envelope.output=={'status':'UNRESOLVED','reason':reason} and not r.evidence
        assert [g[2] for g in r.envelope.gaps]==[reason]


def test_detections_2d_reads_the_street_boxes(tmp_path):
    from app.tests.unit.test_agent import L1_KEY, corpus_store
    s=corpus_store(tmp_path)
    groups={'person':['person'],'vehicle':['car','truck']}
    # the fixture car passes from 2.1 s
    r=tools.detections_2d(s,L1_KEY,'fixture_cam-1',0,2000,groups)
    ev,=r.evidence
    assert (ev.kind,ev.method,ev.cam_id,ev.observation_ids)==('measure','yolo_coco_2d','fixture_cam-1',[])
    assert ev.value['counts']=={'person':1} and ev.value['frames_together']==0 and r.envelope.pts_read==ev.value['frames_read']>0
    r=tools.detections_2d(s,L1_KEY,'fixture_cam-1',2000,5000,groups)
    assert r.evidence[0].value['frames_together']>0 and r.envelope.coverage==1 and r.envelope.gaps==[]


def test_blind_presence_splits_at_visible_samples(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    real=tools.geometry.floor_visibility
    def flicker(data, t_ms, point):
        states=real(data,t_ms,point)
        # Verified visibility interrupts each person's blind interval.
        return {**states,'ceiling_00':'visible'} if 4800<=t_ms<5100 else states
    monkeypatch.setattr(tools.geometry,'floor_visibility',flicker)
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_00',0,10000)
    assert [(e.value['entity_id'],e.t0_ms,e.t1_ms,e.value['seen_by']) for e in r.evidence]==[
        ('P2',3600,4800,['ceiling_04','eye_00']),('P1',4000,4800,['ceiling_04','eye_00']),
        ('P1',5100,6000,['ceiling_04','eye_00']),('P2',5100,6000,['ceiling_04','eye_00']),
        ('P2',6000,7000,['ceiling_04']),('P2',7000,9300,['ceiling_04','eye_00'])]
    assert all('eye_00' not in e.value['seen_by'] for e in r.evidence if e.t0_ms==6000)


WH, FL = 'wh-sdg-fixture-01', 'fl-pie-fixture-01'


@pytest.mark.parametrize('task', ['situation','predict_next','ground_objects','localize','describe_region'])
def test_cosmos_state_follows_where_the_record_came_from(tmp_path, monkeypatch, task):
    s=fixture_store(tmp_path)
    install_vlm(monkeypatch)
    monkeypatch.setenv('RUNS_DIR',str(tmp_path/'runs'))
    actors=[EntityRef(scene_key=WH,entity_id='P1',kind='person'),EntityRef(scene_key=WH,entity_id='F1',kind='forklift')]
    args={'situation':(WH,4600,actors),'predict_next':(WH,4500,actors[1]),
          'ground_objects':(WH,'ceiling_00',4500,['shelf']),
          'localize':(WH,'ceiling_04',1500,7500,'worker steps back'),
          'describe_region':(WH,'eye_00',4500,'F1')}
    call=lambda:getattr(tools,task)(s,*args[task])
    # the scene's pre-run file
    assert call().envelope.exec_state=='precomputed'
    recorded=getattr(vlm,task)
    def live(*a,**kw):
        rec=recorded(*a,**kw)
        path=vlm._cache_dir()/f'{rec.record_id}.json'
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(rec.model_dump_json())
        return rec
    monkeypatch.setattr(vlm,task,live)
    assert call().envelope.exec_state=='live'
    monkeypatch.setattr(vlm,task,recorded)
    # the disk cache an earlier live call wrote
    assert call().envelope.exec_state=='cached'


@pytest.mark.parametrize('mode, delivered, existing, state', [('site',True,False,'live'),('site',False,False,'precomputed'),
                                                              ('site',True,True,'precomputed'),('dev',True,False,'precomputed')])
def test_trigger_is_live_only_when_it_sends_the_alert(tmp_path, monkeypatch, mode, delivered, existing, state):
    from app.api import actions
    from app.core.models import TriggerRequest, TriggerResult
    s=fixture_store(tmp_path)
    monkeypatch.setenv('MODE',mode)
    monkeypatch.setattr(actions,'trigger',lambda store,req:TriggerResult(action_id='a1',delivered=delivered,review_task_id=None,
                                                                        zone_id=None,existing=existing))
    r=tools.trigger(s,TriggerRequest(scene_key=WH,auto=True,actions=['wandb_alert']))
    assert r.envelope.exec_state==state


LIVE={'value_m':7.5,'sigma_m':0.325,'t_ms':3400,'track_ids':[3],'n_frames':30,'n_boxes':12,
      'source_uris':['s3://bucket/detections/seg.json.gz'],'timing_ms':{'sidecar':40.0,'total':120.0}}


def test_ego_distance_uses_the_live_value(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    seen=[]
    def measure(store, key, cam, t0, t1, pair=('person','forklift'), *, rows):
        seen.append((key,cam,t0,t1,pair,rows))
        return LIVE
    monkeypatch.setattr(tools.live_measure,'measure',measure)
    r=tools.ego_distance(s,FL,'dashcam',3000,4000,rows=['row'])
    assert seen==[(FL,'dashcam',3000,4000,('person','vehicle'),['row'])]
    ev,=r.evidence
    assert r.envelope.exec_state=='live' and (ev.kind,ev.method,ev.value,ev.sigma,ev.unit,ev.t0_ms)==('measure','ego_distance',7.5,.325,'m',3400)
    assert r.envelope.output['live']['source_uris']==LIVE['source_uris'] and r.envelope.output['live']['timing_ms']['total']==120
    # P1 is closest at 3.9 s in the ego frames
    assert r.envelope.output['precomputed']=={'value_m':24.168,'sigma_m':1.252,'t_ms':3900,'entity_id':'P1'}


@pytest.mark.parametrize('live', [{**LIVE,'value_m':None,'reason':'no live segment covers it'},RuntimeError('no such key')])
def test_ego_distance_falls_back_to_the_precomputed_value(tmp_path, monkeypatch, live):
    s=fixture_store(tmp_path)
    def measure(*a, **kw):
        if isinstance(live,Exception):
            raise live
        return live
    monkeypatch.setattr(tools.live_measure,'measure',measure)
    r=tools.ego_distance(s,FL,'dashcam',3000,4000,rows=[])
    ev,=r.evidence
    assert r.envelope.exec_state=='precomputed' and (ev.method,ev.value,ev.sigma,ev.t0_ms)==('ego_distance',24.168,1.252,3900)
    assert r.envelope.output['reason']==('no live segment covers it' if isinstance(live,dict) else 'RuntimeError: no such key')


@pytest.mark.parametrize('metric', [True, False])
def test_ego_distance_without_live_or_precomputed_pedestrian_is_unresolved(tmp_path, monkeypatch, metric):
    s=fixture_store(tmp_path)
    manifest=s.manifest
    monkeypatch.setattr(s,'manifest',lambda key:{**manifest(key),'metric':metric})
    calls=[]
    def measure(*a,**kw):
        calls.append(1)
        return {'value_m':None,'reason':'no frame has both'}
    monkeypatch.setattr(tools.live_measure,'measure',measure)
    r=tools.ego_distance(s,FL,'dashcam',0,2000,rows=[])
    assert not r.evidence and r.envelope.exec_state=='precomputed'
    assert r.envelope.output['status']=='UNRESOLVED'
    assert r.envelope.output['reason']==('no frame has both' if metric else 'scene_not_metric')
    assert calls==([1] if metric else [])


@pytest.mark.parametrize('name', ['get_synced_views','visibility','blind_presence','measure','closest_approach',
                                 'geocode','find_passes','searched_passes','facade','gps_position','speed_limit',
                                 'speeding_candidates','events','detections_2d','search'])
def test_asset_tools_are_precomputed_not_live(tmp_path, monkeypatch, name):
    from app.tests.unit.test_agent import corpus_store
    from app.tests.unit.a5_fakes import L1_CAM, L1_KEY
    s = corpus_store(tmp_path)
    monkeypatch.delenv('EMBED_URL', raising=False)
    person = EntityRef(scene_key=WH,entity_id='P1',kind='person')
    forklift = EntityRef(scene_key=WH,entity_id='F1',kind='forklift')
    place = tools.geo.geocode(s,'Queen and Spadina')
    passes = tools.geo.find_passes(s,place['place_id'],100,360)
    calls = {
        'get_synced_views': lambda:tools.get_synced_views(s,WH,4600),
        'visibility': lambda:tools.visibility(s,WH,4600,'P1'),
        'blind_presence': lambda:tools.blind_presence(s,WH,'ceiling_00',0,10000),
        'measure': lambda:tools.measure(s,WH,4600,person,forklift,'distance'),
        'closest_approach': lambda:tools.closest_approach(s,[s.manifest(WH)]),
        'geocode': lambda:tools.geocode(s,'Queen and Spadina'),
        'find_passes': lambda:tools.find_passes(s,place['place_id'],100,360),
        'searched_passes': lambda:tools.searched_passes(s,passes),
        'facade': lambda:tools.facade(s,[p for p in passes if s.manifest(p['trip_id'])['cams']],place['lat'],place['lon'],None),
        'gps_position': lambda:tools.gps_position(s,FL,3000),
        'speed_limit': lambda:tools.speed_limit(s,FL,3000),
        'speeding_candidates': lambda:tools.speeding_candidates(s,[s.manifest(FL)]),
        'events': lambda:tools.events(s,WH,0,10000),
        'detections_2d': lambda:tools.detections_2d(s,L1_KEY,L1_CAM,0,5000,{'person':['person']}),
        'search': lambda:tools.search(s,'Find a person',{'workspace':'warehouse'}),
    }
    assert calls[name]().envelope.exec_state=='precomputed'
