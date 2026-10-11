"""Camera, source and subject bindings are honoured or stay unknown; v2 description and link answers never crash."""
import pytest

from app.api import agent
from app.core import vlm
from app.tests.unit.a5_fakes import L1_KEY
from app.tests.unit.test_agent_observations import receipt
from app.tests.unit.test_places import store as places_store
from app.tests.unit.test_semantic_repairs import store
from pipeline.common.schema_check import check_sse_sequence


def run(store,q,protocol_version=1,**context):
    frames=list(agent.ask_frames(store,agent.AskRequest(q=q,context=context,protocol_version=protocol_version,
                                                       budget={'tools':16,'seconds':45})))
    assert check_sse_sequence(frames,protocol_version=protocol_version)==[]
    return frames


def final(frames):
    return next(f['data']['final'] for f in frames if f['event']=='status')


def predicates(frames):
    return [p for f in frames if f['event']=='claim' for p in f['data']['predicates']]


def calls(frames,name):
    return [f['data']['args'] for f in frames if f['event']=='tool_call' and f['data']['name']==name]


@pytest.mark.parametrize('available',[True,False])
def test_v2_street_description_does_not_crash(store,monkeypatch,available):
    def observed(*args,**kw):
        if not available:
            raise vlm.VLMUnavailable('offline')
        return receipt(*args,**kw)
    monkeypatch.setattr(vlm,'observe_window',observed,raising=False)
    frames=run(store,'Describe this scene',2,workspace='street',scene_key=L1_KEY)
    assert final(frames)=='UNRESOLVED'
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert ('A pedestrian is standing beside a vehicle' in answer['text'])==available


@pytest.mark.parametrize('q',['Link P1 in ceiling_04 and eye_00','Link P1 between ceiling_04 and eye_00'])
def test_v2_link_answers_without_crashing(store,q):
    frames=run(store,q,2,workspace='warehouse',scene_key='wh-sdg-fixture-01')
    assert final(frames)=='SUPPORTED'
    assert any(f['event']=='done' for f in frames)


def test_v2_instant_observation_covers_one_frame_period(store,monkeypatch):
    seen=[]
    def observed(*args,**kw):
        seen.append(args);return receipt(*args,**kw)
    monkeypatch.setattr(vlm,'observe_window',observed,raising=False)
    run(store,'Describe P1 in eye_00 at 1.501 seconds',2,workspace='warehouse',scene_key='wh-sdg-fixture-01')
    assert seen[0][1:3]==('eye_00',[1501,1535])


def test_only_a_named_camera_constrains_the_closest_distance(store):
    ambient=run(store,'How far is P1 from F1?',workspace='warehouse',scene_key='wh-sdg-fixture-01',cam_id='ceiling_00')
    assert final(ambient)=='SUPPORTED' and all(c.get('cam') is None for c in calls(ambient,'measure'))
    named=run(store,'How far is P1 from F1 in ceiling_00?',workspace='warehouse',scene_key='wh-sdg-fixture-01',cam_id='ceiling_00')
    assert final(named)=='UNRESOLVED'
    assert calls(named,'measure') and all(c.get('cam')=='ceiling_00' for c in calls(named,'measure'))


@pytest.mark.parametrize('q,workspace,cam',[('How many people were tracked?','street','no_such_cam'),
                                            ('Where is P1 at 2 seconds?','warehouse','missing_cam')])
def test_unknown_context_camera_without_scene_is_unresolved(store,q,workspace,cam):
    frames=run(store,q,workspace=workspace,cam_id=cam)
    assert final(frames)=='UNRESOLVED'
    assert any(p['status']=='unknown' for p in predicates(frames))


def test_context_camera_selects_blind_spot_camera(store):
    frames=run(store,'Is anyone in the blind spot?',workspace='warehouse',scene_key='wh-sdg-fixture-01',cam_id='ceiling_00')
    assert [c['cam'] for c in calls(frames,'blind_presence')]==['ceiling_00']


def test_blind_spot_for_non_person_subject_is_unresolved(store):
    frames=run(store,'Is there a forklift in the ceiling_00 blind spot?',workspace='warehouse',scene_key='wh-sdg-fixture-01')
    assert final(frames)=='UNRESOLVED'
    assert any(p['status']=='unknown' for p in predicates(frames))


@pytest.mark.parametrize('q,value',[('Is the worker less than 0.1 meters from the forklift in the ceiling_00 blind spot?',0.1),
                                    ('Is anyone less than 2 meters from the wall in the ceiling_00 blind spot?',2.0)])
def test_blind_spot_keeps_distance_requirement_unknown(store,q,value):
    frames=run(store,q,workspace='warehouse',scene_key='wh-sdg-fixture-01')
    assert final(frames)=='UNRESOLVED'
    assert any(p['operator']=='lt' and p['value']==value and p['status']=='unknown' for p in predicates(frames))


def test_facade_keeps_distance_requirement_unknown(places_store):
    frames=run(places_store,'At Queen and Spadina, what is the building on the right within 5 meters?',workspace='fleet')
    assert final(frames)=='UNRESOLVED'
    assert any(p['operator']=='lt' and p['value']==5.0 and p['status']=='unknown' for p in predicates(frames))


def test_gps_position_needs_one_bound_trip(store,monkeypatch):
    scenes=store.list_scenes
    monkeypatch.setattr(store,'list_scenes',lambda *a,**k:sorted(scenes(*a,**k),key=lambda s:s['scene_key']!='fl-pie-fixture-02'))
    frames=run(store,'Where was the car?',workspace='fleet',t_ms=2000)
    assert final(frames)=='UNRESOLVED'
    assert not calls(frames,'gps_position')


def test_braking_requirement_accepts_hard_brake_events(store,monkeypatch):
    # bind the ego brake event to the retrieved card's actors so the event predicate itself is supported
    events_in=store.events_in
    monkeypatch.setattr(store,'events_in',lambda *a,**k:[e.model_copy(update={'actor_ids':['P1','V1']}) if e.type=='hard_brake' else e
                                                       for e in events_in(*a,**k)])
    frames=run(store,'When did the car brake?',workspace='fleet',scene_key='fl-pie-fixture-01')
    assert any(p['operator']=='event' and p['status']=='supported' for p in predicates(frames))
    assert not any(p['operator']=='requested_action:braking' and p['status']=='unknown' for p in predicates(frames))


def test_active_camera_does_not_constrain_a_world_position(store):
    frames=run(store,'Where is P1 at 4.6 seconds?',workspace='warehouse',scene_key='wh-sdg-fixture-01',cam_id='ceiling_00')
    assert final(frames)=='SUPPORTED' and not [p for p in predicates(frames) if p['operator']=='camera_constraint']
    named=run(store,'Where is P1 in ceiling_00 at 4.6 seconds?',workspace='warehouse',scene_key='wh-sdg-fixture-01',cam_id='ceiling_00')
    assert final(named)=='UNRESOLVED' and [p for p in predicates(named) if p['operator']=='camera_constraint']
