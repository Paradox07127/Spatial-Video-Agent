"""Explicit existing spatial data must not be replaced by unrelated event/visibility proof."""
import pytest
from app.api import agent
from app.core import embed,tools
from app.tests.unit.a5_fakes import fixture_store,install_trace,install_vlm
from pipeline.common.schema_check import check_sse_sequence

KEY='wh-sdg-fixture-01'

@pytest.fixture
def store(tmp_path,monkeypatch):
    s=fixture_store(tmp_path);install_trace(monkeypatch);install_vlm(monkeypatch)
    monkeypatch.setattr(embed,'embed_text',lambda q:[0.0]*256)
    monkeypatch.delenv('LLM_URL',raising=False)
    return s

def ask(store,q,**ctx):
    frames=list(agent.ask_frames(store,agent.AskRequest(q=q,context={'workspace':'warehouse','scene_key':KEY,**ctx},budget={'tools':16,'seconds':45})))
    assert check_sse_sequence(frames)==[]
    status=next(f['data']['final'] for f in frames if f['event']=='status')
    answer=next(f['data'] for f in frames if f['event']=='answer')
    evidence={e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data'].get('evidence',[])}
    return status,answer,evidence,frames


def test_explicit_distance_without_event_uses_observed_pair_and_names_minimum(store,monkeypatch):
    monkeypatch.setattr(store,'events_in',lambda *a:[])
    status,answer,evidence,frames=ask(store,'How far is P2 from F1?')
    assert status=='SUPPORTED' and 'minimum observed distance' in answer['text']
    assert [f['data']['name'] for f in frames if f['event']=='tool_call']==['closest_approach','measure']
    assert all(evidence[r['eid']]['kind']=='measure' for r in answer['measure_refs'])


@pytest.mark.parametrize('q', ['How far is P2 from F1?','What is the distance between P2 and F1?'])
def test_explicit_distance_paused_time_is_not_widened(store,q):
    status,answer,evidence,frames=ask(store,q,t_ms=7000)
    assert status=='SUPPORTED'
    measured=next(e for e in evidence.values() if e['method']=='ground_distance')
    assert measured['t0_ms']==7000
    claim=[f['data'] for f in frames if f['event']=='claim'][-1]
    assert {s['entity_id'] for p in claim['predicates'] for s in p['subject_refs']}=={'P2','F1'}


@pytest.mark.parametrize('q',['How far is P2 from every forklift F1?','How far is P2 from the red F1?','How far is P2 not from F1?'])
def test_direct_distance_does_not_discard_unverified_conditions(store,q):
    assert ask(store,q)[0]!='SUPPORTED'


@pytest.mark.parametrize('q',['How fast is F1 moving?','What is the speed of F1?','What was the forklift speed?'])
def test_scalar_speed_at_time_uses_track_measurement(store,q,monkeypatch):
    if 'moving' in q:
        measure=tools.measure
        def precise(*a,**kw):
            result=measure(*a,**kw)
            result.evidence=[e.model_copy(update={'sigma':.01}) for e in result.evidence]
            return result
        monkeypatch.setattr(tools,'measure',precise)
    status,answer,evidence,frames=ask(store,q,t_ms=7000)
    assert status=='SUPPORTED' and 'm/s' in answer['text'] and '±' in answer['text']
    assert {evidence[r['eid']]['method'] for r in answer['measure_refs']}=={'track_speed'}
    assert [r['field'] for r in answer['measure_refs']]==['value','sigma','t0_ms']
    assert not any(f['event']=='tool_call' and f['data']['name']=='situation' for f in frames)


@pytest.mark.parametrize('q',['How fast is F1 moving?','What is the speed of F1?','What was the forklift speed?','Where is P2?'])
def test_missing_measurement_time_is_explicit_unknown(store,q):
    status,answer,evidence,frames=ask(store,q)
    assert status=='UNRESOLVED' and 'timestamp' in answer['text'].lower()
    assert 'Identity' not in answer['text'] and answer['measure_refs']==[]


@pytest.mark.parametrize('tm',[2100,7000])
def test_location_is_true_world_ground_point_with_source_and_numeric_refs(store,tm):
    status,answer,evidence,frames=ask(store,'Where is P2?',t_ms=tm)
    assert status=='SUPPORTED' and 'scene world ground' in answer['text'] and '±' in answer['text']
    measured=next(e for e in evidence.values() if e['method']=='ground_position')
    p=next(p for p in store.moment(KEY,tm).persons if p.entity_id=='P2')
    assert (measured['value']['x_m'],measured['value']['y_m'])==p.root_world
    assert measured['value']['source']==p.root_method and measured['value']['coordinate_frame']=='scene_world_ground'
    assert {r['field'] for r in answer['measure_refs']}=={'value.x_m','value.y_m','sigma','value.t_s'}
    assert not any(f['event']=='tool_call' and f['data']['name']=='visibility' for f in frames)


def test_visibility_without_valid_root_never_proves_position(store,monkeypatch):
    original=store.moment
    monkeypatch.setattr(store,'moment',lambda *a:original(*a).model_copy(update={'persons':[]}))
    status,answer,evidence,frames=ask(store,'Where is P2?',t_ms=7000)
    assert status=='UNRESOLVED' and 'position' in answer['text'].lower() and not answer['measure_refs']


def test_location_zone_cannot_be_ignored(store):
    status,answer,evidence,frames=ask(store,'Where is P2?',t_ms=7000,zone=[(100,100),(101,100),(101,101)])
    assert status=='UNRESOLVED' and answer['measure_refs']==[]


def test_scalar_speed_does_not_transfer_moving_worker_to_forklift(store):
    status,answer,evidence,frames=ask(store,'What is the speed of F1 beside the moving worker?',t_ms=7000)
    assert status=='UNRESOLVED'


def test_speed_comparison_is_not_discarded_by_scalar_measurement(store):
    status,answer,evidence,frames=ask(store,'Was the speed of F1 above 2 m/s?',t_ms=7000)
    assert status=='UNRESOLVED' and 'comparison' in answer['text']


def test_scalar_motion_uncertainty_retains_speed_but_not_full_support(store):
    status,answer,evidence,frames=ask(store,'How fast is F1 moving?',t_ms=7000)
    assert status=='UNRESOLVED' and 'm/s' in answer['text'] and answer['measure_refs']
    claim=[f['data'] for f in frames if f['event']=='claim'][-1]
    assert any(p['operator']=='measured_speed' and p['status']=='supported' for p in claim['predicates'])
    assert any(p['operator']=='action_modifier:moving' and p['status']=='unknown' for p in claim['predicates'])


def test_local_entity_ids_cannot_choose_a_minimum_across_unbound_scenes(store,monkeypatch):
    scenes=store.list_scenes
    def multiple(workspace=None):
        rows=scenes(workspace)
        wh=next((s for s in rows if s['workspace']=='warehouse'),None)
        return [*rows,{**wh,'scene_key':'wh-another-scene'}] if wh else rows
    monkeypatch.setattr(store,'list_scenes',multiple)
    status,answer,evidence,frames=ask(store,'How far is P2 from F1?',scene_key=None)
    assert status=='UNRESOLVED' and 'source scene' in answer['text']
    assert not any(f['event']=='tool_call' and f['data']['name']=='closest_approach' for f in frames)


def test_scalar_speed_does_not_discard_an_unmeasured_spatial_relation(store):
    assert ask(store,'What is the speed of F1 beside the worker?',t_ms=7000)[0]=='UNRESOLVED'


@pytest.mark.parametrize('q,condition',[('What is the speed of the red F1?','attribute:red'),('What is the speed of F1 for 5 seconds?','duration')])
def test_measured_speed_retains_the_unverified_qualifier_in_the_answer(store,q,condition):
    status,answer,evidence,frames=ask(store,q,t_ms=7000)
    assert status=='UNRESOLVED' and condition.replace('_',' ') in answer['text']
    assert 'm/s' in answer['text'] and answer['measure_refs']


def test_person_without_scalar_speed_data_is_unknown_not_no_match(store):
    status,answer,evidence,frames=ask(store,'What is the speed of P2?',t_ms=7000)
    assert status=='UNRESOLVED' and 'missing speed' in answer['text'] and answer['measure_refs']==[]


def test_textual_position_time_uses_the_actual_ground_point(store):
    status,answer,evidence,frames=ask(store,'Where is P2 at 2.1 seconds?')
    assert status=='SUPPORTED'
    assert next(e for e in evidence.values() if e['method']=='ground_position')['t0_ms']==2100


def test_how_fast_never_becomes_l1_presence_proof(store,tmp_path):
    from app.tests.unit.a5_fakes import fixture_l1_store,L1_KEY
    assert ask(fixture_l1_store(tmp_path/'l1'),'How fast is the person?',workspace='street',scene_key=L1_KEY)[0]=='UNRESOLVED'


def test_how_fast_fleet_is_not_an_event_presence_answer(store):
    assert ask(store,'How fast is this car?',workspace='fleet',scene_key='fl-pie-fixture-01')[0]=='UNRESOLVED'


@pytest.mark.parametrize('q',['What is the speed of F1?','How fast is F1 moving?'])
def test_missing_speed_uncertainty_keeps_value_without_sigma_or_motion_proof(store,monkeypatch,q):
    original=tools.measure
    def no_sigma(*a,**kw):
        result=original(*a,**kw)
        result.evidence=[e.model_copy(update={'sigma':None}) for e in result.evidence]
        return result
    monkeypatch.setattr(tools,'measure',no_sigma)
    status,answer,evidence,frames=ask(store,q,t_ms=7000)
    assert status=='UNRESOLVED' and 'uncertainty unavailable' in answer['text']
    assert [r['field'] for r in answer['measure_refs']]==['value','t0_ms']
    assert all(p['status']=='unknown' for p in [f['data'] for f in frames if f['event']=='claim'][-1]['predicates'])
