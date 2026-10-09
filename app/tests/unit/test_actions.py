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
