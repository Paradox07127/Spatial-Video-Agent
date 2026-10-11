"""O1 references require an actual, current SoM binding on every cited frame."""
import pytest
from PIL import Image

from app.core import vlm
from app.core.models import EntityRef

CAM = {'id':'eye','w':640,'h':480}
ROW = [1000,7,'person',10,20,30,50,.9]
OBS = [('P1','eye',7,900,1500)]


@pytest.mark.parametrize('rows,owners,window,age', [
    ({7:[ROW]}, OBS + [('P2','eye',7,900,1500)], (1000,1500), 20),
    ({7:[[999,*ROW[1:]]]}, OBS, (1000,1500), 20),
    ({7:[[980,*ROW[1:]]]}, OBS, (900,1500), 16.7),
    ({7:[ROW]}, [('P1','eye',7,0,1000)], (900,1500), 20),
    ({7:[[1000,7,'person',30,20,10,50,.9]]}, OBS, (900,1500), 20),
    ({7:[[1000,7,'person',10,20,float('nan'),50,.9]]}, OBS, (900,1500), 20),
    ({7:[ROW, [1000,7,'person',20,20,40,50,.9]]}, OBS, (900,1500), 20),
    ({7:[[995,*ROW[1:]]]}, [('P2','eye',7,900,1000),('P1','eye',7,1000,1500)], (900,1500), 20),
])
def test_invalid_or_ambiguous_visual_binding_is_absent(rows,owners,window,age):
    assert vlm._frame_bindings(1000,CAM,rows,owners,window=window,max_age_ms=age)=={}


def test_halfopen_reused_track_binds_only_new_owner():
    owners=[('P2','eye',7,900,1000),('P1','eye',7,1000,1500)]
    assert vlm._frame_bindings(1000,CAM,{7:[ROW]},owners)[7][0]=='P1'


@pytest.fixture
def observation(monkeypatch,tmp_path):
    manifest={'workspace':'warehouse','t0_ms':0,'t1_ms':2000,'fps':30,'cams':[CAM], 'assets':{}}
    monkeypatch.setenv('RUNS_DIR',str(tmp_path));monkeypatch.setenv('VLM_URL','http://model');monkeypatch.setenv('VLM_MODEL','model')
    monkeypatch.setattr(vlm,'_manifest',lambda key:manifest)
    monkeypatch.setattr(vlm,'_cache_identity',lambda key:{'source':'s'})
    monkeypatch.setattr(vlm,'_frames',lambda *a,**kw:[(1000,Image.new('RGB',(4,4))),(1250,Image.new('RGB',(4,4)))])
    monkeypatch.setattr(vlm,'_observations',lambda *a:OBS)
    monkeypatch.setattr(vlm,'_boxes',lambda *a:{7:[ROW]})
    fact={'subject_ids':['P1'],'text':'P1 faces the aisle.','frame_indices':[0],'uncertainty':.2}
    monkeypatch.setattr(vlm,'_chat',lambda *a:{'facts':[fact.copy()]})
    return fact


def test_each_cited_frame_must_show_bound_subject(observation):
    observation['frame_indices']=[0,1]
    with pytest.raises(vlm.VLMUnavailable,match='unique current visual binding'):
        vlm.observe_window('wh-test','eye',(1000,1500),[EntityRef(scene_key='wh-test',entity_id='P1',kind='person')])


def test_valid_subject_binding_and_anonymous_scene_fact(observation):
    result=vlm.observe_window('wh-test','eye',(1000,1500),[EntityRef(scene_key='wh-test',entity_id='P1',kind='person')])
    assert result['facts'][0]['subject_ids']==['P1']
    observation.update(subject_ids=[],text='A person faces the aisle.',frame_indices=[0])
    result=vlm.observe_window('wh-test','eye',(1200,1500),[EntityRef(scene_key='wh-test',entity_id='P1',kind='person')])
    assert result['facts'][0]['subject_ids']==[]


def test_named_subject_cannot_evade_binding_with_empty_subject_ids(observation):
    observation['subject_ids']=[]
    with pytest.raises(vlm.VLMUnavailable,match='text subject is not bound'):
        vlm.observe_window('wh-test','eye',(1000,1500),[EntityRef(scene_key='wh-test',entity_id='P1',kind='person')])
