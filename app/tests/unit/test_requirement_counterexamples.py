"""The same assertions can be run against the pre-fix source snapshot."""
import pytest
from app.core import llm
from app.core.investigation_context import apply_constraints

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
