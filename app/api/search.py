"""POST /api/search: semantic candidates refined to detection timestamps."""
from __future__ import annotations

import os
import re
import time
from typing import Annotated, get_args

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core import embed, geo, llm, street
from app.core.investigation_context import apply_constraints
from app.core.models import EntityRef, Evidence, MomentCard, ObservationRef, Scope, SearchFilters, Tag, Workspace, level_of, tag_of

router = APIRouter()
LEVEL_QUOTA = 4


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    q: str = Field(min_length=1)
    workspace: Workspace | None = None
    scene_id: str | None = Field(default=None, pattern=r'^(wh|fl|st)/[a-z0-9_]+/[a-z0-9_-]+$')
    scene_key: str | None = Field(default=None, pattern=r'^[a-z0-9_-]+$')
    t_range: tuple[Annotated[int, Field(ge=0)], Annotated[int, Field(ge=0)]] | None = None
    t_ms: Annotated[int, Field(ge=0)] | None = None
    selection: list[str] | None = None
    place: str | None = None
    zone: list[tuple[float, float]] | None = Field(default=None, min_length=3)
    k: int = Field(default=10, ge=1, le=50)
    # corpus searches every workspace and ignores workspace and scene; at most min(LEVEL_QUOTA, k) moments per level
    scope: Scope = 'scene'
    sources: list[Tag] | None = None

    @model_validator(mode='after')
    def workspace_for_scene_scope(self):
        if self.scope == 'scene' and self.workspace is None and not self.sources:
            raise ValueError('workspace is required unless scope is corpus')
        return self


def event_types(q):
    text = q.lower()
    for needles, types in [(['dodge','yield','give way','step back'], {'dodge'}),
                           (['cross'], {'ped_crossing'}),
                           (['brake','braking'], {'hard_brake'}),
                           (['near miss','how far','how close','how many meters','distance','gap'], {'near_miss'})]:
        if any(n in text for n in needles):
            return types
    return None


def box_rows(store, key, cam, t0, t1, chunks=None):
    """chunks: a dict that keeps parsed chunk rows across calls of one request."""
    directory = store.manifest(key)['assets']['boxes_dir']
    chunks = {} if chunks is None else chunks
    rows = []
    for chunk in range(t0//10000, (t1-1)//10000+1):
        path = f'{directory}/{cam}/{chunk:05d}.json'
        if (key, path) not in chunks:
            try:
                chunks[key, path] = store.asset_json(key, path)['rows']
            except FileNotFoundError:
                # no chunk file is written for a 10 s window without boxes
                chunks[key, path] = []
        rows.extend(r for r in chunks[key, path] if t0<=r[0]<t1)
    return rows


def retrieve(store, request: SearchRequest, *, parsed=None, vss=None):
    from app.core.tools import result
    started = time.monotonic()
    corpus = request.scope=='corpus'
    context = request.model_dump(exclude_none=True)
    if corpus or request.sources:
        # the parser needs a workspace; corpus retrieval does not read it
        context.setdefault('workspace','warehouse')
    parsed = parsed or llm.parse(request.q, context)
    parsed, constraint_reason = apply_constraints(request.q,context,parsed)
    corpus = parsed.scope=='corpus'
    if corpus:
        parsed = parsed.model_copy(update={'scope':'corpus'})
    key = None if corpus else parsed.scene_key
    evidence, grouped, expanded, chunks, gaps = [], {}, [], {}, []
    backend = getattr(store, 'backend', 'duckdb')
    empty = {'moments':[], 'expanded_ranges':[], 'backend':backend}
    if constraint_reason:
        return parsed, result(key or 'all',0,0,{**empty,'abstain':'UNRESOLVED','reason':constraint_reason},started=started)
    if parsed.template == 'unsupported':
        return parsed, result(key or 'all',0,0,empty,started=started)
    scenes = store.list_scenes(None if corpus else parsed.workspace)
    selected = [m for m in scenes if (not key or m['scene_key']==key)
                and (not parsed.sources or tag_of(m) in parsed.sources)]
    cam_ids = list(dict.fromkeys(c['id'] for m in selected for c in m['cams']
                                if re.search(r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',request.q,re.I)))
    filters = SearchFilters(workspace=parsed.workspace,scene_keys=[m['scene_key'] for m in selected],
                            cam_ids=cam_ids or None,t_range=parsed.t_range,k=50)
    if not selected:
        return parsed, result(key or 'all',0,0,empty,started=started)
    windows = None
    if parsed.place:
        place = geo.geocode(store,parsed.place)
        passes = geo.find_passes(store,place['place_id'],geo.NEAR_M,360) if place else []
        windows = [(p['trip_id'],p['t0_ms'],p['t1_ms']) for p in passes if not key or p['trip_id']==key]
        if not windows:
            return parsed, result(key or 'all',0,0,{**empty,'abstain':'OUT_OF_RANGE'},started=started)
        windows = [w for w in windows if w[0] in filters.scene_keys]
        filters.scene_keys = list(dict.fromkeys(w[0] for w in windows))
    # site (vss given) always embeds; without an embedding service (local dev) the search is unranked over the filtered clips.
    # VSS /search is never awaited here: the store ranks our clips and the live rows directly
    vectors = embed.embed_text(request.q) if vss is not None or os.environ.get('EMBED_URL') else None
    clips = []
    # corpus ranks each workspace on its own so one level cannot crowd out another
    for workspace in (get_args(Workspace) if corpus else (parsed.workspace,)):
        scoped = filters.model_copy(update={'workspace':workspace})
        if windows is None:
            clips.extend(store.search_clips(scoped,vectors,vectors))
        else:
            # rank inside each pass so a short pass is not crowded out by the rest of its trip
            for trip, t0, t1 in windows:
                if parsed.t_range:
                    t0, t1 = max(t0,parsed.t_range[0]), min(t1,parsed.t_range[1])
                if t0 < t1:
                    clips.extend(store.search_clips(scoped.model_copy(update={'scene_keys':[trip],'t_range':(t0,t1)}),vectors,vectors))
    if windows is not None:
        clips = list({c.clip_id:c for c in clips}.values())
        clips = [c for c in clips if any(trip==c.scene_key and t0<c.t1_ms and c.t0_ms<t1 for trip, t0, t1 in windows)]
    street_clips = []
    for clip in clips:
        if clip.workspace=='street':
            if parsed.t_range:
                clip = clip.model_copy(update={'t0_ms':max(clip.t0_ms,parsed.t_range[0]),
                                               't1_ms':min(clip.t1_ms,parsed.t_range[1])})
            if clip.t0_ms<clip.t1_ms:
                street_clips.append(clip)
            continue
        manifest = store.manifest(clip.scene_key)
        t0 = max(manifest['t0_ms'],clip.t0_ms-5000)
        t1 = min(manifest['t1_ms'],clip.t1_ms+5000)
        if parsed.t_range:
            t0, t1 = max(t0,parsed.t_range[0]),min(t1,parsed.t_range[1])
        if t0 >= t1:
            continue
        expanded.append({'clip_id':clip.clip_id,'t0_ms':t0,'t1_ms':t1})
        # the expanded range is context only; a moment's detections and events must lie inside the pass and t_range
        def inside(t):
            return ((windows is None or any(trip==clip.scene_key and a<=t<b for trip, a, b in windows))
                    and (not parsed.t_range or parsed.t_range[0]<=t<parsed.t_range[1]))
        rows = [r for r in box_rows(store,clip.scene_key,clip.cam_id,t0,t1,chunks) if inside(r[0])]
        # Bicycle queries require an observed bicycle; semantic rank alone cannot certify one.
        if any(w in request.q.lower() for w in ['bicycle','cycling']):
            rows = [r for r in rows if r[2]=='bicycle']
            if not rows:
                continue
        def first_inside(e):
            # an event that began earlier still counts from the first allowed instant it covers
            spans = [(a,b) for trip, a, b in windows if trip==clip.scene_key] if windows is not None else [(e.t0_ms,e.t1_ms)]
            if parsed.t_range:
                spans = [(max(a,parsed.t_range[0]),min(b,parsed.t_range[1])) for a, b in spans]
            starts = [max(a,e.t0_ms) for a, b in spans if max(a,e.t0_ms)<min(b,e.t1_ms)]
            return min(starts) if starts else None
        events = [(first_inside(e),e) for e in store.events_in(clip.scene_key,t0,t1)]
        events = [(t,e) for t, e in events if t is not None]
        if parsed.t_range and re.search(r'\b(when|start(?:ed)?|after|before)\b',request.q,re.I):
            # A continuing event is not an event that began in the requested window.
            events = [(t,e) for t,e in events if parsed.t_range[0]<=e.t0_ms<parsed.t_range[1]]
        wanted = event_types(request.q)
        if wanted:
            events = [(t,e) for t, e in events if e.type in wanted]
            if not events:
                continue
        candidates = events if events else [(r[0],None) for r in rows[:1]]
        for candidate_t, event in candidates:
            explicit = [x for x in parsed.entities if x[:1] in 'PFV' and x[1:].isdigit()]
            if explicit and event is not None and not set(explicit)<=set(event.actor_ids):
                continue
            entities = store.entities(clip.scene_key)
            bound = explicit or (event.actor_ids if event else [])
            participant_rows = [r for r in rows if not bound or any(e.entity_id in bound and
                                any(o['cam_id']==clip.cam_id and o['local_track_id']==r[1] and o['t0_ms']<=r[0]<o['t1_ms']
                                    for o in e.observations) for e in entities)]
            stamps = sorted({r[0] for r in participant_rows if t0<=r[0]<t1 and
                             (event is None or event.t0_ms<=r[0]<event.t1_ms and abs(r[0]-candidate_t)<=250)})
            if not stamps:
                gaps.append((max(t0,event.t0_ms) if event else t0,min(t1,event.t1_ms) if event else t1,'no_participant_sample'))
                continue
            t_ms = min(stamps,key=lambda t:abs(t-candidate_t))
            actors = [EntityRef(scene_key=clip.scene_key,entity_id=e.entity_id,kind=e.kind) for e in entities
                      if e.entity_id in bound]
            if parsed.zone:
                from shapely.geometry import Point, Polygon
                moment = store.moment(clip.scene_key,t_ms)
                points = [p.root_world for p in moment.persons if not bound or p.entity_id in bound]
                points += [(f.x,f.y) for f in moment.forklift if not bound or f.entity_id in bound]
                if not any(Polygon(parsed.zone).covers(Point(p)) for p in points):
                    continue
            live = {k:v for k, v in [('rank_vss_text',clip.rank_vss_text),('rank_vss_visual',clip.rank_vss_visual)] if v is not None}
            rank_score = sum(1/(60+r) for r in [clip.rank_text,clip.rank_visual,*live.values()] if r is not None)
            ev = Evidence.create(scene_key=clip.scene_key,cam_id=clip.cam_id,t0_ms=t0,t1_ms=t1,observation_ids=[],
                                 analysis_rev=manifest['analysis_rev'],kind='model',method='caption_embed1_rrf',
                                 params={'clip_id':clip.clip_id},value={'rank_text':clip.rank_text,'rank_visual':clip.rank_visual,
                                                                      **live,'rrf':rank_score})
            evidence.append(ev)
            eids = [ev.eid]
            observations = [ObservationRef(scene_key=clip.scene_key,cam_id=clip.cam_id,local_track_id=r[1],t_ms=t_ms)
                            for r in participant_rows if r[0]==t_ms]
            stamp_ev = Evidence.create(scene_key=clip.scene_key,cam_id=clip.cam_id,t0_ms=t_ms,t1_ms=t_ms+1,
                                       observation_ids=observations,analysis_rev=manifest['analysis_rev'],kind='measure',
                                       method='detection_timestamp',value={'t_ms':t_ms})
            evidence.append(stamp_ev); eids.append(stamp_ev.eid)
            if event is not None:
                event_ev = Evidence.create(scene_key=clip.scene_key,cam_id=None,t0_ms=event.t0_ms,t1_ms=event.t1_ms,
                                           observation_ids=[],analysis_rev=manifest['analysis_rev'],kind='model' if event.source=='model' else 'measure',
                                           method='event_'+event.source,params={'event_id':event.event_id},
                                           value={'event_id':event.event_id,'type':event.type,'t0_ms':event.t0_ms,
                                                  't_s':event.t0_ms/1000,'source':event.source,'actor_ids':event.actor_ids,'params':event.params,
                                                  **({'distance_to':'forklift_footprint','distance_from':'person_root'} if event.type=='near_miss' else {})})
                evidence.append(event_ev); eids.append(event_ev.eid)
                for related in store.events_in(clip.scene_key,t_ms,t_ms+1):
                    if (related.event_id==event.event_id or not set(related.actor_ids)&set(event.actor_ids)
                            or explicit and not set(explicit)<=set(related.actor_ids)):
                        continue
                    related_ev = Evidence.create(scene_key=clip.scene_key,cam_id=None,t0_ms=related.t0_ms,t1_ms=related.t1_ms,
                                                 observation_ids=[],analysis_rev=manifest['analysis_rev'],kind='model' if related.source=='model' else 'measure',
                                                 method='event_'+related.source,params={'event_id':related.event_id},
                                                 value={'event_id':related.event_id,'type':related.type,'t0_ms':related.t0_ms,
                                                        't_s':related.t0_ms/1000,'source':related.source,'actor_ids':related.actor_ids,'params':related.params,
                                                        **({'distance_to':'forklift_footprint','distance_from':'person_root'} if related.type=='near_miss' else {})})
                    evidence.append(related_ev); eids.append(related_ev.eid)
            group_key = (clip.scene_key,t_ms)
            if group_key in grouped:
                card = grouped[group_key]
                card.cams = list(dict.fromkeys([*card.cams,clip.cam_id]))
                card.evidence_ids = list(dict.fromkeys([*card.evidence_ids,*eids]))
                continue
            primary = clip.cam_id
            from app.core.tools import thumbnail
            thumb = thumbnail(store,clip.scene_key,primary,t_ms)
            if thumb is None:
                gaps.append((t_ms,t_ms+1,'missing_thumbnail'))
            grouped[group_key] = MomentCard(moment_id=f'm_{clip.scene_key}_{t_ms}',scene_key=clip.scene_key,t0_ms=t_ms,
                                            t1_ms=min(t_ms+1000,manifest['t1_ms']),cams=[clip.cam_id],primary_cam=primary,
                                            actor_refs=actors,evidence_ids=eids,score=rank_score,
                                            why=[f'caption rank {clip.rank_text}; visual rank {clip.rank_visual}',
                                                 'detection timestamp verified'],
                                            thumb=thumb,
                                            exec_state='live',backend=backend,clip_id=clip.clip_id,
                                            **({'workspace':manifest['workspace'],'evidence_level':level_of(manifest)} if corpus else {}))
    quota = min(LEVEL_QUOTA,request.k) if corpus else request.k
    street_cards = []
    # street scenes have no entity ids and no world frame, so entity and zone questions skip them
    if not parsed.zone and not [x for x in parsed.entities if x[:1] in 'PFV' and x[1:].isdigit()]:
        for clip in sorted(street_clips,key=lambda c:(-street.rrf(c),c.clip_id)):
            if len(street_cards)==quota:
                break
            card, card_evidence = street.moment(store,clip,backend=backend,vss=vss)
            if any(w in request.q.lower() for w in ['bicycle','cycling']) and not card_evidence[1].value['counts'].get('bicycle'):
                continue
            if card.thumb is None:
                gaps.append((card.t0_ms,card.t1_ms,'missing_thumbnail'))
            street_cards.append(card)
            evidence.extend(card_evidence)
            expanded.append({'clip_id':clip.clip_id,'t0_ms':clip.t0_ms,'t1_ms':clip.t1_ms})
    if corpus:
        levels = {}
        for card in sorted([*grouped.values(),*street_cards],key=lambda m:-m.score):
            levels.setdefault(card.evidence_level,[]).append(card)
        moments = [m for level in ('L3','L2','L1') for m in levels.get(level,[])[:quota]]
    else:
        moments = sorted([*grouped.values(),*street_cards],key=lambda m:-m.score)[:request.k]
    evidence = list({e.eid:e for e in evidence}.values())
    output = {'moments':[{**m.model_dump(exclude_none=True),'thumb':m.thumb} for m in moments], 'expanded_ranges':expanded,'backend':backend}
    if not moments and (request.t_ms is not None or request.selection or parsed.zone):
        output.update(abstain='UNRESOLVED',reason='missing_constrained_evidence')
    return parsed, result(key or 'all',min((r['t0_ms'] for r in expanded),default=0),
                          max((r['t1_ms'] for r in expanded),default=0),output,evidence=evidence,
                          pts_read=len(clips),gaps=gaps,started=started)


def search_response(store, parsed, result):
    cards = []
    for moment in result.envelope.output['moments']:
        m = MomentCard.model_validate(moment)
        cards.append(dict(scene_id=store.manifest(m.scene_key)['scene_id'],scene_key=m.scene_key,cam=m.primary_cam,
                          t_ms=m.t0_ms,t1_ms=m.t1_ms,score=m.score,why=m.why,badges=['model','measure'],thumb=m.thumb,
                          clip_id=m.clip_id,backend=m.backend))
    return {'template':parsed.template,'parsed':parsed.model_dump(mode='json'),'cards':cards,
            'took_ms':result.envelope.elapsed_ms,'backend':[result.envelope.output['backend']]}


@router.post('/api/search')
def search(request: Request, body: SearchRequest):
    settings = getattr(request.app.state,'settings',None)
    if getattr(settings,'mode','dev')=='site' and getattr(request.app.state,'vss',None) is None:
        raise HTTPException(503,'site VSS is not configured')
    parsed, result = retrieve(request.app.state.store,body,vss=getattr(request.app.state,'vss',None))
    return search_response(request.app.state.store,parsed,result)
