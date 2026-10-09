"""One trigger entry for the API, watch flow and agent."""
from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import APIRouter, Request
from shapely.geometry import MultiPoint

from app.api.scenes import actor_points
from app.core.models import Action, TriggerRequest, TriggerResult, Zone
from app.core.store import now

router = APIRouter()
logger = logging.getLogger(__name__)


def trigger(store, req: TriggerRequest) -> TriggerResult:
    event_id = (req.event_id if ':' in req.event_id else f'{req.scene_key}:{req.event_id}') if req.event_id else None
    with store.lock:
        existing = store.find_action(req.scene_key, event_id) if event_id else None
        if existing:
            return TriggerResult(**{k: getattr(existing, k) for k in ('action_id', 'delivered', 'review_task_id', 'zone_id')}, existing=True)
        m = store.manifest(req.scene_key)
        event = next((e for e in store.events_in(req.scene_key, m['t0_ms'], m['t1_ms']) if e.event_id == event_id), None)
        if event_id and event is None:
            raise FileNotFoundError('Event not found')
        action_id = 'a_' + uuid4().hex
        zone_id, review_task_id = None, None
        if 'hazard_zone' in req.actions:
            if event is None:
                raise ValueError('hazard_zone requires an event_id')
            t = event.params.get('t_min_ms', event.params.get('near_t0_ms', event.t0_ms))
            positions = actor_points(store, req.scene_key, t, event.actor_ids)
            if not positions:
                raise ValueError('Event has no participant positions')
            polygon = MultiPoint(positions).convex_hull.buffer(3)
            zone_id = 'z_' + uuid4().hex
            zone = Zone(zone_id=zone_id, scene_key=req.scene_key, kind='hazard', polygon_world=list(polygon.exterior.coords),
                        source_event_id=event_id, action_id=action_id, t_range=(event.t0_ms, event.t1_ms), created=now())
        if 'create_review_task' in req.actions:
            n = store.app.execute('SELECT count(*) FROM actions WHERE review_task_id IS NOT NULL').fetchone()[0] + 1
            review_task_id = f'RT-{n}'
        # webhook has no delivery channel, so requesting it leaves the action undelivered
        delivered = 'wandb_alert' in req.actions and 'webhook' not in req.actions
        if 'wandb_alert' in req.actions:
            if store.settings.mode == 'dev':
                logger.info('Emergency action %s scene=%s event=%s review=%s zone=%s', action_id, req.scene_key, event_id, review_task_id, zone_id)
            elif store.settings.wandb_api_key and store.settings.wandb_project:
                try:
                    import wandb
                    wandb.init(entity=store.settings.wandb_team or None, project=store.settings.wandb_project)
                    wandb.alert(title='SVA emergency action', text=f'{req.scene_key} {event_id}: {review_task_id}, {zone_id}')
                except Exception:
                    delivered = False
            else:
                delivered = False
        action = Action(action_id=action_id, scene_key=req.scene_key, event_id=event_id, run_id=req.run_id, claim_id=req.claim_id,
                        auto=req.auto, actions=req.actions, review_task_id=review_task_id, zone_id=zone_id, delivered=delivered, created=now())
        store.app.execute('BEGIN TRANSACTION')
        try:
            # insert_action/insert_zone keep the W0 contract signature, so the unmirrored insert goes through _insert_model
            store._insert_model('actions', action, mirror=False)
            if zone_id:
                store._insert_model('zones', zone, mirror=False)
            store.app.execute('COMMIT')
        except BaseException:
            store.app.execute('ROLLBACK')
            raise
    store._mirror('actions', action_id)
    if zone_id:
        store._mirror('zones', zone_id)
    return TriggerResult(action_id=action_id, delivered=delivered, review_task_id=review_task_id, zone_id=zone_id, existing=False)


@router.post('/api/actions/trigger', response_model=TriggerResult)
def trigger_route(req: TriggerRequest, request: Request):
    return trigger(request.app.state.store, req)


@router.get('/api/actions')
def actions(scene_key: str, request: Request):
    store = request.app.state.store
    return {'actions': store.list_actions(scene_key), 'zones': store.list_zones(scene_key)}
