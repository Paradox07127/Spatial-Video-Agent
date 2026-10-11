"""Deterministic investigation, streamed as contract SSE frames."""
from __future__ import annotations

import base64
import functools
import html
import json
import logging
import re
import threading
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.search import event_types
from app.core import cache, geo, llm, policy, street, tools, trace
from app.core.investigation_context import apply_constraints
from app.core.deadline import DeadlineExceeded, remaining, scope as deadline_scope
from app.core.models import ABSTAIN_TEXT, Claim, EntityRef, MomentCard, Predicate, TriggerRequest, aggregate_of, capabilities, level_of, tag_of, Workspace, Scope, Tag
from app.settings import Settings

router = APIRouter()
logger = logging.getLogger(__name__)
VERBS = {'dodge':'started to dodge','near_miss':'approached {other}','ped_crossing':'crossed in front of the car','ped_near':'appeared in front of the car'}
SIDES = {'right':'on the right','left':'on the left',None:''}
NATIVE_WAIT_S = 10
LEVELS = ('L3','L2','L1')
DISTANCE_WORDS = ['how far','how close','how many meters','distance','gap','near miss','close to','next to']
TEXT = {'UNSUPPORTED':'Identity, intent or responsibility questions are out of scope.','NO_MATCH':'No matching evidence was retrieved.',
        'OUT_OF_RANGE':'The requested time or place is out of range of the footage.','UNRESOLVED':'There is insufficient evidence to confirm this.',
        'CONTRADICTED':'The measured evidence contradicts the predicate.','SUPPORTED':'The evidence supports the predicate.'}
SCENES = {'warehouse':'Warehouse scenes','fleet':'Fleet trips','street':'Street cameras'}
COCO = ('person','bicycle','car','motorcycle','airplane','bus','train','truck','boat','traffic light','fire hydrant','stop sign',
        'parking meter','bench','bird','cat','dog','horse','sheep','cow','elephant','bear','zebra','giraffe','backpack','umbrella',
        'handbag','tie','suitcase','frisbee','skis','snowboard','sports ball','kite','baseball bat','baseball glove','skateboard',
        'surfboard','tennis racket','bottle','wine glass','cup','fork','knife','spoon','bowl','banana','apple','sandwich','orange',
        'broccoli','carrot','hot dog','pizza','donut','cake','chair','couch','potted plant','bed','dining table','toilet','tv',
        'laptop','mouse','remote','keyboard','cell phone','microwave','oven','toaster','sink','refrigerator','book','clock','vase',
        'scissors','teddy bear','hair drier','toothbrush')
VEHICLES = ['bicycle','bus','car','motorcycle','truck']


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


@functools.cache
def site_rows():
    """The site-map rows ({source, scene_key, cam_id, t_ms}) of the live VSS segments."""
    from pipeline.common import site_map
    return list(site_map.load(Settings().site_map_path)[0].values())


def trip_name(key):
    # the fleet page's short trip label
    return re.sub(r'^fl-pie-(set\d+)_video_(\d+)$',r'\1·\2',key)


def noun(kind, workspace):
    """The word for an entity kind in the scene's workspace."""
    if kind=='person':
        return {'warehouse':'worker','street':'person'}.get(workspace,'pedestrian')
    return {'forklift':'forklift','vehicle':'vehicle'}.get(kind,'object')


def coco_groups(q, entities):
    """{name: COCO labels} the question asks about; a generic vehicle is any of VEHICLES."""
    groups = {'person':['person']} if 'person' in entities else {}
    if re.search(r'\bvehicles?\b',q,re.I):
        groups['vehicle'] = VEHICLES
    for label in COCO:
        if label not in groups and re.search(r'\b'+re.escape(label)+r'(?:e?s)?\b',q,re.I):
            groups[label] = [label]
    return groups


def spatial_of(q, template):
    """(capability, operator, value, unit) of the spatial predicate the question needs, or None."""
    text = q.lower()
    threshold = re.search(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',q,re.I)
    if threshold or any(w in text for w in DISTANCE_WORDS) or re.search(r'\b(closest|nearest|minimum|beside|adjacent)\b|\bnear (?:a|an|the) (?:person|worker|pedestrian|forklift|car|vehicle)\b',text):
        if threshold:
            operator = ('ge' if re.search(r'\bat least\b',text) else 'le' if re.search(r'\bat most\b',text) else
                        'gt' if re.search(r'\b(greater than|more than|over|farther than)\b',text) else 'lt')
            return 'distance',operator,float(threshold[1]),'m'
        return ('distance','lt',2.,'m') if 'near miss' in text else ('distance','measured',None,'m')
    if re.search(r"can'?t see|cannot see|blind",q,re.I):
        return 'blind_spot','present_in_blind_zone',None,None
    # the contract table gives speeding and building questions the no_gps reason
    if 'speed' in text:
        return 'speed','gt',None,'km/h'
    if re.search(r'\bbuilding\b',text):
        return 'speed','facade',None,None
    if template=='where_is':
        return 'ground_position','located',None,None
    if template=='link':
        return 'tracks','same_entity',None,None
    return None


def moving_target(q, subjects, vehicle=None):
    """Bind a single explicit moving modifier to the vehicle it actually describes."""
    if len(re.findall(r'\bmoving\b',q,re.I))!=1:
        return None
    match = re.search(r'\bmoving\s+(forklift|car|vehicle|[FV]\d+)\b',q,re.I)
    if not match:
        return None
    token = match[1].lower()
    candidates = {s.entity_id:s for s in [*subjects,*([vehicle] if vehicle else [])] if s.kind in {'forklift','vehicle'}}
    matching = [s for s in candidates.values() if
                (s.entity_id.lower()==token if re.fullmatch(r'[fv]\d+',token) else
                 s.kind=='forklift' if token=='forklift' else s.kind=='vehicle' if token=='car' else True)]
    return matching[0] if len(matching)==1 else None


def abstained(claim):
    return bool((claim.explain or {}).get('abstain'))


def distance_status(ev, operator, value):
    if ev is None:
        return 'unknown'
    if operator=='measured':
        return 'supported'
    low, high = ev.value-ev.sigma, ev.value+ev.sigma
    supported, contradicted = {'lt':(high<value,low>=value),'le':(high<=value,low>value),
                               'gt':(low>value,high<=value),'ge':(low>=value,high<value)}[operator]
    return 'supported' if supported else 'contradicted' if contradicted else 'unknown'


class Budget(BaseModel):
    model_config = ConfigDict(extra='forbid')
    tools: int = Field(default=8, ge=0, le=16)
    seconds: float = Field(default=25, ge=0, le=45)


class AskContext(BaseModel):
    model_config = ConfigDict(extra='forbid')
    workspace: Workspace | None = None
    scene_key: str | None = Field(default=None,pattern=r'^[a-z0-9_-]+$')
    scene_id: str | None = Field(default=None,pattern=r'^(wh|fl|st)/[a-z0-9_]+/[a-zA-Z0-9_-]+$')
    place: str | None = None
    t_ms: Annotated[int, Field(strict=True,ge=0)] | None = None
    t_range: tuple[Annotated[int, Field(strict=True,ge=0)], Annotated[int, Field(strict=True,ge=0)]] | None = None
    selection: list[Annotated[str, Field(strict=True)]] | None = None
    zone: list[tuple[Annotated[float, Field(strict=True,allow_inf_nan=False)], Annotated[float, Field(strict=True,allow_inf_nan=False)]]] | None = Field(default=None,min_length=3)
    scope: Scope = 'scene'
    sources: list[Tag] | None = None

    @model_validator(mode='after')
    def ordered_range(self):
        if self.workspace is None:
            if self.scope!='corpus':
                raise ValueError('context.workspace is required unless scope is corpus')
            # the parser needs a workspace; corpus scope does not read it
            self.workspace = 'warehouse'
        if self.t_range and self.t_range[0]>=self.t_range[1]:
            raise ValueError('context.t_range must have increasing bounds')
        if self.scene_key and self.scene_id and self.scene_key!=self.scene_id.replace('/','-').lower():
            raise ValueError('context.scene_key must match scene_id')
        return self


class AskRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    q: str = Field(min_length=1, max_length=500)
    context: AskContext
    budget: Budget = Field(default_factory=Budget)


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    run_id: str
    verdict: Literal['confirm','reject','edit']
    note: str = ''


class BudgetExhausted(Exception):
    pass


class LevelBudget(BudgetExhausted):
    """Corpus scope: the tools left are kept for the top moment of each later level; only this moment stops."""


def ask_frames(store, body: AskRequest, *, vss=None):
    context = body.context.model_dump(exclude_unset=True)
    start = time.monotonic()
    run = trace.start_run(store,body.q,context)
    run.deadline = start+body.budget.seconds
    # site only: VSS's own retrieval, shown next to the answer for comparison; the answer never waits for it
    native = native_search(vss,body.q) if vss is not None else None
    claims, frames, read, moments = [], [], {}, []
    count = 0
    final, stop_reason = 'UNRESOLVED', 'no_new_info'
    circuit = {'failures':0}
    finished = False
    no_limit = False
    speed_coverage, speed_note = None, None
    covered, building, closest = [], None, None
    claim_id = 'c1'
    reserve, cut, levels = 0, False, {}
    corpus, groups, spatial = False, {}, None
    sent_hits, auto_open = False, None
    quantifier = 'any'
    parsed = None
    statistics_complete = None
    ego_measured = False

    def frame(event, **data):
        f = {'event':event,'data':{'run_id':run.run_id,**data}}
        frames.append(f)
        return f

    def step_frame(t, state, summary):
        t.exec_state('precomputed')
        return frame('step',step_id=t.step_id,i=t.i,state=state,
                     elapsed_ms=round((time.monotonic()-start)*1000),summary=summary,exec_state='waiting')

    def ui(cmd, args, key):
        # once hits are sent the view moves only inside their auto_open scene; across scenes the shell lists candidates
        if sent_hits and cmd!='annotate' and key!=auto_open:
            return
        yield frame('ui',cmd=cmd,args=args,scene_key=key)

    def hits(cards, *, special=False):
        """The hits frame: each moment with its scene's tag and title; a special path's moments also carry workspace and level."""
        nonlocal sent_hits, auto_open
        def card(m):
            manifest = store.manifest(m.scene_key)
            extra = {'workspace':manifest['workspace'],'evidence_level':level_of(manifest)} if special else {}
            return m.model_copy(update={'tag':tag_of(manifest),'title':manifest['title'],**extra})
        cards = [card(m) for m in cards]
        keys = {m.scene_key for m in cards}
        sent_hits, auto_open = True, keys.pop() if len(keys)==1 else None
        store.set_hits(run.run_id,cards)
        return frame('hits',query_id=run.run_id,moments=[{**m.model_dump(exclude_none=True),'thumb':m.thumb} for m in cards],auto_open=auto_open)

    def level(key):
        if key not in levels:
            levels[key] = level_of(store.manifest(key))
        return levels[key]

    def check_budget():
        if count>=body.budget.tools or time.monotonic()-start>=body.budget.seconds:
            raise BudgetExhausted
        if count>=body.budget.tools-reserve:
            raise LevelBudget

    def call(parent, name, state, args, fn):
        nonlocal count
        check_budget()
        count += 1
        with trace.traced(run,'tool.'+name,args,state=state,parent_step_id=parent.step_id) as t:
            yield frame('tool_call',step_id=parent.step_id,call_id=t.step_id,name=name,args=args,why=name)
            try:
                with deadline_scope(run.deadline):
                    res = fn()
            except DeadlineExceeded as exc:
                raise BudgetExhausted from exc
            if res.envelope.exec_state=='failed' and time.monotonic()>=run.deadline:
                raise BudgetExhausted
            res.evidence = [e if e.evidence_level else e.model_copy(update={'evidence_level':level(e.scene_key)}) for e in res.evidence]
            store.add_evidence(run.run_id,res.evidence)
            t.output(res.envelope.model_dump(mode='json'))
            t.evidence([e.eid for e in res.evidence])
            t.exec_state(res.envelope.exec_state)
            if res.envelope.exec_state=='live':
                parent.exec_state('live')
            read.update({e.eid:e for e in res.evidence})
            yield frame('tool_result',step_id=parent.step_id,call_id=t.step_id,ok=res.envelope.exec_state!='failed',
                        envelope=res.envelope.model_dump(mode='json'),evidence=[e.model_dump(mode='json') for e in res.evidence])
            if isinstance(res.envelope.output,dict) and 'record' in res.envelope.output:
                record = res.envelope.output['record']
                yield frame('model_result',scene_key=res.envelope.input_range.scene_key,t_ms=record['t_ms'],results=[record])
            return res

    def claim_frame(predicates, before=None, explain=None, *, key=None, moment=None, lv=None, text='Spatial and event evidence check'):
        # A condition retained by parsing must either be measured or remain unknown.
        conditions = parsed.constraints if parsed else []
        for condition in conditions:
            if not any(p.operator == condition for p in predicates):
                predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[],
                                            operator=condition,value=None,unit=None,status='unknown',eids=[]))
        if 'negation' in conditions:
            for p in predicates:
                p.status = 'unknown'
        unknown = [p.operator for p in predicates if p.status=='unknown']
        if unknown:
            explain = {**(explain or {}),'not_measured':list(dict.fromkeys([*(explain or {}).get('not_measured',[]),*unknown]))}
        aggregate = aggregate_of(predicates)
        claim = Claim(run_id=run.run_id,claim_id=claim_id,text=text,predicates=[p.model_copy(deep=True) for p in predicates],
                      aggregate=aggregate,before=before,after=aggregate,examined_eids=list(read),
                      available_eids=[],explain=explain,scene_key=key,moment_id=moment,evidence_level=level(key) if key else lv)
        claims[:]=[c for c in claims if c.claim_id!=claim_id]+[claim]
        return frame('claim',**{k:v for k,v in claim.model_dump(mode='json').items() if k!='run_id'})

    def counted(exhausted=False, group=None):
        """Candidate quantifiers apply to claims; predicates inside a claim remain conjunctive."""
        kept = [c for c in (claims if group is None else group) if not abstained(c)]
        aggregates = [c.aggregate for c in kept]
        if quantifier in {'all','none'}:
            # Retrieval candidates are not a certificate of full entity/time coverage.
            return 'UNRESOLVED'
        return ('SUPPORTED' if 'SUPPORTED' in aggregates else
                'CONTRADICTED' if aggregates and all(a=='CONTRADICTED' for a in aggregates) and not exhausted else 'UNRESOLVED')

    def abstain_levels(cap, workspace):
        """Corpus scope: one abstain claim per level whose scenes outside `workspace` lack the capability."""
        nonlocal claim_id
        missing = {}
        for s in store.list_scenes():
            reason = capabilities(s)[cap]['reason']
            if s['workspace']!=workspace and reason and (not parsed.sources or tag_of(s) in parsed.sources):
                missing.setdefault(level_of(s),(s['workspace'],reason))
        for lv in LEVELS:
            if lv in missing:
                where, reason = missing[lv]
                claim_id = 'c'+str(len(claims)+1)
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator=cap,value=None,unit=None,status='unknown',eids=[])],
                                  explain={'abstain':{'reason':reason,'text':ABSTAIN_TEXT[reason]}},lv=lv,text=SCENES[where]+' were not checked')

    def assess_l1(card, top):
        """A street (L1) moment: 2D presence of the asked COCO classes; a spatial predicate abstains with the camera's reason;
        Cosmos C4 only on the top street moment."""
        nonlocal final, stop_reason
        key, cam, t0, t1 = card.scene_key, card.primary_cam, card.t0_ms, card.t1_ms
        yield from ui('seek',{'t_ms':t0,'scene_key':key},key)
        with trace.traced(run,'assess',{'scene_key':key,'cam':cam,'t0_ms':t0,'t1_ms':t1},state='inspect') as t:
            yield step_frame(t,'inspect','Read the 2D detections of the street camera')
            res = yield from call(t,'detections_2d','inspect',{'scene_key':key,'cam':cam,'t0_ms':t0,'t1_ms':t1,'groups':groups},
                                  lambda:tools.detections_2d(store,key,cam,t0,t1,groups))
            predicates =[Predicate(predicate_id='p1',subject_refs=[],operator='detected',value=list(groups),unit=None,
                                    status='supported' if res.envelope.output['frames_together'] else 'unknown',eids=[e.eid for e in res.evidence])]
            explain = {}
            if re.search(r'\b(cross\w*|dodge\w*|yield\w*|brak\w*)\b',body.q,re.I):
                predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[],operator='crossing' if re.search(r'\bcross',body.q,re.I) else 'event',
                                            value=None,unit=None,status='unknown',eids=[]))
                explain['abstain'] = {'reason':'no_geometry','text':'This camera has no geometry; a person detection does not verify the requested action.'}
            if spatial:
                cap, operator, value, unit = spatial
                predicates.append(Predicate(predicate_id='p2',subject_refs=[],operator=operator,value=value,unit=unit,status='unknown',eids=[]))
                reason = capabilities(store.manifest(key))[cap]['reason']
                if reason:
                    explain['abstain'] = {'reason':reason,'text':ABSTAIN_TEXT[reason]}
            t.output({'predicates':[p.model_dump(mode='json') for p in predicates],'explain':explain})
        yield claim_frame(predicates,explain=dict(explain) or None,key=key,moment=card.moment_id)
        if top:
            query = body.q.strip().rstrip('?')
            with trace.traced(run,'assess',{'scene_key':key,'cam':cam,'t0_ms':t0,'t1_ms':t1,'query':query},state='assess') as t:
                yield step_frame(t,'assess','Ask Cosmos when it happens in the street clip')
                res = yield from call(t,'localize','assess',{'scene_key':key,'cam':cam,'t0_ms':t0,'t1_ms':t1,'query':query},
                                      lambda:tools.localize(store,key,cam,t0,t1,query,run=run,circuit=circuit))
                record = res.envelope.output.get('record')
                # Cosmos is model output: it is explained, never a predicate
                if record:
                    before = claims[-1].aggregate
                    explain['c4'] = record['record_id']
                    yield claim_frame(predicates,before,dict(explain),key=key,moment=card.moment_id)
                t.output({'explain':explain})
        final = aggregate_of(predicates)
        stop_reason = 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} and not explain.get('abstain') else 'no_new_info'

    try:
        with trace.traced(run,'parse',{'q':body.q,'context':context},state='parse') as t:
            yield step_frame(t,'parse','Parse the question template')
            parsed = llm.rule_parse(body.q,context)
            if llm.needs_llm(body.q,parsed):
                if time.monotonic()-start>=body.budget.seconds:
                    raise BudgetExhausted
                with trace.traced(run,'llm.parse',{'q':body.q,'rule':parsed.model_dump(mode='json')},state='parse',parent_step_id=t.step_id) as child:
                    with deadline_scope(run.deadline):
                        parsed = llm.parse(body.q,context)
                        remaining(1)
                    child.exec_state('live')
                    t.exec_state('live')
                    child.output(parsed.model_dump(mode='json'))
            parsed, constraint_reason = apply_constraints(body.q,context,parsed)
            quantifier = parsed.quantifier
            if parsed.workspace=='fleet' and not parsed.place:
                parsed = parsed.model_copy(update={'place':geo.named_place(store,body.q)})
            t.output(parsed.model_dump(mode='json'))
        corpus = parsed.scope=='corpus'
        groups, spatial = coco_groups(body.q,parsed.entities), spatial_of(body.q,parsed.template)
        if constraint_reason:
            yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='context_constraint',value=None,
                                         unit=None,status='unknown',eids=[])],
                              explain={'note':constraint_reason,'not_measured':['context_constraint']})
            final, stop_reason = 'UNRESOLVED','abstained'
        elif parsed.template=='unsupported':
            final, stop_reason = 'UNSUPPORTED','abstained'
        else:
            # corpus: every scene; the special paths below gate on the workspace of the scenes, never on the context's
            scenes = store.list_scenes(None if corpus else parsed.workspace)
            scoped = [s for s in scenes if corpus or not parsed.scene_key or s['scene_key']==parsed.scene_key]
            # the special paths scan only the scenes whose tag is in the sources
            tagged = [s for s in scoped if not parsed.sources or tag_of(s) in parsed.sources]
            street_scenes = [s for s in tagged if s['workspace']=='street']
            statistic = next((c for c in parsed.constraints if c in {'tracked_count','peak_count'}),None)
            warehouse = [s for s in tagged if s['workspace']=='warehouse']
            fleet = [s for s in tagged if s['workspace']=='fleet']
            blind_cam = None
            if warehouse and re.search(r"can'?t see|cannot see|blind",body.q,re.I):
                blind_cam = next((c['id'] for s in warehouse for c in s['cams']
                                  if re.search(r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',body.q,re.I)),None)
            blind_scenes = [s for s in warehouse if 'visibility' in s['assets'] and any(c['id']==blind_cam for c in s['cams'])]
            speeding_q = bool(fleet) and any(w in body.q.lower() for w in ['speeding'])
            building_q = bool(fleet) and parsed.place and re.search(r'\bbuilding\b',body.q,re.I)
            requested_kinds = {('person' if re.fullmatch(r'P\d+',e) else 'forklift' if re.fullmatch(r'F\d+',e) else 'vehicle' if re.fullmatch(r'V\d+',e) else e) for e in parsed.entities}
            selection = [e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)]
            closest_q = (bool(warehouse) and {'person','forklift'}<=requested_kinds
                         and (re.search(r'\b(closest|nearest|minimum)\b',body.q,re.I)
                              or re.search(r'\bhow close\b.*\b(come|came|comes|coming|get|got|gets|getting)\b',body.q,re.I))
                         and re.search(r'\b(distance|gap|how far|how many meters|how close|closest|nearest)\b',body.q,re.I))
            if not scoped or (parsed.t_range and (parsed.t_range[0]>=parsed.t_range[1] or
                    not any(store.manifest(s['scene_key'])['t0_ms']<parsed.t_range[1] and
                            store.manifest(s['scene_key'])['t1_ms']>parsed.t_range[0] for s in scoped))):
                final, stop_reason = 'OUT_OF_RANGE','abstained'
            elif context.get('t_ms') is not None and not any(
                    store.manifest(s['scene_key'])['t0_ms']<=context['t_ms']<store.manifest(s['scene_key'])['t1_ms'] for s in scoped):
                final, stop_reason = 'OUT_OF_RANGE','abstained'
            elif warehouse and spatial and spatial[0]=='distance' and re.search(r'\b(footprint|clearance|edge)\b',body.q,re.I):
                _,operator,value,unit = spatial
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator=operator,value=value,
                                             unit=unit,status='unknown',eids=[])],key=warehouse[0]['scene_key'],
                                  explain={'note':'Footprint clearance was not measured. Forklift-center distance is a different quantity.',
                                           'not_measured':['footprint clearance (center distance is a different quantity)']})
                final,stop_reason = 'UNRESOLVED','abstained'
            elif (speeding_q or building_q) and (selection or parsed.zone):
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='selected_entity_or_zone',value=None,
                                             unit=None,status='unknown',eids=[])],
                                  explain={'note':'This fleet path cannot apply the selected entity or spatial zone.'})
                final,stop_reason = 'UNRESOLVED','abstained'
            elif (parsed.template=='where_is' and context.get('t_ms') is not None and not parsed.place
                  and store.manifest(parsed.scene_key or scoped[0]['scene_key'])['workspace']=='fleet'
                  and not store.manifest(parsed.scene_key or scoped[0]['scene_key'])['cams']
                  and (selection or parsed.zone or any(e!='vehicle' for e in parsed.entities)
                       or spatial and spatial[0]!='ground_position')):
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='located',value=None,
                                             unit=None,status='unknown',eids=[])],
                                  explain={'note':'Trip GPS locates the recording vehicle; the requested entity, object or zone was not verified.'})
                final,stop_reason = 'UNRESOLVED','abstained'
            elif (parsed.template=='where_is' and context.get('t_ms') is not None and not parsed.place
                  and store.manifest(parsed.scene_key or scoped[0]['scene_key'])['workspace']=='fleet'
                  and not store.manifest(parsed.scene_key or scoped[0]['scene_key'])['cams']):
                key, tm = parsed.scene_key or scoped[0]['scene_key'], context['t_ms']
                with trace.traced(run,'assess',{'scene_key':key,'t_ms':tm},state='measure') as t:
                    yield step_frame(t,'measure','Read the trip GPS position')
                    res = yield from call(t,'gps_position','measure',{'scene_key':key,'t_ms':tm},lambda:tools.gps_position(store,key,tm))
                    predicates = [Predicate(predicate_id='p1',subject_refs=[],operator='located',value=None,unit=None,
                                            status='supported' if res.evidence else 'unknown',eids=[e.eid for e in res.evidence])]
                yield claim_frame(predicates,explain={'note':'Position comes from trip GPS; camera observations were not checked.'},key=key)
                final = aggregate_of(predicates)
                stop_reason = 'enough_evidence' if final=='SUPPORTED' else 'no_new_info'
            elif statistic and street_scenes and 'unsupported_count_subject' in parsed.constraints:
                yield claim_frame([],explain={'note':'This path measures person tracks only; the requested object count was not measured.'})
                final,stop_reason = 'UNRESOLVED','abstained'
            elif statistic and street_scenes and (selection or parsed.zone):
                reason = 'no_geometry' if parsed.zone else 'no_track_ids'
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator=statistic,value=None,
                                             unit=None,status='unknown',eids=[])],
                                  explain={'abstain':{'reason':reason,'text':ABSTAIN_TEXT[reason]}})
                final,stop_reason = 'UNRESOLVED','abstained'
            elif statistic and street_scenes:
                statistics_complete = False
                checked = []
                for scene in street_scenes:
                    key, cam = scene['scene_key'], scene['cams'][0]['id']
                    t0,t1 = scene['t0_ms'],scene['t1_ms']
                    if parsed.t_range:
                        t0,t1 = max(t0,parsed.t_range[0]),min(t1,parsed.t_range[1])
                    if t0>=t1:
                        continue
                    with trace.traced(run,'assess',{'scene_key':key,'t0_ms':t0,'t1_ms':t1},state='measure') as t:
                        yield step_frame(t,'measure','Count tracked people across the requested window' if statistic=='tracked_count' else 'Measure the busiest second across the requested window')
                        stats = yield from call(t,'people_statistics','measure',{'scene_key':key,'cam':cam,'t0_ms':t0,'t1_ms':t1,'mode':statistic},
                                                lambda:street.people_statistics(store,key,cam,t0,t1,statistic))
                        predicates = [Predicate(predicate_id='p1',subject_refs=[],operator=statistic,value=stats.envelope.output['count'],unit='people',
                                                status='supported' if stats.envelope.coverage==1 else 'unknown',eids=[e.eid for e in stats.evidence])]
                        t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                    explain = {}
                    if spatial:
                        cap,operator,value,unit = spatial
                        predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[],operator=operator,
                                                    value=value,unit=unit,status='unknown',eids=[]))
                        reason = capabilities(scene)[cap]['reason']
                        if reason:
                            explain['abstain'] = {'reason':reason,'text':ABSTAIN_TEXT[reason]}
                    if re.search(r'\b(cross\w*|dodge\w*|yield\w*|brak\w*)\b',body.q,re.I):
                        predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[],operator='counted_action',
                                                    value=None,unit=None,status='unknown',eids=[]))
                        explain['note'] = 'Track counts do not verify how many people performed the requested action.'
                    claim_id = 'c'+str(len(claims)+1)
                    yield claim_frame(predicates,explain=explain or None,key=key)
                    checked.append(key)
                statistics_complete = True
                final = counted()
                stop_reason = 'enough_evidence' if final=='SUPPORTED' else 'no_new_info'
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
                                                  lambda:tools.blind_presence(store,key,blind_cam,t0,t1,**({'selection':selection} if selection else {}),**({'zone':parsed.zone} if parsed.zone else {})))
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
                                                    thumb=tools.thumbnail(store,key,primary,interval['t0_ms']),
                                                    backend=getattr(store,'backend','duckdb'),exec_state=res.envelope.exec_state))
                except BudgetExhausted:
                    # the scenes already checked still answer; the unchecked ones only rule out CONTRADICTED
                    exhausted = True
                if cards:
                    yield hits(cards[:5],special=True)
                for scene_index, (key, intervals, persons, predicates) in enumerate(checked):
                    claim_id='c'+str(scene_index+1)
                    if intervals:
                        yield from ui('seek',{'t_ms':intervals[0]['t0_ms'],'scene_key':key},key)
                        yield from ui('highlight',{'ids':persons,'mode':'outline'},key)
                    yield claim_frame(predicates,key=key)
                # one scene with nobody in the blind zone must not veto another scene's finding
                aggregates = [c.aggregate for c in claims]
                final = 'SUPPORTED' if 'SUPPORTED' in aggregates else 'CONTRADICTED' if aggregates and all(a=='CONTRADICTED' for a in aggregates) and not exhausted else 'UNRESOLVED'
                stop_reason = 'budget_exhausted' if exhausted else 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                if corpus:
                    yield from abstain_levels('blind_spot','warehouse')
            elif closest_q:
                with trace.traced(run,'plan',{'query':parsed.model_dump(mode='json')},state='plan') as t:
                    yield step_frame(t,'plan','Choose the closest approach path across the full interval')
                    t.output({'template':parsed.template,'path':['measure','answer']})
                predicates, explain = [], {}
                with trace.traced(run,'assess',{'scene_key':parsed.scene_key,'t_range':parsed.t_range},state='measure') as t:
                    yield step_frame(t,'measure','Scan worker and forklift tracks to measure the closest distance')
                    scan = yield from call(t,'closest_approach','measure',{'scene_key':parsed.scene_key,'t_range':parsed.t_range,'zone':parsed.zone},
                                           lambda:tools.closest_approach(store,warehouse,parsed.t_range,parsed.zone,**({'selection':selection} if selection else {})))
                    nearest = scan.envelope.output['closest']
                    closest = {'missing':scan.envelope.output['missing'],'scan':scan.evidence[0] if scan.evidence else None}
                    if nearest:
                        key, tm = nearest['scene_key'], nearest['t_ms']
                        person = EntityRef(scene_key=key,entity_id=nearest['a'],kind='person')
                        vehicle = EntityRef(scene_key=key,entity_id=nearest['b'],kind='forklift')
                        res = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':person.model_dump(),'b':vehicle.model_dump(),'kind':'distance'},
                                              lambda:tools.measure(store,key,tm,person,vehicle,'distance'))
                        ev = res.evidence[0] if res.evidence else None
                        _, operator, value, unit = spatial
                        reason = res.envelope.output.get('reason')
                        if reason in ABSTAIN_TEXT:
                            explain['abstain'] = {'reason':reason,'text':ABSTAIN_TEXT[reason]}
                        predicates = [Predicate(predicate_id='p1',subject_refs=[person,vehicle],operator=operator,
                                                value=value,unit=unit,status=distance_status(ev,operator,value),eids=[e.eid for e in res.evidence]),
                                      Predicate(predicate_id='p2',subject_refs=[person,vehicle],operator='closest_approach',value=None,unit=None,
                                                status='supported',eids=[e.eid for e in scan.evidence])]
                    t.output({'predicates':[p.model_dump(mode='json') for p in predicates],'missing':closest['missing']})
                if nearest:
                    cam = next(s['cams'] for s in warehouse if s['scene_key']==key)[0]
                    hit = MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam['id']],primary_cam=cam['id'],
                                     actor_refs=[person,vehicle],evidence_ids=[e.eid for e in [*scan.evidence,*res.evidence]],score=1,
                                     why=['closest_approach'],thumb=tools.thumbnail(store,key,cam['id'],tm),
                                     backend=getattr(store,'backend','duckdb'),exec_state=scan.envelope.exec_state)
                    yield hits([hit],special=True)
                    yield from ui('seek',{'t_ms':tm,'scene_key':key},key)
                    yield from ui('highlight',{'ids':[person.entity_id,vehicle.entity_id],'mode':'outline'},key)
                    yield claim_frame(predicates,explain=explain or None,key=key,moment=hit.moment_id)
                final = aggregate_of(predicates)
                stop_reason = 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                if corpus and nearest:
                    yield from abstain_levels('distance','warehouse')
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
                    video = [p for p in passes if any(s['scene_key']==p['trip_id'] and s['cams'] for s in fleet)]
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
                        cam = next(s['cams'] for s in fleet if s['scene_key']==key)[0]
                        hit = MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam['id']],primary_cam=cam['id'],
                                         actor_refs=[],evidence_ids=[e.eid for e in res.evidence],score=1,why=['osm_facade'],
                                         thumb=tools.thumbnail(store,key,cam['id'],tm),
                                         backend=getattr(store,'backend','duckdb'),exec_state=res.envelope.exec_state)
                        yield hits([hit],special=True)
                    if found['t_ms'] is not None:
                        yield from ui('seek',{'t_ms':found['t_ms'],'scene_key':found['scene_key']},found['scene_key'])
                    if found['building']:
                        yield from ui('highlight',{'ids':[found['building']['bid']],'mode':'outline'},found['scene_key'])
                    yield claim_frame(predicates,key=found.get('scene_key') or asked[0]['trip_id'],
                                      moment=f"m_{found['scene_key']}_{found['t_ms']}" if found['building'] else None)
                    final = aggregate_of(predicates)
                    stop_reason = 'enough_evidence' if final=='SUPPORTED' else 'no_new_info'
                    if corpus:
                        yield from abstain_levels('speed','fleet')
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
                                                lambda:tools.speeding_candidates(store,fleet,parsed.t_range,passes=near))
                            speed_coverage = res.envelope.coverage
                            reasons = {'no_gps':'GPS gap','no_speed_limit':'missing speed limit data','no_frames':'missing speed or limit samples'}
                            unchecked = [f"{key}: {a/1000}-{b/1000} s ({reasons.get(reason,reason)})"
                                         for key,coverage in res.envelope.output['scene_coverage'].items() for a,b,reason in coverage['gaps']]
                            speed_note = 'Intervals not checked: '+'; '.join(unchecked)+'.' if unchecked else None
                            moments=[]
                            for c,ev in zip(res.envelope.output['candidates'],res.evidence):
                                cams=next(s['cams'] for s in fleet if s['scene_key']==c['scene_key'])
                                moments.append(MomentCard(moment_id=f"m_{c['scene_key']}_{c['t_ms']}",scene_key=c['scene_key'],t0_ms=c['t_ms'],
                                                          t1_ms=c['t_ms']+1,cams=[x['id'] for x in cams],primary_cam=cams[0]['id'] if cams else '',actor_refs=[],
                                                          evidence_ids=[ev.eid],score=c['over_kmh'],why=['OBD speed minus map speed limit'],
                                                          thumb=tools.thumbnail(store,c['scene_key'],cams[0]['id'],c['t_ms']) if cams else None,
                                                          backend=getattr(store,'backend','duckdb'),exec_state=res.envelope.exec_state))
                            final, no_limit = 'UNRESOLVED', not moments
                        elif (parsed.template=='where_is' and context.get('t_ms') is not None and not parsed.place
                              and capabilities(store.manifest(parsed.scene_key or scoped[0]['scene_key']))['ground_position']['ok']):
                            key=parsed.scene_key or scoped[0]['scene_key']
                            tm=context['t_ms']
                            cam=store.manifest(key)['cams'][0]['id']
                            res=yield from call(t,'get_synced_views','retrieve',{'scene_key':key,'t_ms':tm,'cam':cam},
                                                lambda:tools.get_synced_views(store,key,tm,cam=cam))
                            ids=[e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)] or context.get('selection',[])
                            actors=[EntityRef(scene_key=key,entity_id=e.entity_id,kind=e.kind) for e in store.entities(key)
                                    if e.entity_id in ids and any(o['t0_ms']<=tm<o['t1_ms'] for o in e.observations)]
                            moments=[MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam],
                                                primary_cam=cam,actor_refs=actors,evidence_ids=[e.eid for e in res.evidence],
                                                score=1,why=['entity observation at requested timestamp'],
                                                thumb=res.envelope.output['cams'][0]['frame_url'] if res.envelope.output['cams'] else None,
                                                backend=getattr(store,'backend','duckdb'),exec_state=res.envelope.exec_state)] if actors else []
                            final='UNRESOLVED' if moments else 'NO_MATCH'
                        else:
                            if parsed.template=='where_is' and context.get('t_ms') is not None:
                                pass  # apply_constraints already intersected the exact requested timestamp
                            filters = {'workspace':parsed.workspace,'scene_key':parsed.scene_key,'t_range':parsed.t_range,
                                       'place':parsed.place,'zone':parsed.zone,'sources':parsed.sources,'selection':context.get('selection'),'t_ms':context.get('t_ms'),**({'scope':'corpus'} if corpus else {})}
                            res = yield from call(t,'search','retrieve',{'q':body.q,'filters':filters},
                                                  lambda:tools.search(store,body.q,filters,parsed=parsed,vss=vss,deadline=run.deadline))
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
                                    yield from ui('seek',{'t_ms':first['t0_ms'],'scene_key':first['trip_id']},first['trip_id'])
                        if not moments and not speeding_q:
                            stop_reason='abstained'
                        t.output(res.envelope.output)
                if moments:
                    yield hits(moments,special=speeding_q)
                    tops = [next(m for m in moments if level(m.scene_key)==lv) for lv in LEVELS
                            if corpus and any(level(m.scene_key)==lv for m in moments)]
                    # corpus: the top moment of each level first, then the rest
                    order = [*tops,*(m for m in moments if all(m is not top for top in tops))]
                    limited = not corpus and len(order)>policy.MAX_MOMENTS
                    if not corpus:
                        order = order[:policy.MAX_MOMENTS]
                    street_top = next((m for m in order if level(m.scene_key)=='L1'),None)
                    for card_index, card in enumerate(order):
                        # one tool stays for the top moment of every later level
                        reserve = len(tops)-1-card_index if card_index<len(tops) else 0
                        predicates, explain = [], {}
                        try:
                            check_budget()
                            claim_id='c'+str(len(claims)+1)
                            key, tm, cam = card.scene_key,card.t0_ms,card.primary_cam
                            # dispatch on the moment's own scene, never on the context's workspace
                            if level(key)=='L1':
                                yield from assess_l1(card,card is street_top)
                                continue
                            yield from ui('seek',{'t_ms':tm,'scene_key':key},key)
                            if card.actor_refs:
                                yield from ui('highlight',{'ids':[a.entity_id for a in card.actor_refs],'mode':'outline'},key)
                            manifest = store.manifest(key)
                            entities = [] if speeding_q and not manifest['cams'] else store.entities(key)
                            subjects = card.actor_refs or [EntityRef(scene_key=key,entity_id=e.entity_id,kind=e.kind) for e in entities]
                            related = store.events_in(key,tm,tm+1)
                            has_distance = bool(spatial and spatial[0]=='distance')
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
                            near_miss_center = has_distance and manifest['workspace']=='warehouse' and 'near miss' in body.q.lower()
                            explicit_distance = bool(re.search(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',body.q,re.I))
                            mismatched_clearance = near_miss_center and explicit_distance and not re.search(r'\bcent(?:er|re)\b',body.q,re.I)
                            if near_miss_center:
                                explain['note']='The near-miss rule uses footprint clearance. The separately measured distance is from the worker ground point to the forklift center.'
                                explain['not_measured']=['footprint clearance (center distance is a different quantity)']
                            if speeding:
                                operator, value, unit = 'gt', None, 'km/h'
                            elif has_distance:
                                _, operator, value, unit = spatial
                                if near_miss_center and not explicit_distance:
                                    # The event's footprint threshold cannot be applied to a center distance.
                                    operator, value = 'measured', None
                            elif spatial:
                                _, operator, value, unit = spatial
                            else:
                                operator, value, unit = 'event', 'observed', None
                            predicates = [Predicate(predicate_id='p1',subject_refs=[] if speeding else subjects,operator=operator,
                                                    value=value,unit=unit,status='unknown',eids=[])]
                            wanted = event_types(body.q)
                            event_ids = {e.event_id for e in related if e.source!='model' and wanted and e.type in wanted and
                                         {s.entity_id for s in subjects if s.kind=='person'}<=set(e.actor_ids)}
                            event_eids = [eid for eid in card.evidence_ids if read[eid].kind=='measure' and
                                          isinstance(read[eid].value,dict) and read[eid].value.get('event_id') in event_ids]
                            if not re.search(r'\b(when|dodge\w*|yield\w*|cross\w*|near miss)\b',body.q,re.I):
                                event_eids = []
                            def assess_distance(ev):
                                if mismatched_clearance:
                                    # Keep the user's comparison, but do not judge clearance with a center measurement.
                                    predicates[0].status, predicates[0].eids = 'unknown', []
                                    center = next((p for p in predicates if p.operator=='measured'),None)
                                    if center is None:
                                        center = Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=subjects,
                                                           operator='measured',value=None,unit='m',status='supported',eids=[])
                                        predicates.append(center)
                                    center.eids = [ev.eid]
                                else:
                                    predicates[0].status=distance_status(ev,operator,predicates[0].value)
                                    predicates[0].eids=[ev.eid] if near_miss_center else [ev.eid,*event_eids]
                            measure_result = None
                            with trace.traced(run,'assess',{'scene_key':key,'t_ms':tm,'cam':cam},state='inspect') as t:
                                yield step_frame(t,'inspect','Inspect the primary camera and spatial evidence')
                                if manifest['cams']:
                                    yield from call(t,'get_synced_views','inspect',{'scene_key':key,'t_ms':tm,'cam':cam},
                                                    lambda:tools.get_synced_views(store,key,tm,cam=cam))
                                # Scene scope only: corpus keeps its tools for the level reserve.
                                if (not corpus and manifest['workspace']=='fleet' and not ego_measured and not selection and not parsed.zone
                                    and any(a.kind=='person' for a in card.actor_refs)):
                                    ego_measured = True
                                    yield from call(t,'ego_distance','measure',{'scene_key':key,'cam':cam,'t0_ms':tm,'t1_ms':card.t1_ms},
                                                    lambda:tools.ego_distance(store,key,cam,tm,card.t1_ms,rows=site_rows() if vss is not None else []))
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
                                        predicates[0].status = ('supported' if speed.envelope.output['speed_kmh']>limit else
                                                                'contradicted' if speed_coverage==1 else 'unknown')
                                    predicates[0].eids = [e.eid for e in speed.evidence]
                                    if speed_note:
                                        explain['note'] = speed_note
                                elif parsed.template=='link':
                                    ids=[e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)] or context.get('selection',[])
                                    matched = [e for e in entities if e.entity_id in ids]
                                    lo, hi = parsed.t_range or ((context['t_ms'],context['t_ms']+1) if context.get('t_ms') is not None else
                                                               (manifest['t0_ms'],manifest['t1_ms']))
                                    verified = {}
                                    for moment in moments:
                                        if moment.scene_key!=key:
                                            continue
                                        for eid in moment.evidence_ids:
                                            ev = read[eid]
                                            if ev.method!='detection_timestamp':
                                                continue
                                            observations = [o for o in ev.observation_ids if lo<=o.t_ms<hi and any(
                                                e.association.get('valid') and
                                                (not isinstance(e.association['valid'],list) or len(e.association['valid'])==2 and
                                                 e.association['valid'][0]<=o.t_ms<e.association['valid'][1]) and
                                                any(p['cam_id']==o.cam_id and p['local_track_id']==o.local_track_id and
                                                    p['t0_ms']<=o.t_ms<p['t1_ms'] for p in e.observations) for e in matched)]
                                            if observations:
                                                verified[eid] = observations
                                    observed_cams = {o.cam_id for observations in verified.values() for o in observations}
                                    requested_cams = {c['id'] for c in manifest['cams'] if re.search(
                                        r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',body.q,re.I)}
                                    valid = len(set(ids))==1 and len(matched)==1 and requested_cams<=observed_cams
                                    if re.search(r'\bclips?\b',body.q,re.I):
                                        clip_ids = {r['clip_id'] for r in res.envelope.output['expanded_ranges'] if r['clip_id'].rsplit(':',2)[0]==key}
                                        clips = {clip_id for clip_id in clip_ids if any(
                                            o.cam_id==clip_id.rsplit(':',2)[1] and int(clip_id.rsplit(':',1)[1])*5000<=o.t_ms<
                                            (int(clip_id.rsplit(':',1)[1])+1)*5000 for observations in verified.values() for o in observations)}
                                        requested_clips = set(re.findall(r'[a-z0-9_-]+:[a-z0-9_-]+:\d{5}',body.q,re.I))
                                        valid &= len(clips)>=2 and requested_clips<=clips
                                    else:
                                        valid &= len(observed_cams)>=2
                                    valid &= all(c['sync_method']!='unknown' for c in manifest['cams'])
                                    predicates[0].operator = 'same_entity'
                                    predicates[0].subject_refs = [EntityRef(scene_key=key,entity_id=e.entity_id,kind=e.kind) for e in matched]
                                    predicates[0].status='supported' if valid else 'unknown'
                                    predicates[0].eids=list(verified)
                                    if not valid:
                                        explain['note']='The requested entity and observations in every requested camera or clip were not verified.'
                                elif parsed.template=='where_is' and not parsed.place and context.get('t_ms') is not None:
                                    observed=vis.envelope.output.get('person',{}).get('observed_in',[]) if vis else []
                                    predicates[0].operator='located'
                                    predicates[0].status='unknown'
                                    predicates[0].eids=[]
                                    explain['note']='Camera visibility does not establish a measured ground position.'
                                elif not has_distance:
                                    wanted = event_types(body.q)
                                    predicates[0].status='supported' if event_eids else 'unknown'
                                    predicates[0].eids=event_eids
                                if measure_result and measure_result.evidence:
                                    ev = measure_result.evidence[0]
                                    assess_distance(ev)
                                if 'action_modifier:moving' in parsed.constraints:
                                    moving_vehicle = moving_target(body.q,subjects,vehicle)
                                    if moving_vehicle:
                                        movement = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':moving_vehicle.model_dump(),'b':None,'kind':'speed'},
                                                                   lambda:tools.measure(store,key,tm,moving_vehicle,None,'speed'))
                                        move_ev = next((e for e in movement.evidence if e.kind=='measure' and e.method=='track_speed'),None)
                                        predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[moving_vehicle],
                                                                    operator='action_modifier:moving',value='moving',unit=None,
                                                                    status='supported' if move_ev and move_ev.value>(move_ev.sigma or 0) else 'unknown',
                                                                    eids=[move_ev.eid] if move_ev else []))
                                if has_distance and wanted and re.search(r'\b(when|dodge\w*|yield\w*|cross\w*|near miss)\b',body.q,re.I):
                                    predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=subjects,
                                                                operator='event',value=sorted(wanted),unit=None,
                                                                status='supported' if event_eids else 'unknown',eids=event_eids))
                                if measure_result and measure_result.envelope.output.get('reason') in ABSTAIN_TEXT:
                                    reason = measure_result.envelope.output['reason']
                                    explain['abstain'] = {'reason':reason,'text':ABSTAIN_TEXT[reason]}
                                t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                            yield claim_frame(predicates,explain=explain or None,key=key,moment=card.moment_id)
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
                                        assess_distance(ev)
                                    if measure_result.envelope.output.get('reason') in ABSTAIN_TEXT:
                                        reason = measure_result.envelope.output['reason']
                                        explain['abstain'] = {'reason':reason,'text':ABSTAIN_TEXT[reason]}
                                    t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                            if measure_result and measure_result.evidence:
                                yield from ui('highlight',{'ids':[person.entity_id,vehicle.entity_id],'mode':'outline'},key)
                            with trace.traced(run,'assess',{'predicates':[p.model_dump(mode='json') for p in predicates]},state='assess') as t:
                                yield step_frame(t,'assess','Check the situation and predicate states')
                                if subjects and not speeding and not (parsed.template=='where_is' and not parsed.place and context.get('t_ms') is not None):
                                    situation = yield from call(t,'situation','assess',{'scene_key':key,'t_ms':tm,'actor_refs':[s.model_dump() for s in subjects]},
                                                                lambda:tools.situation(store,key,tm,subjects,run=run,circuit=circuit))
                                    record = situation.envelope.output.get('record')
                                    expected='yielding' if any(e.type=='dodge' for e in related) else 'crossing' if any(e.type=='ped_crossing' for e in related) else None
                                    conflict=bool(record and (record['verification']['status']=='conflict' or
                                                              expected and record['payload']['label']!=expected))
                                    if conflict:
                                        for p in predicates:
                                            distance_eids=[eid for eid in p.eids if read[eid].kind=='measure' and read[eid].method=='ground_distance']
                                            if p.operator=='event':
                                                p.status='unknown'
                                            elif distance_eids and p.operator in {'measured','lt','le','gt','ge'}:
                                                # A conflicting model description cannot erase an independent numeric measurement.
                                                p.eids=distance_eids
                                        if not any(p.status=='unknown' for p in predicates):
                                            predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=subjects,
                                                                        operator='model_conflict',value=None,unit=None,status='unknown',eids=[]))
                                    if situation.envelope.exec_state=='failed':
                                        explain['note']='Cosmos description is unavailable; independent evidence is retained.'
                                    if record:
                                        explain['c1']=record['record_id']
                                        if conflict:
                                            explain['conflict']=True
                                before = next(c.aggregate for c in claims if c.claim_id==claim_id)
                                final = aggregate_of(predicates)
                                yield claim_frame(predicates,before,explain,key=key,moment=card.moment_id)
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
                                            yield from ui('annotate',ui_args,key)
                                stop_reason = 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                            # candidates come highest excess first, so the first decided one answers the question
                            if speeding and final in {'SUPPORTED','CONTRADICTED'}:
                                break
                        except BudgetExhausted as exc:
                            # a measurement read after the claim was sent still reaches it
                            previous = next((c for c in claims if c.claim_id==claim_id),None)
                            if predicates and (previous is None or previous.predicates!=predicates or previous.explain!=(explain or None)):
                                yield claim_frame(predicates,previous.aggregate if previous else None,explain or None,key=key,moment=card.moment_id)
                            if not isinstance(exc,LevelBudget):
                                raise
                            cut = True
                    reserve = 0
                    # an abstained claim does not count; all abstained leaves UNRESOLVED
                    final=counted(cut or limited)
                    if corpus and speeding_q:
                        yield from abstain_levels('speed','fleet')
                    if cut:
                        stop_reason='budget_exhausted'
                    else:
                        stop_reason='enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                    if claims and all(abstained(c) for c in claims) and not cut:
                        stop_reason='abstained'
                elif final not in {'UNSUPPORTED','NO_MATCH','OUT_OF_RANGE'} and not speeding_q:
                    final,stop_reason='NO_MATCH','abstained'
    except (BudgetExhausted, DeadlineExceeded):
        final=counted(True)
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
    if statistics_complete is False:
        final = 'UNRESOLVED'
    if parsed and parsed.quantifier in {'all','none'} and final not in {'UNSUPPORTED','OUT_OF_RANGE'}:
        final = 'UNRESOLVED'
        if not claims:
            yield claim_frame([],explain={'note':'No exhaustive entity/time coverage certificate is available.'})
        if stop_reason not in {'error','budget_exhausted'}:
            stop_reason = 'no_new_info'
    try:
        def compose(final, group, evidence):
            """The sentence for the claims `group`, citing only `evidence` (eid -> Evidence); numbers only through refs."""
            if final in {'SUPPORTED','CONTRADICTED'}:
                group = [c for c in group if not abstained(c) and c.aggregate==final]
            if len(group)>1:
                parts = [compose(final,[c],evidence) for c in group]
                return dict(text=' '.join(p['text'] for p in parts),claim_ids=[i for p in parts for i in p['claim_ids']],
                            cites=list(dict.fromkeys(e for p in parts for e in p['cites'])),measure_refs=[r for p in parts for r in p['measure_refs']])
            evidence = {eid:evidence[eid] for c in group for p in c.predicates for eid in p.eids if eid in evidence}
            text = TEXT[final]
            abstain = next((c.explain['abstain']['text'] for c in group if abstained(c)),None)
            if final=='UNRESOLVED' and abstain:
                text=abstain
            missing = list(dict.fromkeys(p.operator for c in group for p in c.predicates if p.status=='unknown'))
            if final=='UNRESOLVED' and missing and not abstain:
                text='Not measured: '+', '.join(missing).replace('_',' ')+'. The full question remains unresolved.'
            refs, cites, values, names = [], [], [], []
            def ref(e, field, value):
                if e.kind=='model':
                    raise ValueError('Model output cannot create measurement references')
                refs.append({'eid':e.eid,'field':field}); cites.append(e.eid); values.append(value)
                return '{{m'+str(len(values)-1)+'}}'
            def name(value):
                # identifiers such as ceiling_00 or P1 bypass the number sanitizer
                names.append(value)
                return '{{n'+str(len(names)-1)+'}}'
            # nouns follow the workspace of the scenes the sentence is about
            keys = [c.scene_key for c in group if c.scene_key] or [e.scene_key for e in covered]
            workspace = store.manifest(keys[0])['workspace'] if keys else context.get('workspace')
            limit = next((e for e in evidence.values() if e.method=='osm_maxspeed'),None)
            if final=='UNRESOLVED' and (limit and limit.value is None or no_limit):
                text='This road segment lacks map speed limit data, so speeding cannot be determined.'
            if final=='UNRESOLVED' and speed_coverage is not None and speed_coverage<1 and limit and limit.value is not None:
                speed = next((e for e in evidence.values() if e.method=='obd_speed'),None)
                if speed:
                    text=('Maximum measured speed in the covered part: OBD speed '+ref(speed,'value',speed.value)+
                          ' km/h against map speed limit '+ref(limit,'value',limit.value)+' km/h. Part of the requested interval was not checked.')
            if final=='UNRESOLVED' and building:
                text=('No map building facade '+SIDES[building['side']]+' was found in the videos passing '+name(parsed.place)+'.' if building['video']
                      else 'The trips passing '+name(parsed.place)+' have no video, so buildings on either side cannot be viewed.')
            if final=='UNRESOLVED' and closest and closest['missing']:
                text='No '+' and '.join(noun(k,'warehouse') for k in closest['missing'])+' tracks are available in the selected range, so the closest distance between the worker and forklift cannot be measured.'
            if final=='NO_MATCH' and covered:
                kind=next(iter(event_types(body.q) or ()),None)
                what='the '+noun('person',workspace)+' '+VERBS[kind].replace('{other}','the '+noun('vehicle',workspace)) if kind in VERBS else 'a matching event occurred'
                count,*intervals=covered
                text=('Checked '+ref(count,'value.count',count.value['count'])+' video(s) passing '+name(parsed.place)+' ('+'; '.join(
                    name(trip_name(e.scene_key))+', '+ref(e,'value.t0_s',e.value['t0_s'])+'–'+ref(e,'value.t1_s',e.value['t1_s'])+' s'
                    for e in intervals)+'); no evidence that '+what+'.')
            if final in {'SUPPORTED','CONTRADICTED'}:
                cites=list(dict.fromkeys(eid for c in group for p in c.predicates for eid in p.eids))
                predicates=[p for c in group for p in c.predicates]
                blind=next((p for p in predicates if p.operator=='present_in_blind_zone'),None)
                facade=next((p for p in predicates if p.operator=='facade'),None)
                speed = next((e for e in evidence.values() if e.method=='obd_speed'),None)
                if facade:
                    text='The building '+SIDES[building['side']]+' at '+name(parsed.place)+' is '+name(facade.value)+'.'
                elif blind:
                    found=sorted((evidence[e] for p in predicates if p.operator=='present_in_blind_zone' for e in p.eids),
                                 key=lambda e:(e.t0_ms,e.value['entity_id']))
                    if found:
                        text='People are present in the '+name(blind.value)+' blind spot: '+'; '.join(
                            'the '+noun('person',workspace)+' '+name(e.value['entity_id'])+' at '+ref(e,'value.t0_s',e.value['t0_s'])+'–'
                            +ref(e,'value.t1_s',e.value['t1_s'])+' s ('+('visible to '+name(', '.join(e.value['seen_by'])) if e.value['seen_by'] else 'not visible to any other camera')+')'
                            for e in found)+'.'
                    else:
                        text='Checked frame by frame: nobody is present in the '+name(blind.value)+' blind spot.'
                elif closest:
                    ev = next(e for e in evidence.values() if e.method=='ground_distance')
                    scan = next(e for e in evidence.values() if e.method=='closest_approach')
                    text=('The minimum observed distance from the worker ground point to the forklift center was '+ref(ev,'value',ev.value)+' m (±'+ref(ev,'sigma',ev.sigma)+' m), at '
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
                    event=next((e for e in evidence.values() if e.kind=='measure' and e.method.startswith('event_') and isinstance(e.value,dict)
                                and 't_s' in e.value and (wanted and e.value.get('type') in wanted or not wanted and when)),None)
                    ev = next((e for e in evidence.values() if e.kind=='measure' and e.method=='ground_distance'),None)
                    clause=''
                    if event:
                        verb=VERBS.get(event.value.get('type'))
                        at=ref(event,'value.t_s',event.value['t_s'])
                        clause='The '+who+' '+verb.replace('{other}','the '+whom)+' at '+at+' s' if verb else 'The event started at '+at+' s in the scene'
                    if ev:
                        clause+=(', ' if clause else 'The '+who+' was ')+ref(ev,'value',ev.value)+' m (±'+ref(ev,'sigma',ev.sigma)+' m) from the '+whom
                    if clause:
                        text=clause+'.' if final=='SUPPORTED' else text+' '+clause+'.'
            if final=='UNRESOLVED' and any((c.explain or {}).get('conflict') for c in group):
                numeric = next((evidence[eid] for c in group for p in c.predicates if p.status=='supported'
                                for eid in p.eids if eid in evidence and evidence[eid].kind=='measure'
                                and evidence[eid].method=='ground_distance'),None)
                if numeric:
                    quantity=('distance from the worker ground point to the forklift center'
                              if store.manifest(numeric.scene_key)['workspace']=='warehouse' else 'ground distance between the requested entities')
                    text+=' Independently measured '+quantity+': '+ref(numeric,'value',numeric.value)+' m (±'+ref(numeric,'sigma',numeric.sigma)+' m).'
            if workspace=='warehouse' and 'near miss' in body.q.lower() and group:
                center = next((e for e in evidence.values() if e.kind=='measure' and e.method=='ground_distance'),None)
                independent = next((e for e in evidence.values() if e.kind=='measure' and e.method.startswith('event_')
                                    and isinstance(e.value,dict) and e.value.get('type')=='near_miss'),None)
                refs, cites, values = [], [], []
                if final=='SUPPORTED' and independent:
                    text='An independently recorded near-miss event started at '+ref(independent,'value.t_s',independent.value['t_s'])+' s.'
                elif final=='UNRESOLVED' and any((c.explain or {}).get('conflict') for c in group):
                    text='The near-miss event remains unresolved because the model description conflicts with the event evidence.'
                elif final=='UNRESOLVED':
                    text=('The requested near-miss distance comparison is unresolved: center distance does not measure footprint clearance.'
                          if re.search(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',body.q,re.I)
                          else 'The near-miss question remains unresolved; a center distance alone does not establish an independent near-miss event.')
                else:
                    text=TEXT[final]
                if independent and independent.eid not in cites:
                    cites.append(independent.eid)
                if center:
                    text+=' Separately measured distance from the worker ground point to the forklift center: '+ref(center,'value',center.value)+' m (±'+ref(center,'sigma',center.sigma)+' m).'
                text+=' Center distance is not footprint clearance; footprint clearance was not measured in this run.'
            if workspace=='warehouse' and final=='UNRESOLVED' and re.search(r'\b(footprint|clearance|edge)\b',body.q,re.I):
                text='The requested footprint clearance was not measured. Forklift-center distance is a different quantity and cannot verify this condition.'
                refs, cites, values = [], [], []
            cites=list(dict.fromkeys(cites))
            parts = re.split(r'(\{\{[mn]\d+\}\})',text)
            text=''.join(p if re.fullmatch(r'\{\{[mn]\d+\}\}',p) else llm.sanitize_numbers(p,[]) for p in parts)
            for i,value in enumerate(values):
                text=text.replace('{{m'+str(i)+'}}',format(value,'.4g'))
            for i,value in enumerate(names):
                text=text.replace('{{n'+str(i)+'}}',value)
            return dict(text=text,claim_ids=[c.claim_id for c in group],cites=cites,measure_refs=refs)

        def statistics_sentence(cards):
            evidence = {eid:read[eid] for c in cards for p in c.predicates for eid in p.eids if eid in read}
            parts, refs = [], []
            for e in evidence.values():
                if e.method not in {'bytetrack_people_count','yolo_people_peak'}:
                    continue
                v = e.value
                def number(field):
                    refs.append({'eid':e.eid,'field':'value.'+field})
                    return format(v[field],'.4g')
                prefix = 'Measured' if v['coverage']==1 else 'In the covered portion, measured'
                if e.method=='bytetrack_people_count':
                    text = 'Camera '+e.cam_id+' observed '+number('count')+' person tracks in '+number('t0_s')+'–'+number('t1_s')+' s. Line crossings and unique identities were not measured.'
                else:
                    text = prefix+' peak of '+number('count')+' simultaneous person detections'
                    if v['t_s'] is not None:
                        text += ' at '+number('t_s')+' s'
                    text += ' across '+number('t0_s')+'–'+number('t1_s')+' s.'
                if v['coverage']<1:
                    text += ' Missing frames leave the full window unresolved.'
                if any(c.aggregate!='SUPPORTED' for c in cards) and v['coverage']==1:
                    text += ' Other conditions were not measured; the full question remains unresolved.'
                parts.append(text)
            return dict(text=' '.join(parts),claim_ids=[c.claim_id for c in cards],cites=list(evidence),measure_refs=refs)

        def street_sentence(cards):
            """L1: what the 2D detections show; a spatial question keeps its abstain text and cites no measurement."""
            if any(p.operator in {'tracked_count','peak_count'} and p.eids for c in cards for p in c.predicates):
                return statistics_sentence(cards)
            detected = [c.predicates[0] for c in cards]
            found = [p for p in detected if p.status=='supported']
            what = ' and '.join(('an ' if k[0] in 'aeiou' else 'a ')+k for k in detected[0].value)
            if not what:
                text = 'Street camera clips were retrieved by their captions; captions and Cosmos are model output and confirm nothing.'
            elif found:
                text = 'A street camera shows '+what+' in the same frame (2D).'
            else:
                text = 'The street camera frames read do not show '+what+' together; a missed 2D detection does not rule this out.'
            abstain = next((c.explain['abstain']['text'] for c in cards if abstained(c)),None)
            missing = list(dict.fromkeys(p.operator for c in cards for p in c.predicates if p.status=='unknown' and p.operator!='detected'))
            if missing:
                text += ' Not measured: '+', '.join(missing).replace('_',' ')+'. The full question remains unresolved.'
            return dict(text=text+(' '+abstain if abstain else ''),claim_ids=[c.claim_id for c in cards],
                        cites=list(dict.fromkeys(e for p in found for e in p.eids)),measure_refs=[])

        def level_sentence(lv, group):
            cards = [c for c in group if c.scene_key]
            parts = []
            if cards and lv=='L1':
                parts.append(street_sentence(cards))
            elif cards:
                parts.append(compose(counted(stop_reason=='budget_exhausted',cards),cards,
                                     {k:e for k,e in read.items() if e.evidence_level==lv}))
            # a level the special path could not check
            parts += [dict(text=c.text+'. '+c.explain['abstain']['text'],claim_ids=[c.claim_id],cites=[],measure_refs=[])
                      for c in group if not c.scene_key]
            return dict(text=' '.join(p['text'] for p in parts),claim_ids=[i for p in parts for i in p['claim_ids']],
                        cites=list(dict.fromkeys(e for p in parts for e in p['cites'])),measure_refs=[r for p in parts for r in p['measure_refs']],
                        evidence_level=lv)

        with trace.traced(run,'answer',{'final':final,'read_eids':sorted(run.read_eids)},state='answer') as t:
            yield step_frame(t,'answer','Generate an answer from the evidence read')
            if corpus and claims:
                # an overall sentence, then one per level group
                answers = [dict(text=TEXT[final],claim_ids=[c.claim_id for c in claims],cites=[],measure_refs=[],evidence_level=None),
                           *(level_sentence(lv,[c for c in claims if c.evidence_level==lv]) for lv in LEVELS
                             if any(c.evidence_level==lv for c in claims))]
            elif corpus:
                answers = [{**compose(final,claims,read),'evidence_level':None}]
            elif claims and all(c.evidence_level=='L1' for c in claims):
                answers = [street_sentence(claims)]
            else:
                answers = [compose(final,claims,read)]
            answers = [{'sentence_id':'a'+str(i+1),**a} for i, a in enumerate(answers)]
            t.output(answers[0] if len(answers)==1 else answers)
            for answer in answers:
                yield frame('answer',**answer)
        weave_url=trace.finish_run(run,'error' if stop_reason=='error' else 'done',final=final,claims=claims)
        finished=True
        yield frame('status',final=final,cached=False,source_run_id=None,weave_url=weave_url,stop_reason=stop_reason)
        if native is not None:
            # the run is already finalized; this step is still appended to its persisted steps
            with trace.traced(run,'vss.search',{'q':body.q},state='retrieve') as t:
                data = native(max(0,min(body.budget.seconds-(time.monotonic()-start),NATIVE_WAIT_S)))
                t.exec_state('live')
                t.output(data)
            yield frame('vss_native',**data)
        yield frame('done')
    finally:
        if not finished:
            trace.finish_run(run,'aborted',final=None,claims=claims)


@router.post('/api/agent/ask')
def ask(request: Request, body: AskRequest):
    context = body.context.model_dump(exclude_unset=True)
    key = context.get('scene_key') or (context['scene_id'].replace('/','-').lower() if context.get('scene_id') else None)
    if key:
        try:
            manifest = request.app.state.store.manifest(key)
        except FileNotFoundError:
            raise HTTPException(422,'context scene does not exist') from None
        if manifest['workspace']!=context['workspace']:
            raise HTTPException(422,'context scene does not belong to workspace')
    settings=getattr(request.app.state,'settings',None)
    if getattr(settings,'mode','dev')=='site' and getattr(request.app.state,'vss',None) is None:
        raise HTTPException(503,'site VSS is not configured')
    store=request.app.state.store
    def sse(frame):
        return 'event: '+frame['event']+'\ndata: '+json.dumps(frame['data'],ensure_ascii=False)+'\n\n'
    def stream():
        hit=cache.replay(store,body.q,context)
        if hit:
            yield from map(sse,hit[1])
            return
        frames=[]
        for frame in ask_frames(store,body,vss=getattr(request.app.state,'vss',None)):
            frames.append(frame)
            yield sse(frame)
        status=next((f['data'] for f in frames if f['event']=='status'),None)
        results=[f['data'] for f in frames if f['event']=='tool_result']
        unavailable={'vlm_unavailable','sidecar_unavailable','no_detections','detections_mismatch'}
        complete=(all(r['ok'] and r['envelope']['exec_state']!='failed' and r['envelope']['quality']!='unavailable'
                      and not any(g[2] in unavailable for g in r['envelope']['gaps'])
                      and not any(e['method']=='yolo_coco_2d' and e['value'].get('source') is None for e in r['evidence'])
                      for r in results)
                  and not any(s.name.startswith('tool.') and s.exec_state=='failed'
                              for s in store.get_run(frames[0]['data']['run_id']).steps))
        if status and status['stop_reason'] in {'enough_evidence','no_new_info','abstained'} and complete:
            # every frame is already sent; a failed cache write must not break the stream
            try:
                cache.put(store,body.q,context,frames,getattr(store,'backend','duckdb'),frames[0]['data']['run_id'])
            except Exception:
                logger.exception('answer cache write for run %s failed',frames[0]['data']['run_id'])
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
