import sys

from app.api.actions import trigger
from app.core.models import TriggerRequest
from app.tests.unit.test_actions import alerts, store, index_db
from app.tests.unit.test_store import WH


def test_delivery_after_commit_outside_store_lock_and_failure_retry(store, alerts, monkeypatch):
    attempts = []
    def alert(**kw):
        assert not store.lock._is_owned()
        assert store.find_action(WH, WH+':E1') is not None
        attempts.append(kw)
        if len(attempts)==1:
            raise RuntimeError('transient delivery failure')
    monkeypatch.setattr(sys.modules['wandb'], 'alert', alert)
    req = TriggerRequest(scene_key=WH,event_id='E1',auto=False,actions=['wandb_alert'])
    first = trigger(store,req)
    assert not first.delivered
    second = trigger(store,req)
    assert second.existing and second.delivered and second.action_id==first.action_id
    assert trigger(store,req).delivered and len(attempts)==2


def test_new_channels_can_be_added_to_existing_action(store, alerts):
    first = trigger(store,TriggerRequest(scene_key=WH,event_id='E1',auto=False,actions=['create_review_task']))
    second = trigger(store,TriggerRequest(scene_key=WH,event_id='E1',auto=False,actions=['wandb_alert','hazard_zone']))
    assert second.action_id==first.action_id and second.review_task_id==first.review_task_id
    assert second.zone_id and second.delivered and len(alerts)==1
    assert len(store.list_actions(WH))==len(store.list_zones(WH))==1
