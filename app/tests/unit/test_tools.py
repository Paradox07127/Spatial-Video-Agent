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
        ('P2',3600,9300,['ceiling_04','eye_00']),('P1',4000,6000,['ceiling_04','eye_00'])]
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
    runs={}
    for e in r.evidence:
        runs.setdefault(e.value['entity_id'],[]).append((e.t0_ms,e.t1_ms))
    # SDG skeletons store 15 fps stamps rounded to the ms (0, 67, 133, ...); one blind run must stay one interval
    assert all(b[0]-a[1]>100 for v in runs.values() for a,b in zip(v,v[1:])), runs


def test_blind_presence_joins_gaps_shorter_than_the_visibility_step(tmp_path, monkeypatch):
    s=fixture_store(tmp_path)
    real=tools.geometry.floor_visibility
    def flicker(data, t_ms, point):
        states=real(data,t_ms,point)
        # P1's root sits on a cell edge for 0.3 s and reads as visible to ceiling_00
        return {**states,'ceiling_00':'visible'} if 4800<=t_ms<5100 else states
    monkeypatch.setattr(tools.geometry,'floor_visibility',flicker)
    r=tools.blind_presence(s,'wh-sdg-fixture-01','ceiling_00',0,10000)
    assert [(e.value['entity_id'],e.t0_ms,e.t1_ms) for e in r.evidence]==[('P2',3600,9300),('P1',4000,6000)]
