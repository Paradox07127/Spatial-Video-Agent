from app.core.policy import plan_next


def test_gain_and_stop_reasons():
    state = dict(aggregate='UNRESOLVED', tools_left=2, seconds_left=5,
                 candidates=[{'cam': 'eye_00', 'gain': 0.4, 'reason': 'visible'},
                             {'cam': 'ceiling_04', 'gain': 0.6, 'reason': 'visible'}])
    assert plan_next(state)['cam'] == 'ceiling_04'
    assert len(plan_next(state)['candidates']) == 2
    assert plan_next({**state, 'tools_left': 0})['stop_reason'] == 'budget_exhausted'
    assert plan_next({**state, 'seconds_left': 0})['stop_reason'] == 'budget_exhausted'
    assert plan_next({**state, 'aggregate': 'SUPPORTED'})['stop_reason'] == 'enough_evidence'
    assert plan_next({**state, 'candidates': []})['stop_reason'] == 'no_new_info'
