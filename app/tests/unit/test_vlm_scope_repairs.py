"""Hard-scope and malformed model output regressions (no endpoint/network)."""
import json
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from app.core import vlm
from app.core.models import EntityRef

K='wh-test'

@pytest.fixture
def model(monkeypatch,tmp_path):
    manifest={'workspace':'warehouse','t0_ms':0,'t1_ms':10000,'fps':30,'cams':[{'id':'eye'},{'id':'ceiling'}], 'assets':{}}
    monkeypatch.setenv('RUNS_DIR',str(tmp_path))
    monkeypatch.setenv('VLM_URL','http://model')
    monkeypatch.setenv('VLM_MODEL','model')
    monkeypatch.setattr(vlm,'_manifest',lambda key:manifest)
    monkeypatch.setattr(vlm,'_cache_identity',lambda key:{'source':'s','endpoint':'e','model':'model'})
    monkeypatch.setattr(vlm,'_json',lambda *a:None)
    monkeypatch.setattr(vlm,'_pick_cam',lambda *a:'ceiling')
    frames=[]
    monkeypatch.setattr(vlm,'_frames',lambda *a,**kw:frames)
    output={'label':'passing','reason':'observed','uncertainty':.2}
    monkeypatch.setattr(vlm,'_chat',lambda *a:output.copy())
    return frames,output


def test_situation_hard_scope_and_camera_cache_are_distinct(model):
    frames,out=model
    frames.extend([(2799,Image.new('RGB',(2,2))),(2800,Image.new('RGB',(2,2))),(2801,Image.new('RGB',(2,2)))])
    refs=[EntityRef(scene_key=K,entity_id='P1',kind='person')]
    a=vlm.situation(K,2800,refs,cam='eye',t_range=(2800,2801))
    assert a.cam_id=='eye' and [f.t_ms for f in a.input_frame_refs]==[2800]
    b=vlm.situation(K,2800,refs,cam='ceiling',t_range=(2800,2801))
    assert b.record_id!=a.record_id
    with pytest.raises(vlm.VLMUnavailable,match='no video frames'):
        vlm.situation(K,2800,refs,cam='eye',t_range=(2802,2803))


@pytest.mark.parametrize('segment', [(-1,1),(2,1),(18,19),(float('nan'),1)])
def test_c4_rejects_invalid_localization(model,segment):
    frames,out=model
    frames.extend([(1000,Image.new('RGB',(2,2))),(1250,Image.new('RGB',(2,2)))])
    out.clear();out['segments']=[{'start_s':segment[0],'end_s':segment[1],'text':'bad'}]
    with pytest.raises(vlm.VLMUnavailable,match='invalid_model_localization'):
        vlm.localize(K,'eye',1000,1500,'test')


@pytest.mark.parametrize('box', [[4,1,2,3],[1,4,3,2],[1,2,float('inf'),4]])
def test_c3_rejects_invalid_grounding(model,box):
    frames,out=model
    frames.append((1000,Image.new('RGB',(2,2))))
    out.clear();out['objects']=[{'label':'rack','box_2d':box}]
    with pytest.raises(vlm.VLMUnavailable):
        vlm.ground_objects(K,'eye',1000,['rack'])


def test_chat_nontext_and_no_run_circuit_recovers(monkeypatch):
    monkeypatch.setenv('VLM_URL','http://model');monkeypatch.setenv('VLM_MODEL','m')
    vlm._fails.clear();vlm._opened.clear()
    def post(url,**kwargs):
        return httpx.Response(200,json={'choices':[{'message':{'content':{'bad':1}}}]},request=httpx.Request('POST',url))
    monkeypatch.setattr(vlm.httpx,'post',post)
    with pytest.raises(vlm.VLMUnavailable,match='not text'):
        vlm._chat([],'p',{},None)
    def fail(*a,**kw):raise httpx.ConnectError('offline')
    monkeypatch.setattr(vlm.httpx,'post',fail)
    for _ in range(2):
        with pytest.raises(vlm.VLMUnavailable):vlm._chat([],'p',{},None)
    with pytest.raises(vlm.VLMUnavailable,match='circuit open'):vlm._chat([],'p',{},None)
    monkeypatch.setattr(vlm.time,'monotonic',lambda:vlm._opened[('http://model','m')]+31)
    monkeypatch.setattr(vlm.httpx,'post',lambda url,**kw:httpx.Response(200,json={'choices':[{'message':{'content':'{}'}}]},request=httpx.Request('POST',url)))
    assert vlm._chat([],'p',{},None)=={}


def test_observe_window_cites_actual_frames_and_cache(model,monkeypatch):
    frames,out=model
    frames.extend([(1000,Image.new('RGB',(2,2))),(1250,Image.new('RGB',(2,2)))])
    out.clear();out['facts']=[{'subject_ids':['P1'],'text':'P1 faces the aisle.','frame_indices':[1],'uncertainty':.2}]
    monkeypatch.setattr(vlm,'_boxes',lambda *a:{1:[[1000,1,'person',1,1,2,2,.9],[1250,1,'person',1,1,2,2,.9]]})
    monkeypatch.setattr(vlm,'_observations',lambda *a:[('P1','eye',1,1000,1500)])
    monkeypatch.setattr(vlm,'_manifest',lambda key:{'workspace':'warehouse','t0_ms':0,'t1_ms':10000,'fps':30,'cams':[{'id':'eye','w':4,'h':4},{'id':'ceiling','w':4,'h':4}], 'assets':{}})
    actor=EntityRef(scene_key=K,entity_id='P1',kind='person')
    a=vlm.observe_window(K,'eye',(1000,1500),[actor])
    assert a['exec_state']=='live' and a['facts'][0]['frame_refs']==[{'cam_id':'eye','t_ms':1250}]
    b=vlm.observe_window(K,'eye',(1000,1500),[actor])
    assert b['exec_state']=='cached' and b['input_digest']==a['input_digest']
    frames[1]=(1250,Image.new('RGB',(2,2),'red'))
    c=vlm.observe_window(K,'eye',(1000,1500),[actor])
    assert c['exec_state']=='live' and c['input_digest']!=a['input_digest']


@pytest.mark.parametrize('change',[{'subject_ids':['P2']},{'frame_indices':[3]},{'uncertainty':float('nan')},{'text':'P1 is 2 meters away.'}])
def test_observe_window_rejects_false_citations(model,change):
    frames,out=model
    frames.append((1000,Image.new('RGB',(2,2))))
    out.clear();out['facts']=[{'subject_ids':['P1'],'text':'P1 faces the aisle.','frame_indices':[0],'uncertainty':.2,**change}]
    with pytest.raises(vlm.VLMUnavailable,match='invalid_model_observation'):
        vlm.observe_window(K,'eye',(1000,1500),[EntityRef(scene_key=K,entity_id='P1',kind='person')])


def test_cached_c4_is_revalidated_before_replay(model,tmp_path):
    frames,out=model
    frames.extend([(1000,Image.new('RGB',(2,2))),(1250,Image.new('RGB',(2,2)))])
    out.clear();out['segments']=[{'start_s':1.0,'end_s':1.25,'text':'visible'}]
    first=vlm.localize(K,'eye',1000,1500,'cache guard')
    cache=tmp_path/'vlm_cache'/f'{first.record_id}.json'
    saved=json.loads(cache.read_text());saved['record']['payload']['segments'][0]['end_s']=19
    cache.write_text(json.dumps(saved))
    second=vlm.localize(K,'eye',1000,1500,'cache guard')
    assert second.payload['segments'][0]['end_s']==1.25 and second._sva_exec_state=='live'


def test_cached_situation_wrong_camera_is_not_replayed(model,tmp_path):
    frames,out=model;frames.append((1000,Image.new('RGB',(2,2))))
    first=vlm.situation(K,1000,[],cam='eye',t_range=(1000,1001))
    cache=tmp_path/'vlm_cache'/f'{first.record_id}.json'
    saved=json.loads(cache.read_text());saved['record']['input_frame_refs'][0]['cam_id']='ceiling'
    cache.write_text(json.dumps(saved))
    second=vlm.situation(K,1000,[],cam='eye',t_range=(1000,1001))
    assert second.input_frame_refs[0].cam_id=='eye' and second._sva_exec_state=='live'


QUANTITIES=[('A dozen workers stand near the forklift.',False),('Two parked vehicles are visible.',False),
            ('The vehicle moves at twenty miles per hour.',False),('Twelve people stand in the aisle.',False),
            ('The person is half a meter from the vehicle.',False),('The person travels sixty kilometres per hour.',False),
            ('A worker in a yellow vest walks past the forklift.',True)]


@pytest.mark.parametrize('text,ok',QUANTITIES)
def test_observe_window_rejects_spelled_quantities(model,text,ok):
    frames,out=model
    frames.append((1000,Image.new('RGB',(2,2))))
    out.clear();out['facts']=[{'subject_ids':[],'text':text,'frame_indices':[0],'uncertainty':.2}]
    if ok:
        assert vlm.observe_window(K,'eye',(1000,1500),[])['facts'][0]['text']==text
    else:
        with pytest.raises(vlm.VLMUnavailable,match='invalid_model_observation'):
            vlm.observe_window(K,'eye',(1000,1500),[])


def _recording(monkeypatch):
    seen=[]
    def frames(key,m,cam,targets,*a,**kw):
        seen.append((targets,kw));return [(t,Image.new('RGB',(2,2))) for t in targets]
    monkeypatch.setattr(vlm,'_frames',frames)
    return seen


def test_situation_window_is_t_ms_neighbourhood_inside_hard_range(model,monkeypatch):
    seen=_recording(monkeypatch)
    vlm.situation(K,4600,[],cam='eye',t_range=(0,600000))
    targets,kw=seen[0]
    assert 1600<=min(targets) and max(targets)<7600 and kw['hard_window']==(1600,7600)


def test_live_caps_frames_and_reads_inside_hard_window(model,monkeypatch):
    frames,out=model
    out.clear();out['segments']=[]
    seen=_recording(monkeypatch)
    vlm.localize(K,'eye',0,10000,'long window')
    targets,kw=seen[0]
    assert len(targets)<=vlm.MAX_FRAMES and kw['hard_window']==(0,10000)


def test_observations_respect_association_validity(monkeypatch):
    ents={'entities':[{'entity_id':'P1','association':{'valid':[2000,4000]},'observations':[
        {'cam_id':'eye','local_track_id':1,'t0_ms':0,'t1_ms':5000},{'cam_id':'eye','local_track_id':2,'t0_ms':6000,'t1_ms':8000}]}]}
    monkeypatch.setattr(vlm,'_json',lambda *a:ents)
    assert vlm._observations(K,{'assets':{'entities':'entities.json'}})==[('P1','eye',1,2000,4000)]


@pytest.mark.parametrize('segment,ok',[((1.0,2.6),False),((2.5,2.9),True),((1.0,1.4),True)])
def test_c4_coverage_keeps_internal_gaps(model,segment,ok):
    frames,out=model
    frames.extend([(t,Image.new('RGB',(2,2))) for t in (1000,1250,2500,2750)])
    out.clear();out['segments']=[{'start_s':segment[0],'end_s':segment[1],'text':'seen'}]
    if ok:
        assert vlm.localize(K,'eye',1000,3000,'gap').payload['segments'][0]['end_s']==segment[1]
    else:
        with pytest.raises(vlm.VLMUnavailable,match='invalid_model_localization'):
            vlm.localize(K,'eye',1000,3000,'gap')


def test_cache_write_failure_does_not_abort_the_model_call(model,monkeypatch,tmp_path):
    frames,out=model
    frames.append((1000,Image.new('RGB',(2,2))))
    blocked=tmp_path/'blocked';blocked.write_text('not a directory')
    monkeypatch.setenv('RUNS_DIR',str(blocked))
    assert vlm.situation(K,1000,[],cam='eye',t_range=(1000,1001)).payload['label']=='passing'
    out.clear();out['facts']=[]
    assert vlm.observe_window(K,'eye',(1000,1500),[])['facts']==[]


@pytest.mark.parametrize('error',[OSError('disk'),httpx.ReadError('reset')])
def test_model_input_io_errors_are_unavailable(model,monkeypatch,error):
    def broken(*a,**kw):raise error
    monkeypatch.setattr(vlm,'_frames',broken)
    with pytest.raises(vlm.VLMUnavailable):
        vlm.situation(K,1000,[],cam='eye',t_range=(1000,1001))
    with pytest.raises(vlm.VLMUnavailable):
        vlm.observe_window(K,'eye',(1000,1500),[])
