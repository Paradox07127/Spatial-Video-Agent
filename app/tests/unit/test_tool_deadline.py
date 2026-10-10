from app.api import agent
from app.core import street
from app.core.deadline import remaining, DeadlineExceeded
from app.tests.unit.test_semantic_repairs import store


def test_statistics_tool_inherits_deadline_and_stops_as_budget(store, monkeypatch):
    observed = []
    def expired(*args):
        observed.append(remaining(100))
        raise DeadlineExceeded('budget expired')
    monkeypatch.setattr(street,'people_statistics',expired)
    body = agent.AskRequest(q='How many people walk past?',context={'workspace':'street','scene_key':'st-fixture_cam_1-clip01'},
                            budget={'tools':8,'seconds':45})
    frames = list(agent.ask_frames(store,body))
    status = next(f['data'] for f in frames if f['event']=='status')
    assert observed and 0 < observed[0] <= 45
    assert status['stop_reason']=='budget_exhausted' and status['final']=='UNRESOLVED'
