"""Runtime counterexamples: answer scope must match the evidence actually checked."""
import pytest

from app.api import agent
from app.core import llm
from app.tests.unit.test_agent import setup, corpus, ask, of, BLIND_Q, WH_MAIN
from pipeline.common.schema_check import check_sse_sequence


def run(store,q,context):
    frames=ask(store,q,context,{'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[]
    return frames


@pytest.mark.parametrize('q,expected',[
    (BLIND_Q,'SUPPORTED'),
    ('Is there anyone where ceiling_04 cannot see, from 4 to 10 seconds?','CONTRADICTED'),
    ('Is there anyone where eye_00 cannot see, from 6 to 7 seconds?','UNRESOLVED'),
])
def test_blind_answer_limits_population_and_sampling(setup,q,expected):
    _,store=setup
    frames=run(store,q,WH_MAIN['context'])
    assert of(frames,'status')[0]['final']==expected
    text=' '.join(a['text'] for a in of(frames,'answer')).lower()
    assert 'frame by frame' not in text and 'nobody is present' not in text
    assert 'known people' in text and 'sample' in text and 'floor' in text
    assert 'unobserved people' in text and 'between samples' in text
    assert all(c['explain']['not_measured'] for c in of(frames,'claim'))


def test_selected_blind_answer_identifies_limited_population(setup):
    _,store=setup
    frames=run(store,BLIND_Q,{**WH_MAIN['context'],'selection':['P2']})
    text=' '.join(a['text'] for a in of(frames,'answer')).lower()
    assert 'selected people' in text and 'unobserved people' in text


def test_summary_alias_does_not_take_identity_refusal(setup,monkeypatch):
    _,store=setup
    monkeypatch.setenv('LLM_URL','http://offline/v1')
    calls=[]
    monkeypatch.setattr(llm,'complete_json',lambda *a,**k:calls.append(k) or {'template':'unsupported','entities':[]})
    frames=run(store,'Summarize this moment',{**WH_MAIN['context'],'t_ms':4600})
    assert llm.rule_parse('Summarize this moment',WH_MAIN['context']).template=='what_happened'
    assert not calls
    assert of(frames,'status')[0]['final']!='UNSUPPORTED'
    assert of(frames,'tool_call')
    assert not any('Identity, intent' in a['text'] for a in of(frames,'answer'))


def test_unrecognized_question_refusal_does_not_claim_identity(setup):
    _,store=setup
    frames=run(store,'Banana',WH_MAIN['context'])
    assert of(frames,'status')[0]['final']=='UNSUPPORTED'
    assert 'Identity, intent' not in of(frames,'answer')[0]['text']
    assert not of(frames,'tool_call')


def test_pass_close_requires_motion_proof_and_retains_independent_distance(corpus):
    frames=run(corpus,'When does a forklift pass close to a worker?',{'workspace':'warehouse','scope':'corpus'})
    assert of(frames,'status')[0]['final']=='UNRESOLVED'
    l3=next(c for c in of(frames,'claim')[::-1] if c['evidence_level']=='L3')
    assert any(p['operator']=='passing' and p['status']=='unknown' for p in l3['predicates'])
    assert any(p['operator']=='measured' and p['status']=='supported' for p in l3['predicates'])
    answers=of(frames,'answer')
    assert all('Not measured: measured' not in a['text'] for a in answers)
    assert 'predicate' not in answers[0]['text']
    l3_answer=next(a for a in answers if a.get('evidence_level')=='L3')
    assert 'passing' in l3_answer['text'] and ' s' in l3_answer['text'] and ' m' in l3_answer['text']
    assert {r['field'] for r in l3_answer['measure_refs']} >= {'value','sigma','t0_ms'}


def test_camera_free_vehicle_gps_is_in_answer_with_real_sample_time(setup,monkeypatch):
    _,store=setup
    original=store.manifest
    monkeypatch.setattr(store,'manifest',lambda key:{**original(key),'cams':[]} if key=='fl-pie-fixture-01' else original(key))
    frames=run(store,'Where is this car?',{'workspace':'fleet','scene_key':'fl-pie-fixture-01','t_ms':1000})
    assert of(frames,'status')[0]['final']=='SUPPORTED'
    answer=of(frames,'answer')[0]
    assert 'GPS' in answer['text'] and 'latitude' in answer['text'] and 'longitude' in answer['text']
    assert 'uncertainty unavailable' in answer['text']
    assert {r['field'] for r in answer['measure_refs']}=={'value.lat','value.lon','value.t_ms'}
    evidence={e['eid']:e for t in of(frames,'tool_result') for e in t['evidence']}
    assert all(evidence[r['eid']]['method']=='gps_position' for r in answer['measure_refs'])


def test_facade_poi_fallback_is_not_a_verified_building_name(setup,monkeypatch):
    from app.core import tools
    _,store=setup
    original=tools.facade
    def fallback(*a,**kw):
        result=original(*a,**kw)
        value=result.envelope.output['building']
        assert value is not None
        value.update(name=None,addr=None,pois=[{'name':'Fixture shop'}],label='Fixture shop')
        for ev in result.evidence:
            ev.value.update(name=None,addr=None,pois=[{'name':'Fixture shop'}],label='Fixture shop')
        return result
    monkeypatch.setattr(tools,'facade',fallback)
    frames=run(store,'At Queen and Spadina, what is the building on the right?',{'workspace':'fleet'})
    assert of(frames,'status')[0]['final']=='SUPPORTED'
    text=of(frames,'answer')[0]['text']
    assert 'POI label Fixture shop' in text and 'does not verify' in text
    assert 'camera-visible building name or sign' in of(frames,'claim')[-1]['explain']['not_measured']
