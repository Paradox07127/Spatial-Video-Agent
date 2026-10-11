"""Action qualifiers and selected regions cannot disappear in scalar or visual paths."""
import pytest
from app.tests.unit.test_semantic_repairs import store, ask, final


@pytest.mark.parametrize('q',[
    'How far is walking P1 from F1 at second 2?',
    'How far is P1 walking from F1 at second 2?',
    'How far is passing P1 from F1 at second 2?',
])
def test_unverified_action_qualifier_survives_direct_distance(store,q):
    frames=ask(store,q)
    assert final(frames)=='UNRESOLVED'
    assert any(p['status']=='unknown' and p['operator'] in {'action_modifier:walking','passing'}
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])


def test_selected_zone_is_explicitly_unverified_in_full_frame_observations(store,monkeypatch):
    from app.api import agent
    from app.core import vlm
    from app.tests.unit.test_agent_observations import receipt
    monkeypatch.setattr(vlm,'observe_window',receipt,raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe the scene',protocol_version=2,
        context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01',
                 'zone':[[-1,-1],[1,-1],[1,1]]})))
    assert any(p['operator']=='visual_zone' and p['status']=='unknown'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])
    answer=next(f['data']['text'] for f in frames if f['event']=='answer')
    assert 'full camera frames' in answer and 'selected zone' in answer


@pytest.mark.parametrize('introducer',['At','at','Near','near','Near the','near the','At the'])
def test_consumed_named_place_span_keeps_capitalized_introducers(introducer):
    from app.core.task_spec import build_task_spec
    task=build_task_spec(introducer+' Queen and Spadina, what is the building on the right?')
    assert not [r for r in task.requirements if r.kind=='unconsumed']


@pytest.mark.parametrize('q,workspace,context',[
    ('How far is P1 from F1 at second 2 near Alpha?','warehouse',{}),
    ('How fast is F1 at second 2 near Alpha?','warehouse',{}),
    ('Is there anyone in the ceiling_00 blind spot during 10 seconds near Alpha?','warehouse',{}),
    ('How far is P1 from the recording car at second 2 near Alpha?','fleet',{'scene_key':'fl-pie-fixture-01'}),
    ('How fast is the recording car at second 2 near Alpha?','fleet',{'scene_key':'fl-pie-fixture-01'}),
    ('How many people near Alpha?','street',{}),
])
def test_unverified_named_place_survives_non_geographic_fast_paths(store,q,workspace,context):
    frames=ask(store,q,workspace,**context)
    assert final(frames)=='UNRESOLVED'
    assert any(p['operator']=='place_constraint' and p['status']=='unknown'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])


def test_model_observation_cannot_silently_drop_a_named_place(store,monkeypatch):
    from app.api import agent
    from app.core import vlm
    from app.tests.unit.test_agent_observations import receipt
    monkeypatch.setattr(vlm,'observe_window',receipt,raising=False)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Describe the scene near Alpha',protocol_version=2,
        context={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'})))
    assert any(p['operator']=='place_constraint' and p['status']=='unknown' and p['value']=='Alpha'
               for f in frames if f['event']=='claim' for p in f['data']['predicates'])
    answer=next(f['data']['text'] for f in frames if f['event']=='answer')
    assert 'requested place was not verified' in answer
