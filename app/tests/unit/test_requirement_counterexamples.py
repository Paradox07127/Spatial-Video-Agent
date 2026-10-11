"""The same assertions can be run against the pre-fix source snapshot."""
import pytest
from app.core import llm
from app.core.investigation_context import apply_constraints
from app.core.task_spec import build_task_spec
from app.tests.unit.test_semantic_repairs import store,ask,final

@pytest.mark.parametrize('q,expected',[
 ('How far is P1 from F1 at 2 seconds?',(2000,2001)),
 ('How far is P1 from F1 after second 2 before second 1?',(2001,1000)),
 ('How far is P1 from F1 at second 2 at 3 seconds?',(3000,2001)),
 ('How far is P1 from F1 from 1 to 8 seconds between 3 and 5 seconds?',(3000,5000)),
])
def test_all_time_conditions_survive(q,expected):
 ctx={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'}
 parsed,reason=apply_constraints(q,ctx,llm.rule_parse(q,ctx))
 assert parsed.t_range==expected
 assert reason==('conflicting_time_constraints' if expected[0]>=expected[1] else None)

@pytest.mark.parametrize('descriptor',['purple','stationary','seated','magenta','injured','distracted','zigzagging','unusual-patterned'])
def test_open_vocabulary_condition_is_retained(descriptor):
 parsed=llm.rule_parse(f'How far is the {descriptor} worker from F1?',{'workspace':'warehouse'})
 assert any(c.startswith('unconsumed:') for c in parsed.constraints)

@pytest.mark.parametrize('action',['crossing','braking'])
def test_individual_combined_actions_are_retained(action):
 parsed=llm.rule_parse(f'How far is P1 from F1 while {action}?',{'workspace':'warehouse'})
 assert 'requested_action:'+action in parsed.constraints


@pytest.mark.parametrize('q',[
 'Please show the distance from P1 to F1',
 'How far away is P1 from F1?',
 'How far apart are P1 and F1?',
])
def test_distance_paraphrases_preserve_measured_answer(store,q):
 canonical=llm.rule_parse('How far is P1 from F1?',{'workspace':'warehouse'})
 parsed=llm.rule_parse(q,{'workspace':'warehouse'})
 assert parsed.constraints==canonical.constraints
 assert build_task_spec(q).requirements==build_task_spec('How far is P1 from F1?').requirements
 assert final(ask(store,q))=='SUPPORTED'


def test_within_seconds_bounds_the_event_search(store):
 q='Did the worker dodge within 3 seconds?'
 ctx={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'}
 parsed=llm.rule_parse(q,ctx)
 assert parsed.t_range==(0,3000)
 assert build_task_spec(q).time_ranges==((0,3000),)
 parsed,reason=apply_constraints(q,ctx,parsed)
 assert parsed.t_range==(0,3000) and reason is None
 assert final(ask(store,q))=='NO_MATCH'


@pytest.mark.parametrize('condition',['left side','right side','within 3','until 3','17','3.5'])
def test_unmatched_conditions_stay_unknown(store,condition):
 q='Find the worker on the '+condition
 parsed=llm.rule_parse(q,{'workspace':'warehouse'})
 assert all('unconsumed:'+word in parsed.constraints for word in condition.split())
 frames=ask(store,q)
 assert final(frames)=='UNRESOLVED'
 assert any(p['operator'].startswith('unconsumed:') and p['status']=='unknown'
            for f in frames if f['event']=='claim' for p in f['data']['predicates'])
