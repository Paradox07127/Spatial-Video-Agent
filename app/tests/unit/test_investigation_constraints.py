import copy

import pytest

from app.api.search import SearchRequest, retrieve
from app.core import llm, tools
from app.core.investigation_context import apply_constraints
from app.core.models import EntityRef
from app.tests.unit.a5_fakes import fixture_store
from app.tests.unit.test_semantic_repairs import store,ask,final

KEY = 'wh-sdg-fixture-01'


def constrained(q, **context):
    ctx = {'workspace':'warehouse','scene_key':KEY,**context}
    return apply_constraints(q,ctx,llm.rule_parse(q,ctx))


@pytest.mark.parametrize('q',[
    'Find P1 at 2 seconds or at 5 seconds',
    'Find P1 or P2',
])
def test_disjunction_remains_an_unknown_requirement(store,q):
    ctx={'workspace':'warehouse','scene_key':KEY}
    parsed=llm.rule_parse(q,ctx)
    assert 'disjunction' in parsed.constraints
    assert parsed.t_range is None and parsed.entities==[]
    parsed,reason=apply_constraints(q,ctx,parsed.model_copy(update={'constraints':[],
        'entities':['P1','P2'],'t_range':(5000,2001)}))
    assert reason=='disjunction' and 'disjunction' in parsed.constraints
    assert parsed.t_range is None and parsed.entities==[]
    frames=ask(store,q)
    assert final(frames)=='UNRESOLVED'
    claims=[f['data'] for f in frames if f['event']=='claim']
    assert any(c['explain'].get('note')=='disjunction' for c in claims)
    assert any(p['operator']=='disjunction' and p['status']=='unknown'
               for c in claims for p in c['predicates'])
    assert not any(f['event']=='tool_call' for f in frames)


def test_intersects_paused_frame_text_and_context_without_widening():
    parsed, reason = constrained('Find the worker after 7 seconds',t_ms=7100,t_range=(7000,9000),selection=['P2'])
    assert reason is None and parsed.t_range==(7100,7101) and parsed.entities==['P2']
    _, reason = constrained('Find P1 at 4 seconds',t_ms=7000)
    assert reason=='conflicting_time_constraints'
    _, reason = constrained('Find P1',selection=['P2'])
    assert reason=='conflicting_entity_constraints'
    parsed, reason = constrained('How close did the worker come to the forklift?',selection=['P2'])
    assert reason is None and parsed.entities==['P2','forklift']


@pytest.mark.parametrize('q,context',[
    ('When did the worker dodge?',{'selection':['P2'],'t_ms':7000}),
    ('When did the worker dodge after 7 seconds?',{}),
])
def test_search_never_returns_another_person_or_time(tmp_path,monkeypatch,q,context):
    monkeypatch.delenv('EMBED_URL',raising=False)
    s=fixture_store(tmp_path)
    _,result=retrieve(s,SearchRequest(q=q,workspace='warehouse',scene_key=KEY,**context))
    assert result.envelope.output['moments']==[]
    if context:
        assert result.envelope.output['abstain']=='UNRESOLVED'


def test_search_conflicting_constraints_are_unknown(tmp_path):
    s=fixture_store(tmp_path)
    _,result=retrieve(s,SearchRequest(q='Find P1',workspace='warehouse',scene_key=KEY,selection=['P2']))
    assert result.envelope.output['abstain']=='UNRESOLVED'
    assert result.envelope.output['reason']=='conflicting_entity_constraints'


def test_model_events_keep_model_provenance(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    original=s.events_in
    monkeypatch.setattr(s,'events_in',lambda *args:[e.model_copy(update={'source':'model'}) for e in original(*args)])
    result=tools.events(s,KEY,4300,5900)
    assert result.evidence and all(e.kind=='model' for e in result.evidence)
    monkeypatch.delenv('EMBED_URL',raising=False)
    _,result=retrieve(s,SearchRequest(q='When did the worker dodge?',workspace='warehouse',scene_key=KEY))
    events=[e for e in result.evidence if e.method.startswith('event_')]
    assert events and all(e.kind=='model' and e.value['source']=='model' for e in events)


def replace_asset(monkeypatch,store,edit):
    original=store.asset_json
    def asset(key,path):
        doc=copy.deepcopy(original(key,path))
        edit(path,doc)
        return doc
    monkeypatch.setattr(store,'asset_json',asset)


def test_single_sample_does_not_certify_a_second_or_other_missing_person(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    def sparse(path,doc):
        if path=='skeleton.json':
            doc['fps']=15
            for p in doc['persons'].values():
                for field in ('t_ms','root_world','root_sigma_m','root_method','xyz'):
                    p[field]=p[field][:1]
    replace_asset(monkeypatch,s,sparse)
    result=tools.blind_presence(s,KEY,'ceiling_04',0,1000)
    assert result.envelope.coverage==pytest.approx(.067)
    assert result.envelope.output['checked_ms']==67
    assert result.envelope.gaps==[(67,1000,'unknown')]


def test_blind_selection_and_zone_are_applied(tmp_path):
    s=fixture_store(tmp_path)
    result=tools.blind_presence(s,KEY,'ceiling_00',0,10000,selection=['P2'])
    assert [(e.value['entity_id'],e.t0_ms,e.t1_ms,e.value['seen_by']) for e in result.evidence]==[
        ('P2',3600,6000,['ceiling_04','eye_00']),
        ('P2',6000,7000,['ceiling_04']),
        ('P2',7000,9300,['ceiling_04','eye_00']),
    ]
    assert result.envelope.coverage==1
    result=tools.blind_presence(s,KEY,'ceiling_00',0,10000,selection=['P2'],zone=[(100,100),(101,100),(101,101)])
    assert result.evidence==[] and result.envelope.coverage==1


def test_moment_and_closest_reject_internal_track_gap(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    def sparse(path,doc):
        if path=='skeleton.json':
            for p in doc['persons'].values():
                for field in ('t_ms','root_world','root_sigma_m','root_method','xyz'):
                    p[field]=[p[field][0],p[field][-1]]
        if path=='forklift.json':
            for f in doc['forklifts']:
                f['track']=[f['track'][0],f['track'][-1]]
    replace_asset(monkeypatch,s,sparse)
    moment=s.moment(KEY,4500)
    assert moment.persons==[] and moment.forklift==[]
    assert tools.closest_approach(s,[s.manifest(KEY)],(4400,4600)).envelope.output['closest'] is None


def test_closest_binds_person_and_measure_records_only_real_detections(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    result=tools.closest_approach(s,[s.manifest(KEY)],selection=['P2','F1'])
    assert result.envelope.output['closest']['a']=='P2'
    assert result.evidence[0].value['distance_to']=='forklift_center'
    def missing_boxes(path,doc):
        if path.startswith('boxes/'):
            doc['rows']=[]
    replace_asset(monkeypatch,s,missing_boxes)
    a=EntityRef(scene_key=KEY,entity_id='P1',kind='person')
    b=EntityRef(scene_key=KEY,entity_id='F1',kind='forklift')
    result=tools.measure(s,KEY,4600,a,b,'distance')
    assert result.evidence and result.evidence[0].observation_ids==[]
    assert result.envelope.output['observation_role']=='aligned_detection_not_position_inputs'


def test_closest_rejects_stale_forklift_inside_its_overall_track_span(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    def sparse(path,doc):
        if path=='forklift.json':
            for f in doc['forklifts']:
                f['track']=[f['track'][0],f['track'][-1]]
    replace_asset(monkeypatch,s,sparse)
    result=tools.closest_approach(s,[s.manifest(KEY)],(4400,4600))
    assert result.envelope.output['closest'] is None
    assert result.envelope.output['scene_coverage'][KEY]['coverage']==0
    assert {g[2] for g in result.envelope.gaps}=={'stale_forklift_sample'}


def test_continuing_event_is_not_a_start_in_requested_window(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    monkeypatch.delenv('EMBED_URL',raising=False)
    _,result=retrieve(s,SearchRequest(q='When did the worker start to dodge after 5 seconds?',workspace='warehouse',scene_key=KEY))
    assert result.envelope.output['moments']==[]
