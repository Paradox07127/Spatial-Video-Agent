"""A near-miss footprint rule must not be judged with a forklift-center distance."""
import pytest

from app.api import agent
from app.core import embed
from app.tests.unit.a5_fakes import fixture_store,install_trace,install_vlm
from pipeline.common.schema_check import check_sse_sequence


@pytest.fixture
def store(tmp_path,monkeypatch):
    s=fixture_store(tmp_path)
    install_trace(monkeypatch);install_vlm(monkeypatch)
    monkeypatch.setattr(embed,'embed_text',lambda q:[0.0]*256)
    moment,events=s.moment,s.events_in
    def positioned(key,t):
        m=moment(key,t);cams=[c['id'] for c in s.manifest(key)['cams']]
        return m.model_copy(update={
            'persons':[p.model_copy(update={'root_world':(0.,0.),'sigma_m':.01,'seen_by':cams,'observed_in':cams}) if p.entity_id=='P1' else p for p in m.persons],
            'forklift':[f.model_copy(update={'x':2.5,'y':0.,'sigma_m':.01,'footprint':[(1.5,-.5),(3.5,-.5),(3.5,.5),(1.5,.5)]}) if f.entity_id=='F1' else f for f in m.forklift]})
    monkeypatch.setattr(s,'moment',positioned)
    monkeypatch.setattr(s,'events_in',lambda *a:[e.model_copy(update={'params':{**e.params,'min_dist_m':1.5,'threshold_m':2.}}) if e.type=='near_miss' else e for e in events(*a)])
    return s


def ask(store,q):
    frames=list(agent.ask_frames(store,agent.AskRequest(q=q,context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'},budget={'tools':16,'seconds':45})))
    assert check_sse_sequence(frames)==[]
    return frames


def final(frames):
    return next(f['data']['final'] for f in frames if f['event']=='status')


def test_plain_near_miss_uses_independent_rule_not_the_center_threshold(store):
    frames=ask(store,'Was there a near miss?')
    assert final(frames)=='SUPPORTED'
    evidence={e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data'].get('evidence',[])}
    center=next(e for e in evidence.values() if e['method']=='ground_distance')
    rule=next(e for e in evidence.values() if e['method']=='event_rule' and e['value']['type']=='near_miss')
    assert center['value']==2.5
    assert rule['value']['params']['min_dist_m']==1.5 and rule['value']['params']['threshold_m']==2.
    assert rule['value']['distance_to']=='forklift_footprint'
    claim=[f['data'] for f in frames if f['event']=='claim'][-1]
    assert claim['predicates'][0]['operator']=='measured' and claim['predicates'][0]['status']=='supported'
    assert claim['predicates'][0]['eids']==[center['eid']]
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert 'forklift center' in answer['text'] and 'not footprint clearance' in answer['text']
    assert any('center distance' in reason for reason in claim['explain']['not_measured'])


def test_center_distance_without_independent_event_does_not_prove_near_miss(store,monkeypatch):
    original=store.events_in
    monkeypatch.setattr(store,'events_in',lambda *a:[e.model_copy(update={'source':'model'}) if e.type=='near_miss' else e for e in original(*a)])
    frames=ask(store,'Was there a near miss?')
    assert final(frames)=='UNRESOLVED'
    assert all(not any(p['operator']=='event' and p['status']=='supported' for p in f['data']['predicates']) for f in frames if f['event']=='claim')


def test_ambiguous_explicit_clearance_threshold_is_retained_not_judged_by_center(store,monkeypatch):
    original=store.events_in
    monkeypatch.setattr(store,'events_in',lambda *a:[e.model_copy(update={'params':{**e.params,'threshold_m':3.}}) if e.type=='near_miss' else e for e in original(*a)])
    frames=ask(store,'Was there a near miss under 2 meters?')
    assert final(frames)=='UNRESOLVED'
    claim=[f['data'] for f in frames if f['event']=='claim'][-1]
    assert (claim['predicates'][0]['operator'],claim['predicates'][0]['value'],claim['predicates'][0]['status'])==('lt',2.,'unknown')
    assert any(p['operator']=='measured' and p['status']=='supported' for p in claim['predicates'])
    assert 'center distance' in next(f['data']['text'] for f in frames if f['event']=='answer')


@pytest.mark.parametrize('threshold,expected',[(2,'SUPPORTED'),(3,'CONTRADICTED')])
def test_explicit_center_comparison_remains_a_separate_condition(store,threshold,expected):
    frames=ask(store,f'Was there a near miss and was the forklift center more than {threshold} meters from the worker?')
    assert final(frames)==expected
    claim=[f['data'] for f in frames if f['event']=='claim'][-1]
    assert claim['predicates'][0]['operator']=='gt' and claim['predicates'][0]['value']==threshold


@pytest.mark.parametrize('q',[
    'How far is P1 from the forklift footprint?',
    'How close did the worker come to the forklift edge?',
    'What was the minimum distance between the worker and forklift footprint?',
    'Was there a near miss with net clearance under 2 meters?',
])
def test_explicit_footprint_quantity_is_not_answered_with_center_distance(store,q):
    frames=ask(store,q)
    assert final(frames)=='UNRESOLVED'
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert 'different quantity' in answer['text'] and answer['measure_refs']==[]
    assert not any(f['event']=='tool_call' and f['data']['name']=='measure' for f in frames)
