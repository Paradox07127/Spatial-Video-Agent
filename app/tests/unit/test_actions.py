import sys
import types

import pytest
from shapely.geometry import Point, Polygon

from app.api.actions import trigger
from app.core.models import Action, TriggerRequest, TriggerResult, Zone
from app.tests.unit.test_store import WH, store, index_db


def test_trigger_dedup_and_hull(store):
    req = TriggerRequest(scene_key=WH, event_id='E1', auto=True, actions=['create_review_task', 'hazard_zone', 'wandb_alert'])
    first = TriggerResult.model_validate(trigger(store, req))
    second = trigger(store, req.model_copy(update={'event_id': WH + ':E1'}))
    assert first.action_id == second.action_id and second.existing
    assert first.review_task_id == 'RT-1'
    actions, zones = store.list_actions(WH), store.list_zones(WH)
    assert len(actions) == len(zones) == 1
    Action.model_validate(actions[0].model_dump())
    zone = Zone.model_validate(zones[0].model_dump())
    assert zone.source_event_id == WH + ':E1'
    moment = store.moment(WH, 4900)
    p = next(p for p in moment.persons if p.entity_id == 'P1')
    poly = Polygon(zone.polygon_world)
    assert poly.covers(Point(p.root_world))
    assert poly.covers(Point(moment.forklift[0].x, moment.forklift[0].y))
    assert poly.area > 28


def test_trigger_mirrors_after_commit_outside_lock(store):
    log = []
    class Connection:
        def __init__(self, con):
            self.con = con
        def execute(self, sql, *args):
            if sql == 'COMMIT':
                log.append('COMMIT')
            return self.con.execute(sql, *args)
        def __getattr__(self, name):
            return getattr(self.con, name)
    store.app = Connection(store.app)
    store.backend = 'vastdb'
    store.write_vastdb = lambda tables: log.append((list(tables), store.lock._is_owned()))
    trigger(store, TriggerRequest(scene_key=WH, event_id='E1', auto=True, actions=['hazard_zone']))
    assert log == ['COMMIT', (['actions'], False), (['zones'], False)]


@pytest.fixture
def alerts(store, monkeypatch):
    sent = []
    fake = types.ModuleType('wandb')
    fake.init = lambda **kw: None
    fake.alert = lambda **kw: sent.append(kw)
    monkeypatch.setitem(sys.modules, 'wandb', fake)
    store.settings.mode, store.settings.wandb_api_key, store.settings.wandb_project = 'site', 'key', 'proj'
    return sent


@pytest.mark.parametrize('requested, n_alerts, delivered', [
    (['create_review_task'], 0, False),
    (['webhook'], 0, False),
    (['wandb_alert'], 1, True),
    (['wandb_alert', 'webhook'], 1, False),
])
def test_trigger_delivers_only_requested_actions(store, alerts, requested, n_alerts, delivered):
    result = trigger(store, TriggerRequest(scene_key=WH, event_id='E1', auto=True, actions=requested))
    assert len(alerts) == n_alerts
    assert result.delivered is delivered


def test_trigger_wandb_alert_failure_is_undelivered(store, alerts):
    def fail(**kw):
        raise RuntimeError('wandb down')
    sys.modules['wandb'].alert = fail
    result = trigger(store, TriggerRequest(scene_key=WH, event_id='E1', auto=True, actions=['wandb_alert']))
    assert result.delivered is False
