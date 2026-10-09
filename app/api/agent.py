"""Deterministic investigation, streamed as contract SSE frames."""
from __future__ import annotations

import base64
import html
import json
import logging
import re
import threading
import time
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from app.api.search import event_types
from app.core import cache, geo, llm, policy, tools, trace
from app.core.models import Claim, EntityRef, MomentCard, Predicate, TriggerRequest, aggregate_of

router = APIRouter()
logger = logging.getLogger(__name__)
VERBS = {'dodge':'started to dodge','near_miss':'approached {other}','ped_crossing':'crossed in front of the car','ped_near':'appeared in front of the car'}
SIDES = {'right':'on the right','left':'on the left',None:''}
NATIVE_WAIT_S = 10


def native_search(vss, q):
    """Start VSS /search in the background; the returned function waits at most its argument for the vss_native data."""
    started = time.monotonic()
    box = []
    def search():
        try:
            out = vss.search(q,10,{})
            data = {'status':'ok','sql_query':out['sql_query'],'hits':out['hits'][:10]}
        except Exception:
            logger.exception('VSS /search failed')
            data = {'status':'error','sql_query':None,'hits':[]}
        box.append({'elapsed_ms':round((time.monotonic()-started)*1000),**data})
    worker = threading.Thread(target=search,daemon=True)
    worker.start()
    def wait(seconds):
        worker.join(seconds)
        if box:
            return box[0]
        return {'elapsed_ms':round((time.monotonic()-started)*1000),'status':'timeout','sql_query':None,'hits':[]}
    return wait


def trip_name(key):
    # the fleet page's short trip label
    return re.sub(r'^fl-pie-(set\d+)_video_(\d+)$',r'\1·\2',key)


def noun(kind, workspace):
    if kind=='person':
        return 'worker' if workspace=='warehouse' else 'pedestrian'
    return {'forklift':'forklift','vehicle':'vehicle'}.get(kind,'object')


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tools: int = Field(default=8, ge=0, le=16)
    seconds: float = Field(default=25, ge=0, le=45)


class AskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    q: str = Field(min_length=1, max_length=500)
    context: dict
    budget: Budget = Field(default_factory=Budget)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: str
    verdict: Literal['confirm','reject','edit']
    note: str = ''


class BudgetExhausted(Exception):
    pass


def ask_frames(store, body: AskRequest, *, vss=None):
    start = time.monotonic()
    run = trace.start_run(store,body.q,body.context)
    run.deadline = start+body.budget.seconds
    # site only: VSS's own retrieval, shown next to the answer for comparison; the answer never waits for it
    native = native_search(vss,body.q) if vss is not None else None
    claims, frames, read, moments = [], [], {}, []
    count = 0
    final, stop_reason = 'UNRESOLVED', 'no_new_info'
    circuit = {'failures':0}
    finished = False
    no_limit = False
    covered, building, closest = [], None, None
    claim_id = 'c1'

    def frame(event, **data):
        f = {'event':event,'data':{'run_id':run.run_id,**data}}
        frames.append(f)
        return f

    def step_frame(t, state, summary):
        return frame('step',step_id=t.step_id,i=t.i,state=state,
                     elapsed_ms=round((time.monotonic()-start)*1000),summary=summary,exec_state='live')

    def check_budget():
        if count>=body.budget.tools or time.monotonic()-start>=body.budget.seconds:
            raise BudgetExhausted

    def call(parent, name, state, args, fn):
        nonlocal count
        check_budget()
        count += 1
        with trace.traced(run,'tool.'+name,args,state=state,parent_step_id=parent.step_id) as t:
            yield frame('tool_call',step_id=parent.step_id,call_id=t.step_id,name=name,args=args,why=name)
            res = fn()
            store.add_evidence(run.run_id,res.evidence)
            t.output(res.envelope.model_dump(mode='json'))
            t.evidence([e.eid for e in res.evidence])
            t.exec_state(res.envelope.exec_state)
            read.update({e.eid:e for e in res.evidence})
            yield frame('tool_result',step_id=parent.step_id,call_id=t.step_id,ok=res.envelope.exec_state!='failed',
                        envelope=res.envelope.model_dump(mode='json'),evidence=[e.model_dump(mode='json') for e in res.evidence])
            if time.monotonic()-start>=body.budget.seconds:
                raise BudgetExhausted
            if isinstance(res.envelope.output,dict) and 'record' in res.envelope.output:
                record = res.envelope.output['record']
                yield frame('model_result',scene_key=res.envelope.input_range.scene_key,t_ms=record['t_ms'],results=[record])
            return res

    def claim_frame(predicates, before=None, explain=None):
        aggregate = aggregate_of(predicates)
        claim = Claim(run_id=run.run_id,claim_id=claim_id,text='Spatial and event evidence check',predicates=[p.model_copy(deep=True) for p in predicates],
                      aggregate=aggregate,before=before,after=aggregate,examined_eids=list(read),
                      available_eids=[],explain=explain)
        claims[:]=[c for c in claims if c.claim_id!=claim_id]+[claim]
        return frame('claim',**{k:v for k,v in claim.model_dump(mode='json').items() if k!='run_id'})

    try:
        with trace.traced(run,'parse',{'q':body.q,'context':body.context},state='parse') as t:
            yield step_frame(t,'parse','Parse the question template')
            parsed = llm.rule_parse(body.q,body.context)
            if llm.needs_llm(body.q,parsed):
                if time.monotonic()-start>=body.budget.seconds:
                    raise BudgetExhausted
                with trace.traced(run,'llm.parse',{'q':body.q,'rule':parsed.model_dump(mode='json')},state='parse',parent_step_id=t.step_id) as child:
                    parsed = llm.parse(body.q,body.context)
                    child.output(parsed.model_dump(mode='json'))
            if parsed.workspace=='fleet' and not parsed.place:
                parsed = parsed.model_copy(update={'place':geo.named_place(store,body.q)})
            t.output(parsed.model_dump(mode='json'))
        if parsed.template=='unsupported':
            final, stop_reason = 'UNSUPPORTED','abstained'
        else:
            scenes = store.list_scenes(parsed.workspace)
            scoped = [s for s in scenes if not parsed.scene_key or s['scene_key']==parsed.scene_key]
            blind_cam = None
            if parsed.workspace=='warehouse' and re.search(r"can'?t see|cannot see|blind",body.q,re.I):
                blind_cam = next((c['id'] for s in scoped for c in s['cams']
                                  if re.search(r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',body.q,re.I)),None)
            blind_scenes = [s for s in scoped if 'visibility' in s['assets'] and any(c['id']==blind_cam for c in s['cams'])]
            speeding_q = parsed.workspace=='fleet' and any(w in body.q.lower() for w in ['speeding'])
            building_q = parsed.workspace=='fleet' and parsed.place and re.search(r'\bbuilding\b',body.q,re.I)
            closest_q = (parsed.workspace=='warehouse' and {'person','forklift'}<=set(parsed.entities)
                         and re.search(r'\b(closest|nearest|minimum)\b',body.q,re.I)
                         and re.search(r'\b(distance|gap|how far|how many meters|how close|closest|nearest)\b',body.q,re.I))
            if not scoped or (parsed.t_range and (parsed.t_range[0]>=parsed.t_range[1] or
                    not any(store.manifest(s['scene_key'])['t0_ms']<parsed.t_range[1] and
                            store.manifest(s['scene_key'])['t1_ms']>parsed.t_range[0] for s in scoped))):
                final, stop_reason = 'OUT_OF_RANGE','abstained'
            elif body.context.get('t_ms') is not None and not any(
                    store.manifest(s['scene_key'])['t0_ms']<=body.context['t_ms']<store.manifest(s['scene_key'])['t1_ms'] for s in scoped):
                final, stop_reason = 'OUT_OF_RANGE','abstained'
            elif blind_scenes:
                with trace.traced(run,'plan',{'query':parsed.model_dump(mode='json')},state='plan') as t:
                    yield step_frame(t,'plan','Choose the blind spot verification path')
                    t.output({'template':parsed.template,'path':['inspect','assess','answer'],'cam':blind_cam})
                checked, cards = [], []
                exhausted = False
                try:
                    for scene in blind_scenes:
                        key = scene['scene_key']
                        t0, t1 = scene['t0_ms'], scene['t1_ms']
                        if parsed.t_range:
                            t0, t1 = max(t0,parsed.t_range[0]), min(t1,parsed.t_range[1])
                        if t0 >= t1:
                            continue
                        with trace.traced(run,'assess',{'scene_key':key,'cam':blind_cam,'t0_ms':t0,'t1_ms':t1},state='inspect') as t:
                            yield step_frame(t,'inspect','Check people in the camera blind spot frame by frame')
                            res = yield from call(t,'blind_presence','inspect',{'scene_key':key,'cam':blind_cam,'t0_ms':t0,'t1_ms':t1},
                                                  lambda:tools.blind_presence(store,key,blind_cam,t0,t1))
                            intervals = res.envelope.output['intervals']
                            persons = list(dict.fromkeys(i['entity_id'] for i in intervals))
                            status = 'supported' if intervals else 'contradicted' if res.envelope.coverage==1 else 'unknown'
                            predicates = [Predicate(predicate_id='p1',subject_refs=[EntityRef(scene_key=key,entity_id=e,kind='person') for e in persons],
                                                    operator='present_in_blind_zone',value=blind_cam,unit=None,status=status,
                                                    eids=[e.eid for e in res.evidence])]
                            t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                        checked.append((key,intervals,persons,predicates))
                        cams = {c['id']:c for c in scene['cams']}
                        for interval, ev in zip(intervals,res.evidence):
                            primary = (interval['seen_by'] or [blind_cam])[0]
                            cards.append(MomentCard(moment_id=f"m_{key}_{interval['t0_ms']}_{interval['entity_id']}",scene_key=key,
                                                    t0_ms=interval['t0_ms'],t1_ms=interval['t1_ms'],cams=[*interval['seen_by'],blind_cam],
                                                    primary_cam=primary,actor_refs=[EntityRef(scene_key=key,entity_id=interval['entity_id'],kind='person')],
                                                    evidence_ids=[ev.eid],score=1,why=['blind_presence'],
                                                    thumb=store.asset_url(key,f"{cams[primary]['thumbs']}/{interval['t0_ms']//1000:05d}.jpg") if cams[primary]['thumbs'] else None,
                                                    backend=getattr(store,'backend','duckdb'),exec_state='live'))
                except BudgetExhausted:
                    # the scenes already checked still answer; the unchecked ones only rule out CONTRADICTED
                    exhausted = True
                if cards:
                    store.set_hits(run.run_id,cards[:5])
                    yield frame('hits',query_id=run.run_id,moments=[m.model_dump(exclude_none=True) for m in cards[:5]])
                for scene_index, (key, intervals, persons, predicates) in enumerate(checked):
                    claim_id='c'+str(scene_index+1)
                    if intervals:
                        yield frame('ui',cmd='seek',args={'t_ms':intervals[0]['t0_ms'],'scene_key':key})
                        yield frame('ui',cmd='highlight',args={'ids':persons,'mode':'outline'})
                    yield claim_frame(predicates)
                # one scene with nobody in the blind zone must not veto another scene's finding
                aggregates = [c.aggregate for c in claims]
                final = 'SUPPORTED' if 'SUPPORTED' in aggregates else 'CONTRADICTED' if aggregates and all(a=='CONTRADICTED' for a in aggregates) and not exhausted else 'UNRESOLVED'
                stop_reason = 'budget_exhausted' if exhausted else 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
            elif closest_q:
                with trace.traced(run,'plan',{'query':parsed.model_dump(mode='json')},state='plan') as t:
                    yield step_frame(t,'plan','Choose the closest approach path across the full interval')
                    t.output({'template':parsed.template,'path':['measure','answer']})
                predicates = []
                with trace.traced(run,'assess',{'scene_key':parsed.scene_key,'t_range':parsed.t_range},state='measure') as t:
                    yield step_frame(t,'measure','Scan worker and forklift tracks to measure the closest distance')
                    scan = yield from call(t,'closest_approach','measure',{'scene_key':parsed.scene_key,'t_range':parsed.t_range,'zone':parsed.zone},
                                           lambda:tools.closest_approach(store,scoped,parsed.t_range,parsed.zone))
                    nearest = scan.envelope.output['closest']
                    closest = {'missing':scan.envelope.output['missing'],'scan':scan.evidence[0] if scan.evidence else None}
                    if nearest:
                        key, tm = nearest['scene_key'], nearest['t_ms']
                        person = EntityRef(scene_key=key,entity_id=nearest['a'],kind='person')
                        vehicle = EntityRef(scene_key=key,entity_id=nearest['b'],kind='forklift')
                        res = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':person.model_dump(),'b':vehicle.model_dump(),'kind':'distance'},
                                              lambda:tools.measure(store,key,tm,person,vehicle,'distance'))
                        limit = re.search(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',body.q,re.I)
                        ev = res.evidence[0] if res.evidence else None
                        status = ('unknown' if ev is None else 'supported' if not limit or ev.value+ev.sigma<float(limit[1])
                                  else 'contradicted' if ev.value-ev.sigma>=float(limit[1]) else 'unknown')
                        predicates = [Predicate(predicate_id='p1',subject_refs=[person,vehicle],operator='lt' if limit else 'measured',
                                                value=float(limit[1]) if limit else None,unit='m',status=status,eids=[e.eid for e in res.evidence])]
                    t.output({'predicates':[p.model_dump(mode='json') for p in predicates],'missing':closest['missing']})
                if nearest:
                    cam = next(s['cams'] for s in scoped if s['scene_key']==key)[0]
                    hit = MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam['id']],primary_cam=cam['id'],
                                     actor_refs=[person,vehicle],evidence_ids=[e.eid for e in [*scan.evidence,*res.evidence]],score=1,
                                     why=['closest_approach'],thumb=store.asset_url(key,f"{cam['thumbs']}/{tm//1000:05d}.jpg") if cam['thumbs'] else None,
                                     backend=getattr(store,'backend','duckdb'),exec_state='live')
                    store.set_hits(run.run_id,[hit])
                    yield frame('hits',query_id=run.run_id,moments=[hit.model_dump(exclude_none=True)])
                    yield frame('ui',cmd='seek',args={'t_ms':tm,'scene_key':key})
                    yield frame('ui',cmd='highlight',args={'ids':[person.entity_id,vehicle.entity_id],'mode':'outline'})
                    yield claim_frame(predicates)
                final = aggregate_of(predicates)
                stop_reason = 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
            elif building_q:
                side = 'right' if re.search(r'\bright\b',body.q,re.I) else 'left' if re.search(r'\bleft\b',body.q,re.I) else None
                with trace.traced(run,'plan',{'query':parsed.model_dump(mode='json')},state='plan') as t:
                    yield step_frame(t,'plan','Choose the map building facade path')
                    t.output({'template':parsed.template,'path':['retrieve','inspect','answer'],'side':side})
                with trace.traced(run,'retrieve',{'place':parsed.place},state='retrieve') as t:
                    yield step_frame(t,'retrieve','Find videos passing the place')
                    place = (yield from call(t,'geocode','retrieve',{'name':parsed.place},lambda:tools.geocode(store,parsed.place))).envelope.output
                    passes = []
                    if place:
                        passes = (yield from call(t,'find_passes','retrieve',{'place_id':place['place_id'],'r_m':geo.NEAR_M,'fov_deg':360},
                                                  lambda:tools.find_passes(store,place['place_id'],geo.NEAR_M,360))).envelope.output
                    video = [p for p in passes if any(s['scene_key']==p['trip_id'] and s['cams'] for s in scoped)]
                    asked = geo.trim_passes(store,video,parsed.t_range)
                    t.output({'passes':passes,'video_passes':video,'asked_passes':asked})
                building = {'side':side,'video':bool(video)}
                if not passes or video and not asked:
                    final, stop_reason = 'OUT_OF_RANGE','abstained'
                elif video:
                    with trace.traced(run,'assess',{'passes':asked,'side':side},state='inspect') as t:
                        yield step_frame(t,'inspect','Read building facades at the time closest to the place')
                        res = yield from call(t,'facade','inspect',{'passes':asked,'lat':place['lat'],'lon':place['lon'],'side':side},
                                              lambda:tools.facade(store,asked,place['lat'],place['lon'],side))
                        found = res.envelope.output
                        predicates = [Predicate(predicate_id='p1',subject_refs=[],operator='facade',
                                                value=found['building']['label'] if found['building'] else None,unit=None,
                                                status='supported' if found['building'] else 'unknown',eids=[e.eid for e in res.evidence])]
                        t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                    if found['building']:
                        key, tm = found['scene_key'], found['t_ms']
                        cam = next(s['cams'] for s in scoped if s['scene_key']==key)[0]
                        hit = MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam['id']],primary_cam=cam['id'],
                                         actor_refs=[],evidence_ids=[e.eid for e in res.evidence],score=1,why=['osm_facade'],
                                         thumb=store.asset_url(key,f"{cam['thumbs']}/{tm//1000:05d}.jpg"),
                                         backend=getattr(store,'backend','duckdb'),exec_state='live')
                        store.set_hits(run.run_id,[hit])
                        yield frame('hits',query_id=run.run_id,moments=[hit.model_dump(exclude_none=True)])
                    if found['t_ms'] is not None:
                        yield frame('ui',cmd='seek',args={'t_ms':found['t_ms'],'scene_key':found['scene_key']})
                    if found['building']:
                        yield frame('ui',cmd='highlight',args={'ids':[found['building']['bid']],'mode':'outline'})
                    yield claim_frame(predicates)
                    final = aggregate_of(predicates)
                    stop_reason = 'enough_evidence' if final=='SUPPORTED' else 'no_new_info'
            else:
                with trace.traced(run,'plan',{'query':parsed.model_dump(mode='json')},state='plan') as t:
                    yield step_frame(t,'plan','Choose the template investigation path')
                    t.output({'template':parsed.template,'path':['retrieve','inspect','measure','assess','answer']})
                with trace.traced(run,'retrieve',{'query':parsed.model_dump(mode='json')},state='retrieve') as t:
                    yield step_frame(t,'retrieve','Retrieve candidate moments')
                    if parsed.place:
                        place = yield from call(t,'geocode','retrieve',{'name':parsed.place},
                                                lambda:tools.geocode(store,parsed.place))
                        if place.envelope.output is None:
                            final,stop_reason = 'OUT_OF_RANGE','abstained'
                        else:
                            passes = yield from call(t,'find_passes','retrieve',{'place_id':place.envelope.output['place_id'],'r_m':geo.NEAR_M,'fov_deg':360},
                                                     lambda:tools.find_passes(store,place.envelope.output['place_id'],geo.NEAR_M,360))
                            if not passes.envelope.output:
                                final,stop_reason='OUT_OF_RANGE','abstained'
                    if stop_reason!='abstained':
                        if speeding_q:
                            near = passes.envelope.output if parsed.place else None
                            res=yield from call(t,'speeding_candidates','retrieve',{'scene_key':parsed.scene_key,'t_range':parsed.t_range,'passes':near},
                                                lambda:tools.speeding_candidates(store,scoped,parsed.t_range,passes=near))
                            moments=[]
                            for c,ev in zip(res.envelope.output['candidates'],res.evidence):
                                cams=next(s['cams'] for s in scoped if s['scene_key']==c['scene_key'])
                                moments.append(MomentCard(moment_id=f"m_{c['scene_key']}_{c['t_ms']}",scene_key=c['scene_key'],t0_ms=c['t_ms'],
                                                          t1_ms=c['t_ms']+1,cams=[x['id'] for x in cams],primary_cam=cams[0]['id'],actor_refs=[],
                                                          evidence_ids=[ev.eid],score=c['over_kmh'],why=['OBD speed minus map speed limit'],
                                                          thumb=store.asset_url(c['scene_key'],f"{cams[0]['thumbs']}/{c['t_ms']//1000:05d}.jpg"),
                                                          backend=getattr(store,'backend','duckdb'),exec_state='live'))
                            final, no_limit = 'UNRESOLVED', not moments
                        elif parsed.template=='where_is' and body.context.get('t_ms') is not None and not parsed.place:
                            key=parsed.scene_key or scoped[0]['scene_key']
                            tm=body.context['t_ms']
                            cam=store.manifest(key)['cams'][0]['id']
                            res=yield from call(t,'get_synced_views','retrieve',{'scene_key':key,'t_ms':tm,'cam':cam},
                                                lambda:tools.get_synced_views(store,key,tm,cam=cam))
                            ids=[e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)] or body.context.get('selection',[])
                            actors=[EntityRef(scene_key=key,entity_id=e.entity_id,kind=e.kind) for e in store.entities(key)
                                    if e.entity_id in ids and any(o['t0_ms']<=tm<o['t1_ms'] for o in e.observations)]
                            moments=[MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam],
                                                primary_cam=cam,actor_refs=actors,evidence_ids=[e.eid for e in res.evidence],
                                                score=1,why=['entity observation at requested timestamp'],
                                                thumb=res.envelope.output['cams'][0]['frame_url'] if res.envelope.output['cams'] else None,
                                                backend=getattr(store,'backend','duckdb'),exec_state='live')] if actors else []
                            final='UNRESOLVED' if moments else 'NO_MATCH'
                        else:
                            filters = {'workspace':parsed.workspace,'scene_key':parsed.scene_key,'t_range':parsed.t_range,
                                       'place':parsed.place,'zone':parsed.zone}
                            res = yield from call(t,'search','retrieve',{'q':body.q,'filters':filters},
                                                  lambda:tools.search(store,body.q,filters,parsed=parsed,vss=vss))
                            moments = [MomentCard.model_validate(m) for m in res.envelope.output['moments']]
                            final = res.envelope.output.get('abstain','UNRESOLVED') if moments else res.envelope.output.get('abstain','NO_MATCH')
                            if final=='NO_MATCH' and parsed.place:
                                video = {s['scene_key'] for s in scoped if s['cams']}
                                searched = [p for p in passes.envelope.output if p['trip_id'] in video]
                                swept = yield from call(t,'searched_passes','retrieve',{'passes':searched,'t_range':parsed.t_range},
                                                        lambda:tools.searched_passes(store,searched,parsed.t_range))
                                covered = swept.evidence
                                if covered:
                                    first = swept.envelope.output[0]
                                    yield frame('ui',cmd='seek',args={'t_ms':first['t0_ms'],'scene_key':first['trip_id']})
                        if not moments and not speeding_q:
                            stop_reason='abstained'
                        t.output(res.envelope.output)
                if moments:
                    store.set_hits(run.run_id,moments)
                    yield frame('hits',query_id=run.run_id,moments=[m.model_dump(exclude_none=True) for m in moments])
                    for card_index, card in enumerate(moments):
                        check_budget()
                        claim_id='c'+str(card_index+1)
                        key, tm, cam = card.scene_key,card.t0_ms,card.primary_cam
                        yield frame('ui',cmd='seek',args={'t_ms':tm,'scene_key':key})
                        if card.actor_refs:
                            yield frame('ui',cmd='highlight',args={'ids':[a.entity_id for a in card.actor_refs],'mode':'outline'})
                        manifest = store.manifest(key)
                        entities = store.entities(key)
                        subjects = card.actor_refs or [EntityRef(scene_key=key,entity_id=e.entity_id,kind=e.kind) for e in entities]
                        related = store.events_in(key,tm,tm+1)
                        has_distance = any(w in body.q.lower() for w in ['how far','how close','how many meters','distance','gap','near miss'])
                        speeding = manifest['workspace']=='fleet' and any(w in body.q.lower() for w in ['speeding'])
                        vehicle = None
                        if has_distance:
                            ids=[e for e in parsed.entities if re.fullmatch(r'[FV]\d+',e)]
                            if not ids:
                                # the card's event and the events sharing its actors at this moment
                                actors={a.entity_id for a in card.actor_refs}
                                ids=[*actors,*(i for e in related if actors&set(e.actor_ids) for i in e.actor_ids)]
                            vehicles=[EntityRef(scene_key=key,entity_id=e.entity_id,kind=e.kind) for e in entities
                                      if e.entity_id in ids and e.kind in {'forklift','vehicle'}]
                            vehicle=vehicles[0] if len(vehicles)==1 else None
                            subjects=list({s.entity_id:s for s in [*subjects,*([vehicle] if vehicle else [])]}.values())
                        threshold = re.search(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',body.q,re.I)
                        if speeding:
                            operator, value, unit = 'gt', None, 'km/h'
                        elif has_distance and not threshold and not any(w in body.q.lower() for w in ['near miss']):
                            operator, value, unit = 'measured', None, 'm'
                        elif has_distance:
                            operator, unit = 'lt', 'm'
                            value = float(threshold[1]) if threshold else next((e.params['threshold_m'] for e in related if 'threshold_m' in e.params),2.)
                        else:
                            operator, value, unit = 'event', 'observed', None
                        predicates = [Predicate(predicate_id='p1',subject_refs=[] if speeding else subjects,operator=operator,
                                                value=value,unit=unit,status='unknown',eids=[])]
                        measure_result = None
                        with trace.traced(run,'assess',{'scene_key':key,'t_ms':tm,'cam':cam},state='inspect') as t:
                            yield step_frame(t,'inspect','Inspect the primary camera and spatial evidence')
                            yield from call(t,'get_synced_views','inspect',{'scene_key':key,'t_ms':tm,'cam':cam},
                                            lambda:tools.get_synced_views(store,key,tm,cam=cam))
                            person = next((s for s in subjects if s.kind=='person'),None)
                            vis = None
                            if manifest['workspace']=='warehouse' and person:
                                vis = yield from call(t,'visibility','inspect',{'scene_key':key,'t_ms':tm,'target':person.entity_id,'cam':cam},
                                                      lambda:tools.visibility(store,key,tm,person.entity_id,cam=cam))
                            if has_distance and person and vehicle:
                                measure_result = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':person.model_dump(),'b':vehicle.model_dump(),'kind':'distance','cam':cam},
                                                                 lambda:tools.measure(store,key,tm,person,vehicle,'distance',cam=cam))
                            elif speeding:
                                speed = yield from call(t,'speed_limit','measure',{'scene_key':key,'t_ms':tm},lambda:tools.speed_limit(store,key,tm))
                                limit = speed.envelope.output.get('value')
                                predicates[0].value = limit
                                if limit is not None:
                                    predicates[0].status = 'supported' if speed.envelope.output['speed_kmh']>limit else 'contradicted'
                                predicates[0].eids = [e.eid for e in speed.evidence]
                            elif parsed.template=='link':
                                ids=[e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)] or body.context.get('selection',[])
                                valid = len(set(ids))==1 and all(e.association['valid'] for e in entities if e.entity_id in ids)
                                valid &= all(c['sync_method']!='unknown' for c in manifest['cams']) and bool(ids)
                                predicates[0].status='supported' if valid else 'unknown'
                                predicates[0].eids=card.evidence_ids
                            elif parsed.template=='where_is' and not parsed.place and body.context.get('t_ms') is not None:
                                observed=vis.envelope.output.get('person',{}).get('observed_in',[]) if vis else []
                                predicates[0].operator='located'
                                predicates[0].status='supported' if cam in observed else 'unknown'
                                predicates[0].eids=[e.eid for e in vis.evidence] if vis else []
                            elif not has_distance:
                                wanted = event_types(body.q)
                                predicates[0].status='supported' if wanted and any(e.type in wanted for e in related) else 'unknown'
                                predicates[0].eids=[eid for eid in card.evidence_ids if read[eid].kind=='measure']
                            if measure_result and measure_result.evidence:
                                ev = measure_result.evidence[0]
                                predicates[0].status='supported' if operator=='measured' or ev.value+ev.sigma<predicates[0].value else 'contradicted' if ev.value-ev.sigma>=predicates[0].value else 'unknown'
                                predicates[0].eids=[ev.eid]
                            t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                        yield claim_frame(predicates)
                        candidates = []
                        if vis and vis.envelope.output.get('floor_cell'):
                            seen = vis.envelope.output['floor_cell']['seen_by']
                            observed = vis.envelope.output.get('person',{}).get('observed_in',[])
                            height_max = max(c['cam2world'][14] for c in manifest['cams'])
                            candidates = [{'cam':c['id'],'gain':c['cam2world'][14]/height_max if c['id'] in seen and c['id'] in observed else 0,
                                           'reason':'visible floor cell and tracked observation' if c['id'] in seen and c['id'] in observed else 'no additional observation'}
                                          for c in manifest['cams'] if c['id']!=cam and c['sync_method']!='unknown']
                        with trace.traced(run,'policy.plan_next',{'aggregate':aggregate_of(predicates),'tools_used':count},state='plan') as t:
                            yield step_frame(t,'plan','Choose the next evidence by coverage gain')
                            decision = policy.plan_next({'aggregate':aggregate_of(predicates),'tools_left':body.budget.tools-count,
                                                         'seconds_left':body.budget.seconds-(time.monotonic()-start),'candidates':candidates})
                            t.output(decision)
                        if decision.get('cam') and has_distance and person and vehicle:
                            cam = decision['cam']
                            with trace.traced(run,'assess',{'scene_key':key,'t_ms':tm,'cam':cam},state='measure') as t:
                                yield step_frame(t,'measure','Read the next camera and measure distance')
                                yield from call(t,'get_synced_views','inspect',{'scene_key':key,'t_ms':tm,'cam':cam},lambda:tools.get_synced_views(store,key,tm,cam=cam))
                                measure_result = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':person.model_dump(),'b':vehicle.model_dump(),'kind':'distance','cam':cam},
                                                                 lambda:tools.measure(store,key,tm,person,vehicle,'distance',cam=cam))
                                if measure_result.evidence:
                                    ev = measure_result.evidence[0]
                                    predicates[0].status='supported' if operator=='measured' or ev.value+ev.sigma<predicates[0].value else 'contradicted' if ev.value-ev.sigma>=predicates[0].value else 'unknown'
                                    predicates[0].eids=[ev.eid]
                                t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                        if measure_result and measure_result.evidence:
                            yield frame('ui',cmd='highlight',args={'ids':[person.entity_id,vehicle.entity_id],'mode':'outline'})
                        with trace.traced(run,'assess',{'predicates':[p.model_dump(mode='json') for p in predicates]},state='assess') as t:
                            yield step_frame(t,'assess','Check the situation and predicate states')
                            explain = {}
                            if subjects and not speeding and not (parsed.template=='where_is' and not parsed.place and body.context.get('t_ms') is not None):
                                situation = yield from call(t,'situation','assess',{'scene_key':key,'t_ms':tm,'actor_refs':[s.model_dump() for s in subjects]},
                                                            lambda:tools.situation(store,key,tm,subjects,run=run,circuit=circuit))
                                record = situation.envelope.output.get('record')
                                status = 'unknown' if situation.envelope.exec_state=='failed' or record['verification']['status']!='verified' else 'supported'
                                expected='yielding' if any(e.type=='dodge' for e in related) else 'crossing' if any(e.type=='ped_crossing' for e in related) else None
                                conflict=bool(record and (record['verification']['status']=='conflict' or
                                                          expected and record['payload']['label']!=expected))
                                if conflict:
                                    status='unknown'
                                # a failed call stays an unknown predicate; an unverified record is only explained
                                if record is None or record['verification']['status']=='verified':
                                    predicates.append(Predicate(predicate_id='p2',subject_refs=subjects,operator='situation',
                                                                value=record['payload']['label'] if record else None,unit=None,status=status,
                                                                eids=[e.eid for e in situation.evidence]))
                                if record:
                                    explain['c1']=record['record_id']
                                    if conflict:
                                        explain['conflict']=True
                            before = next(c.aggregate for c in claims if c.claim_id==claim_id)
                            final = aggregate_of(predicates)
                            yield claim_frame(predicates,before,explain)
                            t.output({'before':before,'after':final,'explain':explain})
                            distance = predicates[0].operator in {'lt','le','measured'} and any(read[e].method=='ground_distance' for e in predicates[0].eids)
                            if final=='SUPPORTED' and distance and not explain.get('conflict'):
                                for event in related:
                                    if event.severity>=2 and {person.entity_id,vehicle.entity_id}<=set(event.actor_ids):
                                        req = TriggerRequest(scene_key=key,event_id=event.event_id,run_id=run.run_id,claim_id=claim_id,auto=True,
                                                             actions=['create_review_task','hazard_zone','wandb_alert'])
                                        try:
                                            action = yield from call(t,'actions.trigger','assess',req.model_dump(),lambda:tools.trigger(store,req))
                                        except BudgetExhausted:
                                            raise
                                        except Exception as exc:
                                            # a failed action does not undo the decided claim
                                            logger.exception('auto trigger %s failed in ask %s',event.event_id,run.run_id)
                                            step=max((s for s in store.get_run(run.run_id).steps if s.exec_state=='failed'),key=lambda s:s.i)
                                            step.note=f'{type(exc).__name__}: {exc}'
                                            store.update_step(run.run_id,step)
                                            continue
                                        ui_args={'kind':'label','t_ms':tm,'text':'Action results recorded'}
                                        if action.envelope.output['zone_id'] is not None:
                                            ui_args.update(kind='zone',zone_id=action.envelope.output['zone_id'])
                                        yield frame('ui',cmd='annotate',args=ui_args)
                            stop_reason = 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                        # candidates come highest excess first, so the first decided one answers the question
                        if speeding and final in {'SUPPORTED','CONTRADICTED'}:
                            break
                    final=aggregate_of([p for c in claims for p in c.predicates])
                elif final not in {'UNSUPPORTED','NO_MATCH','OUT_OF_RANGE'} and not speeding_q:
                    final,stop_reason='NO_MATCH','abstained'
    except BudgetExhausted:
        final=aggregate_of([p for c in claims for p in c.predicates])
        stop_reason='budget_exhausted'
    except GeneratorExit:
        trace.finish_run(run,'aborted',final=None,claims=claims)
        finished = True
        raise
    except Exception as exc:
        logger.exception('ask %s failed',run.run_id)
        final,stop_reason='UNRESOLVED','error'
        failed=[s for s in store.get_run(run.run_id).steps if s.exec_state=='failed']
        if failed:
            step=max(failed,key=lambda s:s.i)
            step.note=f'{type(exc).__name__}: {exc}'
            store.update_step(run.run_id,step)
    finally:
        if finished:
            return
    try:
        with trace.traced(run,'answer',{'final':final,'read_eids':sorted(run.read_eids)},state='answer') as t:
            yield step_frame(t,'answer','Generate an answer from the evidence read')
            text = {'UNSUPPORTED':'Identity, intent or responsibility questions are out of scope.','NO_MATCH':'No matching evidence was retrieved.',
                    'OUT_OF_RANGE':'The requested time or place is out of range of the footage.','UNRESOLVED':'There is insufficient evidence to confirm this.',
                    'CONTRADICTED':'The measured evidence contradicts the predicate.','SUPPORTED':'The evidence supports the predicate.'}[final]
            refs, cites, values, names = [], [], [], []
            def ref(e, field, value):
                refs.append({'eid':e.eid,'field':field}); cites.append(e.eid); values.append(value)
                return '{{m'+str(len(values)-1)+'}}'
            def name(value):
                # identifiers such as ceiling_00 or P1 bypass the number sanitizer
                names.append(value)
                return '{{n'+str(len(names)-1)+'}}'
            limit = next((e for e in read.values() if e.method=='osm_maxspeed'),None)
            if final=='UNRESOLVED' and (limit and limit.value is None or no_limit):
                text='This road segment lacks map speed limit data, so speeding cannot be determined.'
            if final=='UNRESOLVED' and building:
                text=('No map building facade '+SIDES[building['side']]+' was found in the videos passing '+name(parsed.place)+'.' if building['video']
                      else 'The trips passing '+name(parsed.place)+' have no video, so buildings on either side cannot be viewed.')
            if final=='UNRESOLVED' and closest and closest['missing']:
                text='No '+' and '.join(noun(k,'warehouse') for k in closest['missing'])+' tracks are available in the selected range, so the closest distance between the worker and forklift cannot be measured.'
            if final=='NO_MATCH' and covered:
                workspace=body.context.get('workspace')
                kind=next(iter(event_types(body.q) or ()),None)
                what='the '+noun('person',workspace)+' '+VERBS[kind].replace('{other}','the '+noun('vehicle',workspace)) if kind in VERBS else 'a matching event occurred'
                count,*intervals=covered
                text=('Checked '+ref(count,'value.count',count.value['count'])+' video(s) passing '+name(parsed.place)+' ('+'; '.join(
                    name(trip_name(e.scene_key))+', '+ref(e,'value.t0_s',e.value['t0_s'])+'–'+ref(e,'value.t1_s',e.value['t1_s'])+' s'
                    for e in intervals)+'); no evidence that '+what+'.')
            if final in {'SUPPORTED','CONTRADICTED'}:
                cites=list(dict.fromkeys(eid for c in claims for p in c.predicates for eid in p.eids))
                predicates=[p for c in claims for p in c.predicates]
                workspace=body.context.get('workspace')
                blind=next((p for p in predicates if p.operator=='present_in_blind_zone'),None)
                facade=next((p for p in predicates if p.operator=='facade'),None)
                speed = next((e for e in read.values() if e.method=='obd_speed'),None)
                if facade:
                    text='The building '+SIDES[building['side']]+' at '+name(parsed.place)+' is '+name(facade.value)+'.'
                elif blind:
                    found=sorted((read[e] for p in predicates if p.operator=='present_in_blind_zone' for e in p.eids),
                                 key=lambda e:(e.t0_ms,e.value['entity_id']))
                    if found:
                        text='People are present in the '+name(blind.value)+' blind spot: '+'; '.join(
                            'the '+noun('person',workspace)+' '+name(e.value['entity_id'])+' at '+ref(e,'value.t0_s',e.value['t0_s'])+'–'
                            +ref(e,'value.t1_s',e.value['t1_s'])+' s ('+('visible to '+name(', '.join(e.value['seen_by'])) if e.value['seen_by'] else 'not visible to any other camera')+')'
                            for e in found)+'.'
                    else:
                        text='Checked frame by frame: nobody is present in the '+name(blind.value)+' blind spot.'
                elif closest:
                    ev, scan = read[predicates[0].eids[0]], closest['scan']
                    text=('The worker and forklift were closest at a distance of '+ref(ev,'value',ev.value)+' m (±'+ref(ev,'sigma',ev.sigma)+' m), at '
                          +ref(scan,'value.t_s',scan.value['t_s'])+' s'
                          +('' if predicates[0].operator!='lt' else ', less than the requested distance' if final=='SUPPORTED' else ', not less than the requested distance')+'.')
                elif speed and limit and limit.value is not None:
                    text=('The vehicle was speeding: ' if final=='SUPPORTED' else 'The vehicle was not speeding: ')+'OBD speed '+ref(speed,'value',speed.value)+' km/h, map speed limit '+ref(limit,'value',limit.value)+' km/h.'
                else:
                    subjects=[s for p in predicates for s in p.subject_refs]
                    person=next((s for s in subjects if s.kind=='person'),subjects[0] if subjects else None)
                    other=next((s for s in subjects if s.kind in {'forklift','vehicle'}),None)
                    who, whom = noun(person.kind if person else None,workspace), noun(other.kind,workspace) if other else 'other party'
                    wanted=event_types(body.q)
                    when=re.search(r'\bwhen\b',body.q,re.I)
                    event=next((e for e in read.values() if e.kind=='measure' and e.method.startswith('event_') and isinstance(e.value,dict)
                                and 't_s' in e.value and (wanted and e.value.get('type') in wanted or not wanted and when)),None)
                    ev = next((e for e in read.values() if e.kind=='measure' and e.method=='ground_distance'),None)
                    clause=''
                    if event:
                        verb=VERBS.get(event.value.get('type'))
                        at=ref(event,'value.t_s',event.value['t_s'])
                        clause='The '+who+' '+verb.replace('{other}','the '+whom)+' at '+at+' s' if verb else 'The event started at '+at+' s in the scene'
                    if ev:
                        clause+=(', ' if clause else 'The '+who+' was ')+ref(ev,'value',ev.value)+' m (±'+ref(ev,'sigma',ev.sigma)+' m) from the '+whom
                    if clause:
                        text=clause+'.' if final=='SUPPORTED' else text+' '+clause+'.'
            cites=list(dict.fromkeys(cites))
            parts = re.split(r'(\{\{[mn]\d+\}\})',text)
            text=''.join(p if re.fullmatch(r'\{\{[mn]\d+\}\}',p) else llm.sanitize_numbers(p,[]) for p in parts)
            for i,value in enumerate(values):
                text=text.replace('{{m'+str(i)+'}}',format(value,'.4g'))
            for i,value in enumerate(names):
                text=text.replace('{{n'+str(i)+'}}',value)
            answer = dict(sentence_id='a1',text=text,claim_ids=[c.claim_id for c in claims],cites=cites,measure_refs=refs)
            t.output(answer)
            yield frame('answer',**answer)
        weave_url=trace.finish_run(run,'error' if stop_reason=='error' else 'done',final=final,claims=claims)
        finished=True
        yield frame('status',final=final,cached=False,source_run_id=None,weave_url=weave_url,stop_reason=stop_reason)
        if native is not None:
            # the run is already finalized; this step is still appended to its persisted steps
            with trace.traced(run,'vss.search',{'q':body.q},state='retrieve') as t:
                data = native(max(0,min(body.budget.seconds-(time.monotonic()-start),NATIVE_WAIT_S)))
                t.output(data)
            yield frame('vss_native',**data)
        yield frame('done')
    finally:
        if not finished:
            trace.finish_run(run,'aborted',final=None,claims=claims)


@router.post('/api/agent/ask')
def ask(request: Request, body: AskRequest):
    if body.context.get('workspace') not in {'warehouse','fleet'}:
        raise HTTPException(422,'context.workspace must be warehouse or fleet')
    settings=getattr(request.app.state,'settings',None)
    if getattr(settings,'mode','dev')=='site' and getattr(request.app.state,'vss',None) is None:
        raise HTTPException(503,'site VSS is not configured')
    store=request.app.state.store
    def sse(frame):
        return 'event: '+frame['event']+'\ndata: '+json.dumps(frame['data'],ensure_ascii=False)+'\n\n'
    def stream():
        hit=cache.replay(store,body.q,body.context)
        if hit:
            yield from map(sse,hit[1])
            return
        frames=[]
        for frame in ask_frames(store,body,vss=getattr(request.app.state,'vss',None)):
            frames.append(frame)
            yield sse(frame)
        status=next((f['data'] for f in frames if f['event']=='status'),None)
        if status and status['stop_reason'] in {'enough_evidence','no_new_info','abstained'}:
            cache.put(store,body.q,body.context,frames,getattr(store,'backend','duckdb'),frames[0]['data']['run_id'])
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})


@router.get('/api/agent/runs/{run_id}')
def get_run(request: Request, run_id: str):
    run=request.app.state.store.get_run(run_id)
    if run is None:
        raise HTTPException(404,'run not found')
    return run


@router.get('/api/agent/runs')
def list_runs(request: Request, scene_key: str | None = None, limit: int = Query(20, le=200)):
    return request.app.state.store.list_runs(scene_key,limit)


@router.post('/api/agent/confirm')
def confirm(request: Request, body: ConfirmRequest):
    run=request.app.state.store.get_run(body.run_id)
    if run is None:
        raise HTTPException(404,'run not found')
    record={'title':'Event Review Record','run_id':run.run_id,'verdict':body.verdict,'note':body.note,
            'final':run.final,'claims':[c.model_dump(mode='json') for c in run.claims],
            'steps':[{'i':s.i,'state':s.state,'name':s.name,'output':s.output} for s in run.steps]}
    page='<html lang="en"><meta charset="utf-8"><title>Event Review Record</title><h1>Event Review Record</h1><pre>'+html.escape(json.dumps(record,ensure_ascii=False,indent=2))+'</pre></html>'
    return {'record':record,'html':page,'record_url':'data:text/html;base64,'+base64.b64encode(page.encode()).decode()}
