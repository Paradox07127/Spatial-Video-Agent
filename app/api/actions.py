"""One trigger entry for the API, watch flow and agent."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Request
from shapely.geometry import MultiPoint

from app.api.scenes import actor_points
from app.core.models import Action, TriggerRequest, TriggerResult, Zone
from app.core.store import now

router = APIRouter()
logger = logging.getLogger(__name__)


def deliver_notifications(store, action):
    """Claim channels locally, send after commit without the DB lock, and retain retryable failures."""
    channels = [c for c in action.actions if c in ('wandb_alert', 'webhook')]
    delivered = bool(channels)
    for channel in channels:
        with store.lock:
            row = store.app.execute('SELECT status,updated FROM action_deliveries WHERE action_id=? AND channel=?', [action.action_id,channel]).fetchone()
            if row and row[0] == 'sent':
                continue
            if row and row[0] == 'sending' and row[1] > datetime.now(timezone.utc).replace(tzinfo=None)-timedelta(seconds=60):
                delivered = False
                continue
            store.app.execute('INSERT OR REPLACE INTO action_deliveries VALUES (?,?,?,?)', [action.action_id,channel,'sending',now()])
        ok = False
        try:
            if channel == 'wandb_alert':
                if store.settings.mode == 'dev':
                    logger.info('Emergency action %s scene=%s event=%s', action.action_id, action.scene_key, action.event_id)
                    ok = True
                elif store.settings.wandb_api_key and store.settings.wandb_project:
                    import wandb
                    wandb.init(entity=store.settings.wandb_team or None, project=store.settings.wandb_project)
                    wandb.alert(title='SVA emergency action', text=f'{action.scene_key} {action.event_id}: {action.review_task_id}, {action.zone_id}')
                    ok = True
            elif getattr(store.settings,'webhook_url',''):
                import httpx
                with httpx.Client(timeout=5) as client:
                    response = client.post(store.settings.webhook_url, json=action.model_dump(mode='json'),
                                           headers={'Idempotency-Key':action.action_id})
                    response.raise_for_status()
                    ok = True
        except Exception:
            logger.warning('Action delivery deferred for %s/%s', action.action_id, channel)
        with store.lock:
            store.app.execute('UPDATE action_deliveries SET status=?,updated=? WHERE action_id=? AND channel=?', ['sent' if ok else 'failed',now(),action.action_id,channel])
        delivered = delivered and ok
    with store.lock:
        store.app.execute('UPDATE actions SET delivered=? WHERE action_id=?', [delivered,action.action_id])
    return delivered


def trigger(store, req: TriggerRequest) -> TriggerResult:
    event_id = (req.event_id if ':' in req.event_id else f'{req.scene_key}:{req.event_id}') if req.event_id else None
    with store.lock:
        existing = store.find_action(req.scene_key, event_id) if event_id else None
        if existing and existing.delivered:
            # Older releases and remote snapshots predate per-channel delivery rows.
            # Their successful channels must not be sent again during migration.
            for channel in existing.actions:
                if channel in ('wandb_alert','webhook'):
                    store.app.execute('INSERT INTO action_deliveries VALUES (?,?,?,?) ON CONFLICT DO NOTHING',
                                      [existing.action_id,channel,'sent',now()])
        m = store.manifest(req.scene_key)
        event = next((e for e in store.events_in(req.scene_key, m['t0_ms'], m['t1_ms']) if e.event_id == event_id), None)
        if event_id and event is None:
            raise FileNotFoundError('Event not found')
        action_id = existing.action_id if existing else 'a_' + uuid4().hex
        zone_id, review_task_id = (existing.zone_id,existing.review_task_id) if existing else (None,None)
        zone = None
        requested = list(dict.fromkeys((existing.actions if existing else [])+req.actions))
        if 'hazard_zone' in requested and not zone_id:
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
        if 'create_review_task' in requested and not review_task_id:
            n = store.app.execute('SELECT count(*) FROM actions WHERE review_task_id IS NOT NULL').fetchone()[0] + 1
            review_task_id = f'RT-{n}'
        action = Action(action_id=action_id, scene_key=req.scene_key, event_id=event_id, run_id=req.run_id, claim_id=req.claim_id,
                        auto=req.auto, actions=requested, review_task_id=review_task_id, zone_id=zone_id, delivered=False, created=existing.created if existing else now())
        store.app.execute('BEGIN TRANSACTION')
        try:
            # insert_action/insert_zone keep the W0 contract signature, so the unmirrored insert goes through _insert_model
            if existing:
                changes = [(name,value) for name,value,old in
                           [('actions',requested,existing.actions),('review_task_id',review_task_id,existing.review_task_id),
                            ('zone_id',zone_id,existing.zone_id)] if value!=old]
                # A duplicate trigger is a read/retry, not a LIST rewrite. DuckDB
                # rewrites LIST UPDATEs to delete/insert, including ART maintenance.
                if changes:
                    store.app.execute('UPDATE actions SET '+','.join(name+'=?' for name,_ in changes)+' WHERE action_id=?',
                                      [value for _,value in changes]+[action_id])
            else:
                store._insert_model('actions', action, mirror=False)
            if zone:
                store._insert_model('zones', zone, mirror=False)
            store.app.execute('COMMIT')
        except BaseException:
            store.app.execute('ROLLBACK')
            raise
    delivered = deliver_notifications(store, action)
    store._mirror('actions', action_id)
    if zone:
        store._mirror('zones', zone_id)
    return TriggerResult(action_id=action_id, delivered=delivered, review_task_id=review_task_id, zone_id=zone_id, existing=existing is not None)


@router.post('/api/actions/trigger', response_model=TriggerResult)
def trigger_route(req: TriggerRequest, request: Request):
    return trigger(request.app.state.store, req)


@router.get('/api/actions')
def actions(scene_key: str, request: Request):
    store = request.app.state.store
    return {'actions': store.list_actions(scene_key), 'zones': store.list_zones(scene_key)}
