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
    monkeypatch.setattr(vlm,'_frames',lambda *a:frames)
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
