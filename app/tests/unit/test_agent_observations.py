"""Visual answers are useful, bounded, unverified and independent of detection retrieval."""
import pytest
from app.api import agent
from app.core import vlm,tools
from app.tests.unit.test_semantic_repairs import store
from pipeline.common.schema_check import check_sse_sequence


def receipt(key,cam,t_range,actors,**kw):
    lo,hi=t_range
    refs=[{'cam_id':cam,'t_ms':lo}]
    return {'cam_id':cam,'t0_ms':lo,'t1_ms':hi,'input_frame_refs':refs,
            'facts':[{'subject_ids':[a.entity_id for a in actors],
                      'text':'A pedestrian is standing beside a vehicle','frame_refs':refs,'uncertainty':.3}],
            'model_rev':'cosmos-fixture','prompt_rev':'observation-1','input_digest':'sha256:'+'a'*64,'exec_state':'live'}

@pytest.mark.parametrize('q,context,expected_cam,expected_range',[
    ('Describe the scene',{},'ceiling_00',[0,6000]),
    ('Describe P1 in eye_00 at second 2',{},'eye_00',[2000,2001]),
    ('Describe the purple P1 in eye_00',{'t_ms':2000,'t_range':[1000,3000]},'eye_00',[2000,2001]),
])
def test_v2_observation_answers_keep_scope_and_unknown_conditions(store,monkeypatch,q,context,expected_cam,expected_range):
    seen=[]
    def observed(*args,**kw):
        seen.append(args)
        return receipt(*args,**kw)
    monkeypatch.setattr(vlm,'observe_window',observed,raising=False)
    monkeypatch.setattr(tools,'search',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Descriptions must not depend on detection retrieval')))
    frames=list(agent.ask_frames(store,agent.AskRequest(q=q,protocol_version=2,
                context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01',**context})))
    assert check_sse_sequence(frames,protocol_version=2)==[]
    assert seen[0][1:3]==(expected_cam,expected_range)
    obs=next(f['data']['observation'] for f in frames if f['event']=='observation')
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert 'A pedestrian is standing beside a vehicle' in answer['text']
    assert 'unverified' in answer['text'].lower()
    assert answer['measure_refs']==[] and answer['text_spans']==[]
    assert answer['model_refs']==[{'observation_id':obs['observation_id'],'eid':obs['eid'],'field':'facts.0.text'}]
    assert next(f['data']['final'] for f in frames if f['event']=='status')=='UNRESOLVED'
    assert all(e['kind']=='model' for f in frames if f['event']=='tool_result' for e in f['data']['evidence'])
    assert not any(f['event']=='tool_call' and f['data']['name']=='actions.trigger' for f in frames)
    if 'purple' in q:
        assert 'unconsumed:purple' in answer['text']


def test_v2_empty_observation_does_not_invent_a_description(store,monkeypatch):
    def empty(*args,**kw):
        raw=receipt(*args,**kw);raw['facts']=[];return raw
    monkeypatch.setattr(vlm,'observe_window',empty,raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe the scene',protocol_version=2,
                context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'})))
    assert check_sse_sequence(frames,protocol_version=2)==[]
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert not answer['model_refs'] and not answer['measure_refs']
    assert 'unresolved' in answer['text'].lower()


def test_numeric_spans_are_exact_unicode_positions_and_ignore_equal_camera_id():
    ref={'eid':'e_abc','field':'value.count'}
    text,spans=agent.render_cited_text('摄像 Camera 5 observed {{m0}} person tracks; {{n0}}',[ref],[5],['P1'],['.4g'])
    assert spans==[{'start':21,'end':22,**ref}]
    assert text[spans[0]['start']:spans[0]['end']]=='5'
    p={'text':text,'text_spans':spans,'claim_ids':[],'cites':[],'measure_refs':[ref]}
    joined=agent.join_answer_parts([{'text':'🙂 first','claim_ids':[],'cites':[],'measure_refs':[]},p])
    span=joined['text_spans'][0]
    assert joined['text'][span['start']:span['end']]=='5'
    assert span['start']==len('🙂 first ')+21


def test_v2_session_ask_delegates_to_durable_service():
    from types import SimpleNamespace
    job={'session_id':'s_'+'a'*24,'turn_id':'t_'+'b'*24,'job_id':'j_'+'c'*24}
    class Service:
        def submit(self,body):
            self.body=body;return job
        def stream(self,job_id):
            assert job_id==job['job_id'];return iter(['event: done\ndata: {}\n\n'])
    service=Service()
    request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(store=object(),settings=SimpleNamespace(mode='dev'),investigations=service)))
    body=agent.AskRequest(q='Describe the scene',protocol_version=2,session_id=job['session_id'],request_id='req1',expected_revision=0,context={'scene_key':'wh-sdg-fixture-01'})
    response=agent.ask(request,body)
    assert response.headers['X-SVA-Session']==job['session_id']
    assert response.headers['X-SVA-Turn']==job['turn_id'] and response.headers['X-SVA-Job']==job['job_id']
    assert service.body is body


@pytest.mark.parametrize('data',[
    {'protocol_version':1,'session_id':'s_'+'a'*24,'request_id':'req','expected_revision':0},
    {'protocol_version':2,'parent_turn_id':'t_'+'a'*24},
    {'protocol_version':2,'session_id':'s_'+'a'*24},
    {'protocol_version':2,'request_id':'req'},
    {'protocol_version':2,'expected_revision':0},
])
def test_session_metadata_cannot_silently_run_ephemerally(data):
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        agent.AskRequest(q='Describe the scene',context={'workspace':'warehouse'},**data)


def test_persistent_turn_can_inherit_omitted_workspace():
    request=agent.AskRequest(q='Describe the scene',context={},protocol_version=2,
        session_id='s_'+'a'*24,request_id='req',expected_revision=0)
    assert request.context.workspace is None


def test_conflicting_hard_camera_is_unresolved_before_model(store,monkeypatch):
    monkeypatch.setattr(vlm,'observe_window',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Conflicting source must not reach model')),raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe P1 in eye_00',protocol_version=2,
        context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01','cam_id':'ceiling_00'})))
    assert next(f['data']['final'] for f in frames if f['event']=='status')=='UNRESOLVED'
    assert not any(f['event']=='observation' for f in frames)


@pytest.mark.parametrize('source',['wh-sdg-fixture-02','wh-sdg-does-not-exist'])
def test_explicit_source_cannot_describe_different_active_scene(store,monkeypatch,source):
    monkeypatch.setattr(vlm,'observe_window',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Conflicting source must not reach model')),raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe '+source,protocol_version=2,
        context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'})))
    assert next(f['data']['final'] for f in frames if f['event']=='status')=='UNRESOLVED'
    assert not any(f['event']=='observation' for f in frames)


def test_explicit_source_names_bind_unscoped_visual_observations(store,monkeypatch):
    calls=[]
    def observed(*args,**kwargs):
        calls.append(args);return receipt(*args,**kwargs)
    monkeypatch.setattr(vlm,'observe_window',observed,raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe wh-sdg-fixture-01',protocol_version=2,
        context={'workspace':'warehouse'})))
    assert calls[0][0]=='wh-sdg-fixture-01'
    assert any(f['event']=='observation' for f in frames)


def test_unknown_text_camera_cannot_fall_back_to_default_camera(store,monkeypatch):
    monkeypatch.setattr(vlm,'observe_window',lambda *a,**k:(_ for _ in ()).throw(AssertionError('Unknown camera must not reach model')),raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe P1 in eye_98',protocol_version=2,
        context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'})))
    assert next(f['data']['final'] for f in frames if f['event']=='status')=='UNRESOLVED'
    assert not any(f['event']=='observation' for f in frames)
