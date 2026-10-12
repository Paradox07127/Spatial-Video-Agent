"""Deterministic investigation, streamed as contract SSE frames."""
from __future__ import annotations

import base64
import concurrent.futures
import functools
import html
import json
import logging
import math
import re
import threading
import time
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.api.search import event_types
from app.core import cache, followup, geo, llm, policy, street, tools, trace
from app.core.investigation_context import apply_constraints
from app.core.task_spec import UNIVERSAL_TIME, build_task_spec, consume_comparison_time
from app.core.tools import farthest_sample
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
UNMAPPED = 'This question could not be mapped to a supported investigation template.'
SCENES = {'warehouse':'Warehouse scenes','fleet':'Fleet trips','street':'Street cameras'}
COCO = ('person','bicycle','car','motorcycle','airplane','bus','train','truck','boat','traffic light','fire hydrant','stop sign',
        'parking meter','bench','bird','cat','dog','horse','sheep','cow','elephant','bear','zebra','giraffe','backpack','umbrella',
        'handbag','tie','suitcase','frisbee','skis','snowboard','sports ball','kite','baseball bat','baseball glove','skateboard',
        'surfboard','tennis racket','bottle','wine glass','cup','fork','knife','spoon','bowl','banana','apple','sandwich','orange',
        'broccoli','carrot','hot dog','pizza','donut','cake','chair','couch','potted plant','bed','dining table','toilet','tv',
        'laptop','mouse','remote','keyboard','cell phone','microwave','oven','toaster','sink','refrigerator','book','clock','vase',
        'scissors','teddy bear','hair drier','toothbrush')
VEHICLES = ['bicycle','bus','car','motorcycle','truck']
NATIVE_WORKERS = 4
NATIVE_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=NATIVE_WORKERS,thread_name_prefix='vss-native')
NATIVE_SLOTS = threading.BoundedSemaphore(NATIVE_WORKERS)


def native_search(vss, q, *, deadline=None):
    """Start VSS /search in the background; the returned function waits at most its argument for the vss_native data.
    None when every comparison worker is busy: the comparison is skipped."""
    if not NATIVE_SLOTS.acquire(blocking=False):
        return None
    started = time.monotonic()
    box = []
    def search():
        try:
            out = vss.search(q,10,{},timeout=remaining(30,deadline=deadline))
            data = {'status':'ok','sql_query':out['sql_query'],'hits':out['hits'][:10]}
        except Exception:
            logger.exception('VSS /search failed')
            data = {'status':'error','sql_query':None,'hits':[]}
        finally:
            NATIVE_SLOTS.release()
        box.append({'elapsed_ms':round((time.monotonic()-started)*1000),**data})
    worker = NATIVE_POOL.submit(search)
    def wait(seconds):
        try:
            worker.result(seconds)
        except concurrent.futures.TimeoutError:
            pass
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


def comparison_of(text):
    text = text.lower()
    return ('ge' if re.search(r'\bat least\b',text) else 'le' if re.search(r'\bat most\b',text) else
            'gt' if re.search(r'\b(greater than|more than|over|farther than)\b',text) else 'lt')


def distance_comparisons(q):
    """(operator, value) of each distance threshold; with several, each reads the words of its own clause."""
    matches = list(re.finditer(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',q,re.I))
    if len(matches)==1:
        return [(comparison_of(q),float(matches[0][1]))]
    return [(comparison_of(q[before.end() if before else 0:m.start()]),float(m[1])) for before, m in zip([None,*matches],matches)]


def spatial_of(q, template):
    """(capability, operator, value, unit) of the spatial predicate the question needs, or None."""
    text = q.lower()
    threshold = re.search(r'(\d+(?:\.\d+)?)\s*(?:m\b|meters?\b)',q,re.I)
    if threshold or any(w in text for w in DISTANCE_WORDS) or re.search(r'\b(closest|nearest|minimum|beside|adjacent)\b|\bnear (?:a|an|the) (?:person|worker|pedestrian|forklift|car|vehicle)\b',text):
        if threshold:
            operator, value = distance_comparisons(q)[0]
            return 'distance',operator,value,'m'
        return ('distance','lt',2.,'m') if 'near miss' in text else ('distance','measured',None,'m')
    if re.search(r"can'?t see|cannot see|blind",q,re.I):
        return 'blind_spot','present_in_blind_zone',None,None
    # the contract table gives speeding and building questions the no_gps reason
    if 'speed' in text or 'how fast' in text:
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


def finite_sigma(ev):
    return isinstance(ev.sigma,(float,int)) and math.isfinite(ev.sigma) and ev.sigma>=0


def abstained(claim):
    return bool((claim.explain or {}).get('abstain'))


def quantity_name(predicate):
    if predicate.operator=='measured' and predicate.unit=='m':
        return 'distance'
    return {'located':'position','measured_speed':'speed','present_in_blind_zone':'sampled blind-zone presence','visual_zone':'selected zone','place_constraint':'requested place'}.get(
        predicate.operator,predicate.operator.replace('_',' '))


def distance_status(ev, operator, value):
    if ev is None:
        return 'unknown'
    if operator=='measured':
        return 'supported'
    observed=ev.value['distance_m'] if ev.method=='bound_ego_distance' else ev.value
    low, high = observed-ev.sigma, observed+ev.sigma
    supported, contradicted = {'lt':(high<value,low>=value),'le':(high<=value,low>value),
                               'gt':(low>value,high<=value),'ge':(low>=value,high<value)}[operator]
    return 'supported' if supported else 'contradicted' if contradicted else 'unknown'


def render_cited_text(template, refs, values, names, formats):
    """Resolve source placeholders once and record their exact Unicode codepoint spans."""
    parts, spans, cursor, length = [], [], 0, 0
    for match in re.finditer(r'\{\{([mn])(\d+)\}\}',template):
        prefix=template[cursor:match.start()]
        parts.append(prefix); length+=len(prefix)
        i=int(match[2])
        value=format(values[i],formats[i]) if match[1]=='m' else names[i]
        if match[1]=='m':
            spans.append({'start':length,'end':length+len(value),**refs[i]})
        parts.append(value); length+=len(value); cursor=match.end()
    parts.append(template[cursor:])
    return ''.join(parts),spans


def join_answer_parts(parts, **extra):
    text,spans,offset=[],[],0
    for part in parts:
        if text: offset+=1
        spans.extend({**span,'start':span['start']+offset,'end':span['end']+offset}
                     for span in part.get('text_spans',[]))
        text.append(part['text']); offset+=len(part['text'])
    return dict(text=' '.join(text),claim_ids=[i for p in parts for i in p['claim_ids']],
                cites=list(dict.fromkeys(e for p in parts for e in p['cites'])),
                measure_refs=[r for p in parts for r in p['measure_refs']],text_spans=spans,
                model_refs=[r for p in parts for r in p.get('model_refs',[])],**extra)


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
    cam_id: Annotated[str,Field(strict=True,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')] | None = None
    t_ms: Annotated[int, Field(strict=True,ge=0)] | None = None
    t_range: tuple[Annotated[int, Field(strict=True,ge=0)], Annotated[int, Field(strict=True,ge=0)]] | None = None
    selection: list[Annotated[str, Field(strict=True)]] | None = None
    zone: list[tuple[Annotated[float, Field(strict=True,allow_inf_nan=False)], Annotated[float, Field(strict=True,allow_inf_nan=False)]]] | None = Field(default=None,min_length=3)
    scope: Scope = 'scene'
    sources: list[Tag] | None = None

    @model_validator(mode='after')
    def ordered_range(self):
        if self.workspace is None and self.scope=='corpus':
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
    protocol_version: Literal[1,2] = 1
    session_id: str | None = Field(default=None,pattern=r'^s_[0-9a-f]{24}$')
    parent_turn_id: str | None = Field(default=None,pattern=r'^t_[0-9a-f]{24}$')
    expected_revision: Annotated[int,Field(strict=True,ge=0)] | None = None
    request_id: str | None = Field(default=None,min_length=1,max_length=128,pattern=r'^[A-Za-z0-9_.:-]+$')

    @model_validator(mode='after')
    def protocol_session_fields(self):
        session_fields=(self.session_id,self.parent_turn_id,self.expected_revision,self.request_id)
        if self.protocol_version==1 and any(v is not None for v in session_fields):
            raise ValueError('Session fields require protocol_version=2')
        if not self.session_id and any(v is not None for v in (self.parent_turn_id,self.expected_revision,self.request_id)):
            raise ValueError('parent_turn_id, expected_revision and request_id require session_id')
        if self.session_id and (self.request_id is None or self.expected_revision is None):
            raise ValueError('session_id requires request_id and expected_revision')
        if self.context.workspace is None and not (self.protocol_version==2 and self.session_id):
            raise ValueError('context.workspace is required unless scope is corpus or a persistent session supplies it')
        return self


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
    native = native_search(vss,body.q,deadline=run.deadline) if vss is not None else None
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
    hard_cam = context.get('cam_id')
    statistics_complete = None
    direct_pair_distance = False
    distance_extreme = None
    ego_measured = False
    observations = []
    ego_measurement = None
    place_verified = False
    late = False

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

    def call(parent, name, state, args, fn, *, budgeted=True):
        nonlocal count, late
        if budgeted:
            check_budget()
            count += 1
        elif time.monotonic()-start>=body.budget.seconds:
            raise BudgetExhausted
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
            if isinstance(res.envelope.output,dict) and res.envelope.output.get('observation'):
                observation = res.envelope.output['observation']
                observations.append(observation)
                yield frame('observation',observation=observation)
            if isinstance(res.envelope.output,dict) and 'record' in res.envelope.output:
                record = res.envelope.output['record']
                yield frame('model_result',scene_key=res.envelope.input_range.scene_key,t_ms=record['t_ms'],results=[record])
        # a tool that overran the deadline keeps its evidence for this step; the run then stops as budget-exhausted
        late = late or time.monotonic()>=run.deadline
        return res

    def claim_frame(predicates, before=None, explain=None, *, key=None, moment=None, lv=None, text='Spatial and event evidence check'):
        # A condition retained by parsing must either be measured or remain unknown.
        if parsed and parsed.place and not place_verified and not any(p.operator=='place_constraint' for p in predicates):
            predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[],
                                        operator='place_constraint',value=parsed.place,unit=None,status='unknown',eids=[]))
        if spatial and spatial[0]=='distance' and spatial[2] is not None:
            # A further threshold holds only where the primary comparison's own distance sample satisfies it too.
            primary = next((p for p in predicates if (p.operator,p.value)==spatial[1:3]),None)
            sample = next((read[e] for e in (primary.eids if primary and primary.status!='unknown' else [])
                           if read[e].kind=='measure' and read[e].method in {'ground_distance','bound_ego_distance'}),None)
            for operator, value in distance_comparisons(body.q)[1:]:
                bound = next((p for p in predicates if p is not primary and (p.operator,p.value)==(operator,value)),None)
                if bound is None:
                    bound = Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=primary.subject_refs if primary else [],
                                      operator=operator,value=value,unit='m',status='unknown',eids=[])
                    predicates.append(bound)
                supported = distance_status(sample,operator,value)=='supported'
                bound.status, bound.eids = ('supported',[sample.eid]) if supported else ('unknown',[])
        distance_status_of={p.status for p in predicates if distance_extreme and p.operator in {'lt','le','gt','ge'} and p.unit=='m' and p.eids}
        speed_supported=any(p.operator in {'lt','le','gt','ge'} and p.unit in {'m/s','km/h'} and p.status=='supported'
                            and any(e in read and read[e].method in {'obd_speed','track_speed'} for e in p.eids) for p in predicates)
        conditions = consume_comparison_time(body.q,parsed.constraints,existential='supported' in distance_status_of or speed_supported,
                                             universal='contradicted' in distance_status_of) if parsed else []
        for condition in conditions:
            if condition.startswith('requested_action:'):
                action = condition.split(':',1)[1]
                kinds = event_types(action)
                verified = [eid for p in predicates if p.status=='supported' for eid in p.eids
                            if eid in read and read[eid].kind=='measure' and isinstance(read[eid].value,dict)
                            and read[eid].value.get('type') in kinds]
                if verified:
                    continue  # The existing event predicate already consumes this requirement.
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
        # a same-time candidate card that consumed no evidence cannot undo a verdict measured at that time
        at = {m.moment_id:(m.scene_key,m.t0_ms) for m in moments}
        decided = {at[c.moment_id] for c in kept if c.moment_id in at and c.aggregate in {'SUPPORTED','CONTRADICTED'}}
        kept = [c for c in kept if not (c.aggregate=='UNRESOLVED' and c.moment_id in at and at[c.moment_id] in decided
                                        and not any(p.eids for p in c.predicates))]
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
            named_sources=set(re.findall(r'(?<![A-Za-z0-9_])(?:wh|fl|st)-[a-z0-9_]+(?:-[a-z0-9_]+)*(?![A-Za-z0-9_])',body.q.lower()))
            source_reason=None
            if named_sources:
                if len(named_sources)!=1 or parsed.scene_key and named_sources!={parsed.scene_key}:
                    source_reason='conflicting_source_constraints'
                else:
                    named_key=next(iter(named_sources))
                    try:
                        named_manifest=store.manifest(named_key)
                    except FileNotFoundError:
                        source_reason='unknown_source_constraint'
                    else:
                        if named_manifest['workspace']!=parsed.workspace:
                            source_reason='conflicting_workspace_constraints'
                        elif parsed.sources and tag_of(named_manifest) not in parsed.sources:
                            source_reason='conflicting_source_constraints'
                        else:
                            parsed=parsed.model_copy(update={'scene_key':named_key,'scope':'scene'})
            parsed, constraint_reason = apply_constraints(body.q,context,parsed)
            constraint_reason=constraint_reason or source_reason
            if body.protocol_version==2 and parsed.template=='what_happened' and parsed.zone:
                parsed=parsed.model_copy(update={'constraints':list(dict.fromkeys([*parsed.constraints,'visual_zone']))})
            if parsed.scene_key:
                source=store.manifest(parsed.scene_key)
                source_cams={c['id'] for c in source['cams']}
                named_cams={c for c in source_cams if re.search(r'(?<![A-Za-z0-9_])'+re.escape(c)+r'(?![A-Za-z0-9_])',body.q,re.I)}
                opaque_refs={token.split(':',1)[0].lower() for token in re.findall(r'\b[a-z]+_\d+(?::\d+)?\b',body.q,re.I)}
                if opaque_refs-{c.lower() for c in source_cams}:
                    constraint_reason=constraint_reason or 'unknown_reference_constraint'
                if hard_cam and (hard_cam not in source_cams or named_cams and named_cams!={hard_cam}):
                    constraint_reason='conflicting_camera_constraints'
                elif not hard_cam and len(named_cams)==1:
                    hard_cam=next(iter(named_cams))
            elif hard_cam and not any(c['id']==hard_cam for s in store.list_scenes(None if parsed.scope=='corpus' else parsed.workspace)
                                      if not parsed.sources or tag_of(s) in parsed.sources for c in s['cams']):
                constraint_reason=constraint_reason or 'conflicting_camera_constraints'
            quantifier = parsed.quantifier
            if parsed.workspace=='fleet' and not parsed.place:
                parsed = parsed.model_copy(update={'place':geo.named_place(store,body.q)})
            parsed_output={**parsed.model_dump(mode='json'),'task_spec':build_task_spec(body.q).as_dict()}
            valid_time=(parsed.t_range is None or 0<=parsed.t_range[0]<parsed.t_range[1])
            if constraint_reason is None and valid_time:
                explicit_ids=[e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)]
                effective={**context,**{k:parsed_output[k] for k in
                    ('workspace','scene_key','scope','sources','place','t_range','zone')},
                    'selection':explicit_ids or context.get('selection'),'cam_id':hard_cam}
                if parsed.t_range and parsed.t_range[1]-parsed.t_range[0]==1:
                    effective['t_ms']=parsed.t_range[0]
                parsed_output['effective_context']=effective
            t.output(parsed_output)
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
            # measurements are world-frame: only a camera the question names constrains them, not the active view in context
            named_cam = hard_cam if hard_cam and re.search(r'(?<![A-Za-z0-9_])'+re.escape(hard_cam)+r'(?![A-Za-z0-9_])',body.q,re.I) else None
            blind_cam = None
            if warehouse and re.search(r"can'?t see|cannot see|blind",body.q,re.I):
                blind_cam = next((c['id'] for s in warehouse for c in s['cams']
                                  if re.search(r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',body.q,re.I)),hard_cam)
            blind_scenes = [s for s in warehouse if 'visibility' in s['assets'] and any(c['id']==blind_cam for c in s['cams'])]
            speeding_q = bool(fleet) and any(w in body.q.lower() for w in ['speeding'])
            building_q = bool(fleet) and parsed.place and re.search(r'\bbuilding\b',body.q,re.I)
            requested_kinds = {('person' if re.fullmatch(r'P\d+',e) else 'forklift' if re.fullmatch(r'F\d+',e) else 'vehicle' if re.fullmatch(r'V\d+',e) else e) for e in parsed.entities}
            # blind_presence tests person ground points only
            blind_other = bool(blind_scenes) and bool(requested_kinds-{'person'})
            selection = [e for e in parsed.entities if re.fullmatch(r'[PFV]\d+',e)]
            point_match=re.search(r'\bat\s+(?:(\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b)|second\s+(\d+(?:\.\d+)?))',body.q,re.I)
            requested_time=context.get('t_ms')
            if requested_time is None and point_match:
                requested_time=int(float(point_match[1] or point_match[2])*1000)
            explicit_pair=(len(selection)==2 and any(e.startswith('P') for e in selection) and any(e.startswith('F') for e in selection))
            direct_pair_distance=bool(warehouse and explicit_pair and spatial and spatial[0]=='distance'
                                      and not re.search(r'\b(when|dodge\w*|yield\w*|cross\w*|brak\w*|near miss)\b',body.q,re.I))
            scalar_speed_q=bool(warehouse and re.search(r'\b(how fast|speed)\b',body.q,re.I) and not speeding_q
                                and not re.search(r'\b(dodge\w*|yield\w*|cross\w*|brak\w*)\b',body.q,re.I))
            ego_named=bool(re.search(r'\b(ego|recording (?:car|vehicle)|camera car|this car)\b',body.q,re.I))
            bound_ego_q=bool(fleet and ego_named and spatial and spatial[0]=='distance')
            ego_scalar_q=bool(fleet and ego_named and not speeding_q and re.search(r'\b(how fast|speed)\b',body.q,re.I)
                              and not re.search(r'\b(dodg\w*|cross\w*|brak\w*)\b',body.q,re.I))
            world_position_q=bool(warehouse and parsed.template=='where_is' and not parsed.place and spatial and spatial[0]=='ground_position')
            minimum_q=bool(re.search(r'\b(closest|nearest|minimum)\b',body.q,re.I))
            always_q=bool(re.search(UNIVERSAL_TIME,body.q,re.I))
            maximum_q=bool(spatial and spatial[0]=='distance' and spatial[1] in {'lt','le','gt','ge'}
                           and (spatial[1] in {'gt','ge'})!=always_q and not minimum_q)
            closest_q = direct_pair_distance or (bool(warehouse) and {'person','forklift'}<=requested_kinds
                         and (re.search(r'\b(closest|nearest|minimum)\b',body.q,re.I)
                              or always_q and spatial and spatial[0]=='distance'
                              or re.search(r'\bhow close\b.*\b(come|came|comes|coming|get|got|gets|getting)\b',body.q,re.I))
                         and (always_q or re.search(r'\b(distance|gap|how far|how many meters|how close|closest|nearest)\b',body.q,re.I)))
            # Existential greater / universal less comparisons need the largest sampled distance.
            farthest_q=bool(warehouse and maximum_q and quantifier=='any'
                            and {'person','forklift'}<=requested_kinds and not minimum_q
                            and not re.search(r'\b(when|dodge\w*|yield\w*|cross\w*|brak\w*|near miss|footprint|clearance|edge)\b',body.q,re.I))
            if not scoped or (parsed.t_range and (parsed.t_range[0]>=parsed.t_range[1] or
                    not any(store.manifest(s['scene_key'])['t0_ms']<parsed.t_range[1] and
                            store.manifest(s['scene_key'])['t1_ms']>parsed.t_range[0] for s in scoped))):
                final, stop_reason = 'OUT_OF_RANGE','abstained'
            elif context.get('t_ms') is not None and not any(
                    store.manifest(s['scene_key'])['t0_ms']<=context['t_ms']<store.manifest(s['scene_key'])['t1_ms'] for s in scoped):
                final, stop_reason = 'OUT_OF_RANGE','abstained'
            elif body.protocol_version==2 and parsed.template=='what_happened':
                if len(scoped)!=1:
                    yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='visual_observation',
                                                 value=None,unit=None,status='unknown',eids=[])],
                                      explain={'note':'Select one source scene for a bounded visual observation.'})
                else:
                    from app.core.model_observation import observe
                    manifest=store.manifest(scoped[0]['scene_key'])
                    key=manifest['scene_key']
                    requested_cams=[c for c in manifest['cams'] if re.search(
                        r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',body.q,re.I)]
                    cameras=([c for c in manifest['cams'] if c['id']==hard_cam] if hard_cam else requested_cams or manifest['cams'][:1])
                    # A single source may have been resolved only here. Validate
                    # explicit camera references before choosing its default view.
                    camera_refs={token.split(':',1)[0].lower() for token in re.findall(r'\b[a-z]+_\d+(?::\d+)?\b',body.q,re.I)}
                    if camera_refs-{c['id'].lower() for c in manifest['cams']}:
                        cameras=[]
                    lo,hi=parsed.t_range or (manifest['t0_ms'],min(manifest['t1_ms'],manifest['t0_ms']+6000))
                    if hi-lo==1:
                        hi=lo+math.ceil(1000/manifest['fps'])  # an instant off a frame boundary still needs the next decoded frame
                    lo,hi=max(lo,manifest['t0_ms']),min(hi,manifest['t1_ms'])
                    known={e.entity_id:e for e in store.entities(key)} if manifest['workspace']!='street' else {}
                    subjects=[EntityRef(scene_key=key,entity_id=e,kind=known[e].kind) for e in selection if e in known]
                    if len(subjects)!=len(selection) or not cameras:
                        yield claim_frame([Predicate(predicate_id='p1',subject_refs=subjects,operator='visual_observation',
                                                     value=None,unit=None,status='unknown',eids=[])],key=key,
                                          explain={'note':'The selected source has no readable camera or requested entity binding.'})
                    else:
                        observation_eids=[]
                        for camera in cameras:
                            cam=camera['id']
                            with trace.traced(run,'assess',{'scene_key':key,'cam':cam,'t_range':[lo,hi]},state='inspect') as t:
                                yield step_frame(t,'inspect','Inspect sampled video frames for a sourced visual observation')
                                res=yield from call(t,'observe_window','inspect',{'scene_key':key,'cam':cam,'t_range':[lo,hi],
                                    'actor_refs':[s.model_dump() for s in subjects]},
                                    lambda:observe(store,key,cam,(lo,hi),subjects,run=run))
                                observation_eids.extend(e.eid for e in res.evidence)
                                record=res.envelope.output.get('observation')
                                if record and record.get('frame_refs'):
                                    tm=record['frame_refs'][0]['t_ms']
                                    card=MomentCard(moment_id=f'{key}:{cam}:{tm}:observation',scene_key=key,
                                        t0_ms=record['t0_ms'],t1_ms=record['t1_ms'],cams=[cam],primary_cam=cam,
                                        actor_refs=subjects,evidence_ids=[e.eid for e in res.evidence],score=1,
                                        why=['model_observation'],thumb=tools.thumbnail(store,key,cam,tm),
                                        tag=tag_of(manifest))
                                    moments.append(card)
                                t.output({'observation_id':record.get('observation_id') if record else None,
                                          'verification':'unverified'})
                        if moments:
                            yield hits(moments)
                            yield from ui('seek',{'t_ms':moments[0].t0_ms,'scene_key':key},key)
                        yield claim_frame([Predicate(predicate_id='p1',subject_refs=subjects,operator='visual_observation',
                                                     value=None,unit=None,status='unknown',eids=observation_eids)],key=key,
                                          moment=moments[0].moment_id if moments else None,
                                          explain={'note':'Visual model observations are unverified; they do not create measured event or geometry facts.'})
                final,stop_reason='UNRESOLVED','no_new_info'
            elif bound_ego_q or ego_scalar_q:
                key=scoped[0]['scene_key']
                note=None
                if len(scoped)!=1 or scoped[0]['workspace']!='fleet': note='Select one Fleet source for an ego measurement.'
                elif ego_scalar_q and (selection or parsed.zone): note='OBD speed belongs only to the recording vehicle; actor or zone constraints were not verified.'
                elif ego_scalar_q and re.search(r'\b(greater|less|more than|under|above|below|over|at least|at most)\b|\d+(?:\.\d+)?\s*(?:m/s|km/h|mps)\b',body.q,re.I): note='The requested speed comparison was not verified.'
                elif bound_ego_q and (len(selection)!=1 or not selection[0].startswith('P')): note='Select one named pedestrian for a bound ego distance.'
                elif ego_scalar_q and requested_time is None: note='Specify a time for the recording vehicle speed sample.'
                elif bound_ego_q and parsed.t_range is None: note='Specify a time or window for the bound ego distance.'
                if note:
                    yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='ego_measurement',value=None,
                                                 unit=None,status='unknown',eids=[])],key=key,explain={'note':note})
                    final,stop_reason='UNRESOLVED','abstained'
                else:
                    target=EntityRef(scene_key=key,entity_id='ego' if ego_scalar_q else selection[0],
                                     kind='vehicle' if ego_scalar_q else 'person')
                    with trace.traced(run,'assess',{'scene_key':key,'target':target.entity_id,'t_range':parsed.t_range},state='measure') as t:
                        yield step_frame(t,'measure','Read the bound recording-vehicle measurement')
                        if ego_scalar_q:
                            res=yield from call(t,'ego_speed','measure',{'scene_key':key,'t_ms':requested_time},
                                                lambda:tools.ego_speed(store,key,requested_time))
                        else:
                            res=yield from call(t,'bound_ego_distance','measure',{'scene_key':key,'t_range':parsed.t_range,
                                'target':target.model_dump(),'zone':parsed.zone},
                                lambda:tools.bound_ego_distance(store,key,parsed.t_range,target,zone=parsed.zone))
                        # the strongest witness: largest lower bound for existential greater / universal less, else smallest upper bound
                        distance_extreme=(None if ego_scalar_q else ('low_max' if maximum_q else 'high_min') if spatial[1] in {'lt','le','gt','ge'}
                                          and not minimum_q else 'min')
                        ev=(res.evidence[('min','max','low_max','high_min').index(distance_extreme)] if distance_extreme else res.evidence[0]) if res.evidence else None
                        _,operator,value,unit=spatial
                        status='supported' if ev and ego_scalar_q else distance_status(ev,operator,value)
                        if not ego_scalar_q and not minimum_q and status==('supported' if always_q else 'contradicted') and (always_q or operator in {'gt','ge'}):
                            status='unknown'  # a sampled extreme cannot show what the unobserved times held
                        yield claim_frame([Predicate(predicate_id='p1',subject_refs=[] if ego_scalar_q else [target],
                            operator='measured_speed' if ego_scalar_q else operator,value=value,
                            unit='km/h' if ego_scalar_q else unit,status=status,eids=[ev.eid] if ev else [])],key=key,
                            explain={'note':res.envelope.output.get('reason')} if not ev else None)
                        final=claims[-1].aggregate
                        if ev:
                            ego_measurement={'eid':ev.eid,'target':target.entity_id,'speed':ego_scalar_q,'extreme':distance_extreme}
                            yield from ui('seek',{'t_ms':ev.t0_ms,'scene_key':key},key)
                        t.output({'aggregate':final,'measurement':res.envelope.output})
                    stop_reason='enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
            elif direct_pair_distance and len(warehouse)!=1:
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='measured',value=None,unit='m',status='unknown',eids=[])],
                                  explain={'note':'Select one source scene: entity IDs are local to each scene.',
                                           'not_measured':['distance: select one source scene']})
                final,stop_reason='UNRESOLVED','abstained'
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
                  and not store.manifest(parsed.scene_key or scoped[0]['scene_key'])['cams']
                  and not parsed.scene_key and len([s for s in scoped if s['workspace']=='fleet'])!=1):
                yield claim_frame([Predicate(predicate_id='p1',subject_refs=[],operator='located',value=None,
                                             unit=None,status='unknown',eids=[])],
                                  explain={'note':'Select one Fleet source for a trip GPS position.'})
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
            elif scalar_speed_q or world_position_q:
                kind='speed' if scalar_speed_q else 'ground_position'
                operator='measured_speed' if scalar_speed_q else 'located'
                unit='m/s' if scalar_speed_q else 'm'
                predicates=[Predicate(predicate_id='p1',subject_refs=[],operator=operator,value=None,unit=unit,status='unknown',eids=[])]
                measurement_ids=list(selection)
                if not measurement_ids and len(warehouse)==1:
                    candidates=[e for e in store.entities(warehouse[0]['scene_key']) if e.kind in requested_kinds]
                    if len(candidates)==1: measurement_ids=[candidates[0].entity_id]
                reason=None
                if len(warehouse)!=1 or len(measurement_ids)!=1:
                    reason='Select one scene and one explicit entity for this measurement.'
                elif requested_time is None:
                    reason='A timestamp is required for this '+('speed' if scalar_speed_q else 'position')+' measurement.'
                elif parsed.t_range and not parsed.t_range[0]<=requested_time<parsed.t_range[1]:
                    reason='The measurement timestamp is outside the requested time window.'
                elif scalar_speed_q and (parsed.zone or spatial and spatial[0]=='distance' or re.search(r'\b(greater|less|more than|under|above|below|over|at least|at most)\b|\d+(?:\.\d+)?\s*(?:m/s|km/h|mps)\b',body.q,re.I)):
                    reason='The requested speed comparison or spatial zone was not verified.'
                    predicates[0].value=body.q
                if reason:
                    explain={'note':reason,'not_measured':[reason]}
                    yield claim_frame(predicates,explain=explain,key=warehouse[0]['scene_key'])
                else:
                    key=warehouse[0]['scene_key'];entity_id=measurement_ids[0]
                    entity=next((e for e in store.entities(key) if e.entity_id==entity_id),None)
                    target=EntityRef(scene_key=key,entity_id=entity_id,kind=entity.kind if entity else 'person')
                    with trace.traced(run,'assess',{'scene_key':key,'t_ms':requested_time,'entity_id':entity_id},state='measure') as t:
                        yield step_frame(t,'measure','Measure tracked speed' if scalar_speed_q else 'Read the measured world ground position')
                        res=yield from call(t,'measure' if scalar_speed_q else 'ground_position','measure',
                                            {'scene_key':key,'t_ms':requested_time,**({'a':target.model_dump(),'kind':'speed','cam':named_cam} if scalar_speed_q else {'target':target.model_dump(),'zone':parsed.zone})},
                                            lambda:tools.measure(store,key,requested_time,target,None,'speed',cam=named_cam) if scalar_speed_q
                                            else tools.ground_position(store,key,requested_time,target,zone=parsed.zone))
                        ev=next((e for e in res.evidence if e.kind=='measure' and e.method==('track_speed' if scalar_speed_q else 'ground_position')),None)
                        missing_uncertainty=bool(ev and not finite_sigma(ev))
                        if ev and scalar_speed_q and (not isinstance(ev.value,(int,float)) or not math.isfinite(ev.value)):
                            ev=None
                        predicates[0].subject_refs=[target]
                        predicates[0].status='supported' if ev and not missing_uncertainty else 'unknown'
                        predicates[0].eids=[ev.eid] if ev else []
                        if (scalar_speed_q and 'action_modifier:moving' in parsed.constraints and len(re.findall(r'\bmoving\b',body.q,re.I))==1
                                and (moving_target(body.q,[target],target) or target.kind in {'forklift','vehicle'} and re.search(r'\b'+re.escape(entity_id)+r'\s+moving\b',body.q,re.I))):
                            predicates.append(Predicate(predicate_id='p2',subject_refs=[target],operator='action_modifier:moving',value='moving',unit=None,
                                                        status='supported' if ev and not missing_uncertainty and ev.value>ev.sigma else 'unknown',eids=[ev.eid] if ev else []))
                        if not ev or missing_uncertainty:
                            reason='missing_measurement_uncertainty' if missing_uncertainty else res.envelope.output.get('reason','missing_measurement')
                            explain={'note':'The requested '+('speed' if scalar_speed_q else 'position')+' was not measured: '+reason.replace('_',' ')+'.',
                                     'not_measured':['requested '+kind.replace('_',' ')+': '+reason.replace('_',' ')]}
                            if missing_uncertainty:
                                explain={'note':'A speed value is available, but its measurement uncertainty is unavailable.',
                                         'not_measured':['speed uncertainty']}
                        else:
                            explain={'note':'Speed comes from the sampled entity track.' if scalar_speed_q else 'Position is a sampled ground point in scene world coordinates, not camera visibility.'}
                        hit=None
                        if ev:
                            cams=store.manifest(key)['cams']
                            observed=[o.cam_id for o in ev.observation_ids if any(c['id']==o.cam_id for c in cams)]
                            if world_position_q and named_cam and named_cam not in observed:
                                predicates.append(Predicate(predicate_id='p'+str(len(predicates)+1),subject_refs=[target],operator='camera_constraint',
                                                            value=named_cam,unit=None,status='unknown',eids=[]))
                            cam=hard_cam or (observed[0] if observed else cams[0]['id'] if cams else '')
                            hit=MomentCard(moment_id=f'm_{key}_{requested_time}',scene_key=key,t0_ms=requested_time,t1_ms=requested_time+1,
                                           cams=[cam] if cam else [],primary_cam=cam,actor_refs=[target],evidence_ids=[ev.eid],score=1,
                                           why=['sampled entity measurement','source view; position proof comes from the measured world point'] if world_position_q else ['sampled track speed'],
                                           thumb=tools.thumbnail(store,key,cam,requested_time) if cam else None,
                                           backend=getattr(store,'backend','duckdb'),exec_state=res.envelope.exec_state)
                            yield hits([hit],special=True)
                        yield claim_frame(predicates,explain=explain,key=key,moment=hit.moment_id if hit else None)
                        t.output({'predicates':[p.model_dump(mode='json') for p in predicates],'explain':explain})
                    if ev:
                        yield from ui('seek',{'t_ms':requested_time,'scene_key':key},key)
                        yield from ui('highlight',{'ids':[entity_id],'mode':'outline'},key)
                final=counted()
                stop_reason='enough_evidence' if final=='SUPPORTED' else 'abstained'
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
                    if hard_cam and not any(c['id']==hard_cam for c in scene['cams']):
                        continue
                    key, cam = scene['scene_key'], hard_cam or scene['cams'][0]['id']
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
            elif blind_other:
                predicates = [Predicate(predicate_id='p1',subject_refs=[],operator='present_in_blind_zone',value=blind_cam,
                                        unit=None,status='unknown',eids=[])]
                if spatial and spatial[0]!='blind_spot':
                    _,operator,value,unit = spatial
                    predicates.append(Predicate(predicate_id='p2',subject_refs=[],operator=operator,value=value,unit=unit,status='unknown',eids=[]))
                yield claim_frame(predicates,key=blind_scenes[0]['scene_key'] if len(blind_scenes)==1 else None,
                                  explain={'note':'The blind-spot check tests people only; '+', '.join(sorted(requested_kinds-{'person'}))+' presence was not checked.'})
                final,stop_reason = 'UNRESOLVED','abstained'
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
                            yield step_frame(t,'inspect','Check sampled person ground points against the camera floor visibility grid')
                            res = yield from call(t,'blind_presence','inspect',{'scene_key':key,'cam':blind_cam,'t0_ms':t0,'t1_ms':t1},
                                                  lambda:tools.blind_presence(store,key,blind_cam,t0,t1,**({'selection':selection} if selection else {}),**({'zone':parsed.zone} if parsed.zone else {})))
                            intervals = res.envelope.output['intervals']
                            persons = list(dict.fromkeys(i['entity_id'] for i in intervals))
                            status = 'supported' if intervals else 'contradicted' if res.envelope.coverage==1 else 'unknown'
                            predicates = [Predicate(predicate_id='p1',subject_refs=[EntityRef(scene_key=key,entity_id=e,kind='person') for e in persons],
                                                    operator='present_in_blind_zone',value=blind_cam,unit=None,status=status,
                                                    eids=[e.eid for e in res.evidence])]
                            if spatial and spatial[0]!='blind_spot':
                                _,operator,value,unit = spatial
                                predicates.append(Predicate(predicate_id='p2',subject_refs=[],operator=operator,value=value,unit=unit,status='unknown',eids=[]))
                            t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                        checked.append((key,intervals,persons,predicates,res.envelope.coverage))
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
                for scene_index, (key, intervals, persons, predicates, coverage) in enumerate(checked):
                    claim_id='c'+str(scene_index+1)
                    if intervals:
                        yield from ui('seek',{'t_ms':intervals[0]['t0_ms'],'scene_key':key},key)
                        yield from ui('highlight',{'ids':persons,'mode':'outline'},key)
                    yield claim_frame(predicates,explain={'note':'Only '+('selected' if selection else 'known')+' people at checked pose sample times were tested against the floor visibility grid. '+('Sampling coverage is incomplete. ' if coverage!=1 else '')+'Unobserved people and times between samples were not verified.',
                                                          'not_measured':['unobserved people','times between samples']},key=key)
                # one scene with nobody in the blind zone must not veto another scene's finding
                aggregates = [c.aggregate for c in claims]
                final = 'SUPPORTED' if 'SUPPORTED' in aggregates else 'CONTRADICTED' if aggregates and all(a=='CONTRADICTED' for a in aggregates) and not exhausted else 'UNRESOLVED'
                stop_reason = 'budget_exhausted' if exhausted else 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                if corpus:
                    yield from abstain_levels('blind_spot','warehouse')
            elif farthest_q:
                distance_extreme='max'
                _, operator, value, unit = spatial
                predicates = [Predicate(predicate_id='p1',subject_refs=[],operator=operator,value=value,unit=unit,status='unknown',eids=[])]
                hit = None
                with trace.traced(run,'assess',{'scene_key':parsed.scene_key,'t_range':parsed.t_range},state='measure') as t:
                    yield step_frame(t,'measure','Scan worker and forklift tracks for the largest sampled distance')
                    scan = yield from call(t,'farthest_sample','measure',{'scene_key':parsed.scene_key,'t_range':parsed.t_range,'zone':parsed.zone},
                                           lambda:farthest_sample(store,[s for s in warehouse if not named_cam or any(c['id']==named_cam for c in s['cams'])],
                                                                  parsed.t_range,parsed.zone,selection=selection or None))
                    farthest = scan.envelope.output['farthest']
                    if farthest:
                        key, tm = farthest['scene_key'], farthest['t_ms']
                        person = EntityRef(scene_key=key,entity_id=farthest['a'],kind='person')
                        vehicle = EntityRef(scene_key=key,entity_id=farthest['b'],kind='forklift')
                        res = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':person.model_dump(),'b':vehicle.model_dump(),'kind':'distance','cam':named_cam},
                                              lambda:tools.measure(store,key,tm,person,vehicle,'distance',cam=named_cam))
                        ev = res.evidence[0] if res.evidence else None
                        predicates[0] = predicates[0].model_copy(update={'subject_refs':[person,vehicle],'status':distance_status(ev,operator,value),
                                                                         'eids':[e.eid for e in res.evidence]})
                    t.output({'predicates':[p.model_dump(mode='json') for p in predicates],'missing':scan.envelope.output['missing']})
                if farthest:
                    cam = next(c['id'] for s in warehouse if s['scene_key']==key for c in s['cams'] if not hard_cam or c['id']==hard_cam)
                    hit = MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam],primary_cam=cam,
                                     actor_refs=[person,vehicle],evidence_ids=[e.eid for e in res.evidence],score=1,
                                     why=['largest sampled distance'],thumb=tools.thumbnail(store,key,cam,tm),
                                     backend=getattr(store,'backend','duckdb'),exec_state=scan.envelope.exec_state)
                    yield hits([hit],special=True)
                    yield from ui('seek',{'t_ms':tm,'scene_key':key},key)
                    yield from ui('highlight',{'ids':[person.entity_id,vehicle.entity_id],'mode':'outline'},key)
                yield claim_frame(predicates,explain=None if farthest else {'note':'No worker and forklift pair was sampled in the requested range.'},
                                  key=hit.scene_key if hit else None,moment=hit.moment_id if hit else None)
                final = claims[-1].aggregate
                stop_reason = 'enough_evidence' if final in {'SUPPORTED','CONTRADICTED'} else 'no_new_info'
                if corpus and farthest:
                    yield from abstain_levels('distance','warehouse')
            elif closest_q:
                distance_extreme='min'
                with trace.traced(run,'plan',{'query':parsed.model_dump(mode='json')},state='plan') as t:
                    yield step_frame(t,'plan','Choose the closest approach path across the full interval')
                    t.output({'template':parsed.template,'path':['measure','answer']})
                predicates, explain = [], {}
                with trace.traced(run,'assess',{'scene_key':parsed.scene_key,'t_range':parsed.t_range},state='measure') as t:
                    yield step_frame(t,'measure','Scan worker and forklift tracks to measure the closest distance')
                    scan = yield from call(t,'closest_approach','measure',{'scene_key':parsed.scene_key,'t_range':parsed.t_range,'zone':parsed.zone},
                                           lambda:tools.closest_approach(store,[s for s in warehouse if not named_cam or any(c['id']==named_cam for c in s['cams'])],
                                                                         parsed.t_range,parsed.zone,**({'selection':selection} if selection else {})))
                    nearest = scan.envelope.output['closest']
                    closest = {'missing':scan.envelope.output['missing'],'scan':scan.evidence[0] if scan.evidence else None}
                    if nearest:
                        key, tm = nearest['scene_key'], nearest['t_ms']
                        person = EntityRef(scene_key=key,entity_id=nearest['a'],kind='person')
                        vehicle = EntityRef(scene_key=key,entity_id=nearest['b'],kind='forklift')
                        res = yield from call(t,'measure','measure',{'scene_key':key,'t_ms':tm,'a':person.model_dump(),'b':vehicle.model_dump(),'kind':'distance','cam':named_cam},
                                              lambda:tools.measure(store,key,tm,person,vehicle,'distance',cam=named_cam))
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
                    cam = next(c for s in warehouse if s['scene_key']==key for c in s['cams'] if not hard_cam or c['id']==hard_cam)
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
                    video = [p for p in passes if any(s['scene_key']==p['trip_id'] and s['cams']
                                                      and (not hard_cam or any(c['id']==hard_cam for c in s['cams'])) for s in fleet)]
                    asked = geo.trim_passes(store,video,parsed.t_range)
                    t.output({'passes':passes,'video_passes':video,'asked_passes':asked})
                building = {'side':side,'video':bool(video)}
                if not passes or video and not asked:
                    final, stop_reason = 'OUT_OF_RANGE','abstained'
                elif video:
                    place_verified=True  # The facade path reads source-bound GPS passes.
                    with trace.traced(run,'assess',{'passes':asked,'side':side},state='inspect') as t:
                        yield step_frame(t,'inspect','Read building facades at the time closest to the place')
                        res = yield from call(t,'facade','inspect',{'passes':asked,'lat':place['lat'],'lon':place['lon'],'side':side},
                                              lambda:tools.facade(store,asked,place['lat'],place['lon'],side))
                        found = res.envelope.output
                        predicates = [Predicate(predicate_id='p1',subject_refs=[],operator='facade',
                                                value=found['building']['label'] if found['building'] else None,unit=None,
                                                status='supported' if found['building'] else 'unknown',eids=[e.eid for e in res.evidence])]
                        if spatial and spatial[1]!='facade':
                            _,operator,value,unit = spatial
                            predicates.append(Predicate(predicate_id='p2',subject_refs=[],operator=operator,value=value,unit=unit,status='unknown',eids=[]))
                        t.output({'predicates':[p.model_dump(mode='json') for p in predicates]})
                    if found['building']:
                        key, tm = found['scene_key'], found['t_ms']
                        cam = next(c for s in fleet if s['scene_key']==key for c in s['cams'] if not hard_cam or c['id']==hard_cam)
                        hit = MomentCard(moment_id=f'm_{key}_{tm}',scene_key=key,t0_ms=tm,t1_ms=tm+1,cams=[cam['id']],primary_cam=cam['id'],
                                         actor_refs=[],evidence_ids=[e.eid for e in res.evidence],score=1,why=['osm_facade'],
                                         thumb=tools.thumbnail(store,key,cam['id'],tm),
                                         backend=getattr(store,'backend','duckdb'),exec_state=res.envelope.exec_state)
                        yield hits([hit],special=True)
                    if found['t_ms'] is not None:
                        yield from ui('seek',{'t_ms':found['t_ms'],'scene_key':found['scene_key']},found['scene_key'])
                    if found['building']:
                        yield from ui('highlight',{'ids':[found['building']['bid']],'mode':'outline'},found['scene_key'])
                    yield claim_frame(predicates,explain={'not_measured':['camera-visible building name or sign']},key=found.get('scene_key') or asked[0]['trip_id'],
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
                            from app.core.task_spec import consume_verified_place_prefix
                            parsed=parsed.model_copy(update={'constraints':consume_verified_place_prefix(body.q,parsed.place,parsed.constraints)})
                            passes = yield from call(t,'find_passes','retrieve',{'place_id':place.envelope.output['place_id'],'r_m':geo.NEAR_M,'fov_deg':360},
                                                     lambda:tools.find_passes(store,place.envelope.output['place_id'],geo.NEAR_M,360))
                            if not passes.envelope.output:
                                final,stop_reason='OUT_OF_RANGE','abstained'
                            else:
                                place_verified=True  # Candidate retrieval is constrained to these GPS passes.
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
                            cam=hard_cam or store.manifest(key)['cams'][0]['id']
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
                                       'place':parsed.place,'zone':parsed.zone,'sources':parsed.sources,'selection':context.get('selection'),'t_ms':context.get('t_ms'),'cam_id':hard_cam,**({'scope':'corpus'} if corpus else {})}
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
                            # a fleet event without actors is the recording vehicle's own (hard_brake)
                            event_ids = {e.event_id for e in related if e.source!='model' and wanted and e.type in wanted and
                                         (not e.actor_ids and manifest['workspace']=='fleet' or
                                          {s.entity_id for s in subjects if s.kind=='person'}<=set(e.actor_ids))}
                            event_eids = [eid for eid in card.evidence_ids if read[eid].kind=='measure' and
                                          isinstance(read[eid].value,dict) and read[eid].value.get('event_id') in event_ids]
                            # the measured events of this claim: the consumed ones and those of its own subjects
                            own = [e for e in related if e.source!='model' and (e.event_id in event_ids or set(e.actor_ids)&{s.entity_id for s in subjects})]
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
                                lo, hi = (max(tm,parsed.t_range[0]),min(card.t1_ms,parsed.t_range[1])) if parsed.t_range else (tm,card.t1_ms)
                                # Scene scope only: corpus keeps its tools for the level reserve.
                                if (not corpus and manifest['workspace']=='fleet' and not ego_measured and not selection and not parsed.zone
                                    and any(a.kind=='person' for a in card.actor_refs) and lo<hi):
                                    ego_measured = True
                                    yield from call(t,'ego_distance','measure',{'scene_key':key,'cam':cam,'t0_ms':lo,'t1_ms':hi},
                                                    lambda:tools.ego_distance(store,key,cam,lo,hi,rows=site_rows() if vss is not None else []))
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
                                            link_obs = [o for o in ev.observation_ids if lo<=o.t_ms<hi and any(
                                                e.association.get('valid') and
                                                (not isinstance(e.association['valid'],list) or len(e.association['valid'])==2 and
                                                 e.association['valid'][0]<=o.t_ms<e.association['valid'][1]) and
                                                any(p['cam_id']==o.cam_id and p['local_track_id']==o.local_track_id and
                                                    p['t0_ms']<=o.t_ms<p['t1_ms'] for p in e.observations) for e in matched)]
                                            if link_obs:
                                                verified[eid] = link_obs
                                    observed_cams = {o.cam_id for refs in verified.values() for o in refs}
                                    requested_cams = {c['id'] for c in manifest['cams'] if re.search(
                                        r'(?<![A-Za-z0-9_])'+re.escape(c['id'])+r'(?![A-Za-z0-9_])',body.q,re.I)}
                                    valid = len(set(ids))==1 and len(matched)==1 and requested_cams<=observed_cams
                                    if re.search(r'\bclips?\b',body.q,re.I):
                                        clip_ids = {r['clip_id'] for r in res.envelope.output['expanded_ranges'] if r['clip_id'].rsplit(':',2)[0]==key}
                                        clips = {clip_id for clip_id in clip_ids if any(
                                            o.cam_id==clip_id.rsplit(':',2)[1] and int(clip_id.rsplit(':',1)[1])*5000<=o.t_ms<
                                            (int(clip_id.rsplit(':',1)[1])+1)*5000 for refs in verified.values() for o in refs)}
                                        requested_clips = set(re.findall(r'[a-z0-9_-]+:[a-z0-9_-]+:\d{5}',body.q,re.I))
                                        valid &= len(clips)>=2 and requested_clips<=clips
                                    else:
                                        valid &= len(observed_cams)>=2
                                    valid &= all(tools.sync_trusted(manifest,c) for c in manifest['cams'] if c['id'] in observed_cams)
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
                                                                    status='supported' if move_ev and finite_sigma(move_ev) and isinstance(move_ev.value,(int,float))
                                                                    and math.isfinite(move_ev.value) and move_ev.value>move_ev.sigma else 'unknown',
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
                                              for c in manifest['cams'] if not hard_cam and c['id']!=cam and tools.sync_trusted(manifest,c)]
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
                                    situation = yield from call(t,'situation','assess',{'scene_key':key,'t_ms':tm,'actor_refs':[s.model_dump() for s in subjects],'cam':cam,'t_range':parsed.t_range},
                                                                lambda:tools.situation(store,key,tm,subjects,cam=cam,t_range=parsed.t_range,run=run,circuit=circuit))
                                    record = situation.envelope.output.get('record')
                                    expected='yielding' if any(e.type=='dodge' for e in own) else 'crossing' if any(e.type=='ped_crossing' for e in own) else None
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
                                    card_events = {read[e].value.get('event_id') for e in card.evidence_ids if read[e].kind=='measure' and isinstance(read[e].value,dict)}
                                    for event in own:
                                        if event.severity>=2 and {person.entity_id,vehicle.entity_id}<=set(event.actor_ids) and event.event_id in card_events:
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
                elif not moments and not speeding_q:
                    if final=='NO_MATCH' and (any(c.startswith(('unconsumed:','attribute:')) for c in parsed.constraints) or
                                               spatial and any(c.startswith('requested_action:') for c in parsed.constraints)):
                        claim_id='c'+str(len(claims)+1)
                        yield claim_frame([],explain={'note':'No candidate verified every requested condition.'})
                        final='UNRESOLVED'
                    stop_reason='abstained'  # Retrieval's missing-evidence verdict must survive.
        if late:
            raise BudgetExhausted

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
                parts = {}
                for c in group:
                    part = compose(final,[c],evidence)
                    if part['text'] in parts:
                        parts[part['text']]['claim_ids'] += part['claim_ids']
                    else:
                        parts[part['text']] = part
                return join_answer_parts(list(parts.values()))
            evidence = {eid:evidence[eid] for c in group for p in c.predicates for eid in p.eids if eid in evidence}
            text = TEXT[final]
            if final=='UNSUPPORTED' and not re.search(llm.FORBIDDEN,body.q,re.I):
                text=UNMAPPED
            abstain = next((c.explain['abstain']['text'] for c in group if abstained(c)),None)
            if final=='UNRESOLVED' and abstain:
                text=abstain
            missing = list(dict.fromkeys(quantity_name(p) for c in group for p in c.predicates if p.status=='unknown'))
            if final=='UNRESOLVED' and missing and not abstain:
                text='Not measured: '+', '.join(missing).replace('_',' ')+'. The full question remains unresolved.'
            if final=='UNRESOLVED' and (any(p.operator in {'measured_speed','located'} for c in group for p in c.predicates) or direct_pair_distance):
                notes=[c.explain.get('note') for c in group if c.explain and c.explain.get('note')]
                if notes and not any(p.status=='supported' and p.operator in {'measured_speed','located'} for c in group for p in c.predicates):
                    text=' '.join(notes)+' The full question remains unresolved.'
            refs, cites, values, names, formats = [], [], [], [], []
            def ref(e, field, value, fmt='.4g'):
                if e.kind=='model':
                    raise ValueError('Model output cannot create measurement references')
                refs.append({'eid':e.eid,'field':field}); cites.append(e.eid); values.append(value); formats.append(fmt)
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
            if final in {'NO_MATCH','UNRESOLVED'} and covered:
                kind=next(iter(event_types(body.q) or ()),None)
                what='the '+noun('person',workspace)+' '+VERBS[kind].replace('{other}','the '+noun('vehicle',workspace)) if kind in VERBS else 'a matching event occurred'
                count,*intervals=covered
                text=('Checked '+ref(count,'value.count',count.value['count'])+' video(s) passing '+name(parsed.place)+' ('+'; '.join(
                    name(trip_name(e.scene_key))+', '+ref(e,'value.t0_s',e.value['t0_s'])+'–'+ref(e,'value.t1_s',e.value['t1_s'])+' s'
                    for e in intervals)+'); no evidence that '+what+'.')
                if final=='UNRESOLVED':
                    text+=' The requested event remains unresolved; this search does not verify absence.'
            predicates=[p for c in group for p in c.predicates]
            direct_numeric=any(p.operator in {'measured_speed','located'} and any(
                eid in evidence and evidence[eid].method in {'track_speed','ground_position'} for eid in p.eids) for p in predicates)
            unresolved_text=text
            independent_distance=next((evidence[eid] for p in predicates if p.status=='supported' and p.operator=='measured'
                                       for eid in p.eids if eid in evidence and evidence[eid].kind=='measure' and evidence[eid].method=='ground_distance'),None)
            if final in {'SUPPORTED','CONTRADICTED'} or direct_numeric:
                cites=list(dict.fromkeys(eid for c in group for p in c.predicates for eid in p.eids))
                blind=next((p for p in predicates if p.operator=='present_in_blind_zone'),None)
                facade=next((p for p in predicates if p.operator=='facade'),None)
                speed = next((e for e in evidence.values() if e.method=='obd_speed'),None)
                if facade:
                    mapped=next((e for e in evidence.values() if e.method=='osm_facade'),None)
                    poi_label=next((p.get('name') for p in mapped.value.get('pois',[])),None) if mapped else None
                    label_source=('building name' if mapped and mapped.value.get('name') else 'POI label' if poi_label else 'address' if mapped and mapped.value.get('addr') else 'OSM identifier')
                    text='The mapped facade '+SIDES[building['side']]+' at '+name(parsed.place)+' has '+label_source+' '+name(facade.value)+'. This map association does not verify the camera-visible building name or sign.'
                elif blind:
                    found=sorted((evidence[e] for p in predicates if p.operator=='present_in_blind_zone' for e in p.eids),
                                 key=lambda e:(e.t0_ms,e.value['entity_id']))
                    if found:
                        text='Sampled ground points of '+('selected people' if selection else 'known people')+' fall in the floor blind cells of '+name(blind.value)+': '+'; '.join(
                            'the '+noun('person',workspace)+' '+name(e.value['entity_id'])+' at '+ref(e,'value.t0_s',e.value['t0_s'])+'–'
                            +ref(e,'value.t1_s',e.value['t1_s'])+' s ('+('floor cells marked visible to '+name(', '.join(e.value['seen_by'])) if e.value['seen_by'] else 'no other camera marked visible')+')'
                            for e in found)+'.'
                    else:
                        text='No blind-zone presence was found among '+('selected people' if selection else 'known people')+' at checked ground-point sample times in the '+name(blind.value)+' floor visibility grid.'
                elif any(e.method=='gps_position' for e in evidence.values()) and any(p.operator=='located' for p in predicates):
                    gps=next(e for e in evidence.values() if e.method=='gps_position')
                    text=('The recording vehicle GPS position was latitude '+ref(gps,'value.lat',gps.value['lat'],'.6f')+
                          '°, longitude '+ref(gps,'value.lon',gps.value['lon'],'.6f')+'° at GPS sample time '+
                          ref(gps,'value.t_ms',gps.value['t_ms']/1000)+' s ('+name(gps.value['state'])+
                          '; uncertainty unavailable). Camera observations were not checked.')
                elif any(e.method in {'ground_position','track_speed'} for e in evidence.values()) and any(p.operator in {'located','measured_speed'} for p in predicates):
                    position=next((e for e in evidence.values() if e.method=='ground_position'),None)
                    speed=next((e for e in evidence.values() if e.method=='track_speed'),None)
                    target=next((s for p in predicates for s in p.subject_refs),None)
                    if position:
                        text=(name(position.value['entity_id'])+' was at scene world ground coordinates x='+ref(position,'value.x_m',position.value['x_m'])+
                              ' m, y='+ref(position,'value.y_m',position.value['y_m'])+' m (±'+ref(position,'sigma',position.sigma)+
                              ' m), at '+ref(position,'value.t_s',position.value['t_s'])+' s; source '+name(position.value['source'])+'.')
                    elif speed:
                        speed_value=ref(speed,'value',speed.value)
                        uncertainty=('±'+ref(speed,'sigma',speed.sigma)+' m/s'
                                     if isinstance(speed.sigma,(int,float)) and math.isfinite(speed.sigma) and speed.sigma>=0 else 'uncertainty unavailable')
                        text=(name(target.entity_id)+' had measured track speed '+speed_value+' m/s ('+uncertainty+
                              ') at '+ref(speed,'t0_ms',speed.t0_ms/1000)+' s.')
                    if final=='UNRESOLVED': text+=' '+unresolved_text
                elif closest:
                    ev = next(e for e in evidence.values() if e.method=='ground_distance')
                    scan = next(e for e in evidence.values() if e.method=='closest_approach')
                    text=('The minimum observed distance from the worker ground point to the forklift center was '+ref(ev,'value',ev.value)+' m (±'+ref(ev,'sigma',ev.sigma)+' m), at '
                          +ref(scan,'value.t_s',scan.value['t_s'])+' s'
                          +('' if predicates[0].operator!='lt' else ', less than the requested distance' if final=='SUPPORTED' else ', not less than the requested distance')+'.')
                    if direct_pair_distance:
                        text=text[:-1]+' in the sampled window '+ref(scan,'value.window_t0_s',scan.value['window_t0_s'])+'–'+ref(scan,'value.window_t1_s',scan.value['window_t1_s'])+' s.'
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
                    if ev and when and not event:
                        clause+=' at sampled time '+ref(ev,'t0_ms',ev.t0_ms/1000)+' s'
                    if clause:
                        text=clause+'.' if final=='SUPPORTED' else text+' '+clause+'.'
            if final=='UNRESOLVED' and independent_distance and not direct_numeric and not any((c.explain or {}).get('conflict') for c in group):
                quantity=('distance from the worker ground point to the forklift center' if workspace=='warehouse' else 'ground distance between the requested entities')
                text+=' Independently measured '+quantity+': '+ref(independent_distance,'value',independent_distance.value)+' m (±'+ref(independent_distance,'sigma',independent_distance.sigma)+' m) at '+ref(independent_distance,'t0_ms',independent_distance.t0_ms/1000)+' s.'
            if any(p.operator=='present_in_blind_zone' for p in predicates):
                if final=='UNRESOLVED':
                    text+=' Ground-point samples of '+('selected people' if selection else 'known people')+' were checked against the requested floor visibility grid.'
                    if any('Sampling coverage is incomplete.' in (c.explain or {}).get('note','') for c in group):
                        text+=' Sampling coverage is incomplete.'
                text+=' Unobserved people and times between samples were not verified.'
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
                refs, cites, values, formats = [], [], [], []
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
                refs, cites, values, formats = [], [], [], []
            if ego_measurement and ego_measurement['eid'] in evidence:
                ev=evidence[ego_measurement['eid']]
                measurement=('Recording vehicle OBD speed: '+ref(ev,'value',ev.value)+' km/h at sample '+ref(ev,'t0_ms',ev.t0_ms/1000)+
                             ' s. Speed uncertainty is unavailable.' if ego_measurement['speed'] else
                             ('Minimum observed' if ego_measurement['extreme']=='min' else 'Sampled')+' ground distance from '+name(ego_measurement['target'])+' to the recording vehicle origin: '+
                             ref(ev,'value.distance_m',ev.value['distance_m'])+' m (±'+ref(ev,'sigma',ev.sigma)+' m) at '+ref(ev,'t0_ms',ev.t0_ms/1000)+' s. Times between samples were not verified.')
                text=measurement if final in {'SUPPORTED','CONTRADICTED'} else text+' Independently measured: '+measurement
            cites=list(dict.fromkeys(cites))
            parts = re.split(r'(\{\{[mn]\d+\}\})',text)
            text=''.join(p if re.fullmatch(r'\{\{[mn]\d+\}\}',p) else llm.sanitize_numbers(p,[]) for p in parts)
            text,spans=render_cited_text(text,refs,values,names,formats)
            return dict(text=text,claim_ids=[c.claim_id for c in group],cites=cites,measure_refs=refs,text_spans=spans)

        def statistics_sentence(cards):
            evidence = {eid:read[eid] for c in cards for p in c.predicates for eid in p.eids if eid in read}
            parts, refs, values = [], [], []
            for e in evidence.values():
                if e.method not in {'bytetrack_people_count','yolo_people_peak'}:
                    continue
                v = e.value
                def number(field):
                    refs.append({'eid':e.eid,'field':'value.'+field})
                    values.append(v[field])
                    return '{{m'+str(len(values)-1)+'}}'
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
            text,spans=render_cited_text(' '.join(parts),refs,values,[],['.4g']*len(values))
            return dict(text=text,claim_ids=[c.claim_id for c in cards],cites=list(evidence),measure_refs=refs,text_spans=spans)

        def street_sentence(cards):
            """L1: what the 2D detections show; a spatial question keeps its abstain text and cites no measurement."""
            if any(p.operator in {'tracked_count','peak_count'} and p.eids for c in cards for p in c.predicates):
                return statistics_sentence(cards)
            detected = [p for c in cards for p in c.predicates if p.operator=='detected' and isinstance(p.value,list)]
            found = [p for p in detected if p.status=='supported']
            what = ' and '.join(('an ' if k[0] in 'aeiou' else 'a ')+k for k in (detected[0].value if detected else []))
            if not what:
                text = 'Street camera clips were retrieved by their captions; captions and Cosmos are model output and confirm nothing.'
            elif found:
                text = 'A street camera shows '+what+' in the same frame (2D).'
            else:
                text = 'The street camera frames read do not show '+what+' together; a missed 2D detection does not rule this out.'
            abstain = next((c.explain['abstain']['text'] for c in cards if abstained(c)),None)
            missing = list(dict.fromkeys(quantity_name(p) for c in cards for p in c.predicates if p.status=='unknown' and p.operator!='detected'))
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
            return join_answer_parts(parts,evidence_level=lv)

        def cited(part, sentence, cites=()):
            """A fact frame; `sentence(ref, name)` places numbers only through measure refs, as in compose."""
            refs, values, names, formats = [], [], [], []
            def ref(e, field, value):
                if e.kind=='model':
                    raise ValueError('Model output cannot create measurement references')
                refs.append({'eid':e.eid,'field':field}); values.append(value); formats.append('.4g')
                return '{{m'+str(len(values)-1)+'}}'
            def name(value):
                names.append(value)
                return '{{n'+str(len(names)-1)+'}}'
            parts = re.split(r'(\{\{[mn]\d+\}\})',sentence(ref,name))
            text,spans = render_cited_text(''.join(p if re.fullmatch(r'\{\{[mn]\d+\}\}',p) else llm.sanitize_numbers(p,[]) for p in parts),
                                           refs,values,names,formats)
            return dict(part=part,text=text,claim_ids=[],cites=list(dict.fromkeys([*cites,*(r['eid'] for r in refs)])),
                        measure_refs=refs,text_spans=spans)

        def focus_of():
            """The moment a follow-up refers to: the context's time (and selection), else the first moment sent in hits."""
            key = parsed.scene_key or context.get('scene_key')
            hit = next((m for f in frames if f['event']=='hits' for m in f['data']['moments']),None)
            if context.get('t_ms') is not None:
                t, ids, resolved = context['t_ms'], context.get('selection') or [], True
            # an unparsed time condition leaves the hit's time outside what the question allows
            elif hit and 'temporal_relation' not in parsed.constraints and not any(re.fullmatch(r'unconsumed:\d+',c) for c in parsed.constraints):
                key, t, ids, resolved = key or hit['scene_key'], hit['t0_ms'], [r['entity_id'] for r in hit['actor_refs']], False
            else:
                return None
            manifest = store.manifest(key) if key else None
            if manifest is None or not manifest['t0_ms']<=t<manifest['t1_ms'] or parsed.t_range and not parsed.t_range[0]<=t<parsed.t_range[1]:
                return None
            person = next((i for i in ids if re.fullmatch(r'P\d+',i)),None)
            forklift = next((i for i in ids if re.fullmatch(r'F\d+',i)),None)
            moment = store.moment(key,t) if manifest['workspace']=='warehouse' else None
            if moment and moment.forklift and not (person and forklift):
                f = next((f for f in moment.forklift if f.entity_id==forklift),moment.forklift[0])
                near = min(moment.persons,key=lambda p:math.dist(p.root_world,(f.x,f.y)),default=None)
                forklift, person = f.entity_id, person or (near.entity_id if near else None)
            return dict(scene_key=key,t_ms=t,person=person,forklift=forklift,resolved=resolved,manifest=manifest,moment=moment)

        def analyse():
            """Answers for a question no template measured: what is unmet and why, the facts measured at the focus,
            and suggested follow-ups. Its reads do not count against the tool budget."""
            focus = focus_of()
            parts = followup.unmet_parts(body.q,parsed,bool(focus and focus['resolved']))
            unmet = dict(part='unmet',text=' '.join(parts) or 'The question asks for something the tools do not measure directly; below is what was measured.',
                         claim_ids=[c.claim_id for c in claims],cites=[],measure_refs=[],text_spans=[])
            if focus is None:
                return [unmet], []
            key, t, person, forklift = focus['scene_key'], focus['t_ms'], focus['person'], focus['forklift']
            ids = [i for i in (person,forklift) if i]
            yield from ui('seek',{'t_ms':t,'scene_key':key},key)
            if ids:
                yield from ui('highlight',{'ids':ids,'mode':'outline'},key)
            point = next((p for p in focus['moment'].persons if p.entity_id==person),None) if focus['moment'] else None
            if point:
                yield from ui('annotate',{'kind':'label','t_ms':t,'xy':list(point.root_world),
                                          'text':person+' · '+('seen by '+', '.join(point.observed_in) if point.observed_in else 'no camera detected')},key)
            if focus['manifest']['workspace']!='warehouse' or not focus['manifest']['metric']:
                return [unmet], []
            refs = {i:EntityRef(scene_key=key,entity_id=i,kind='person' if i.startswith('P') else 'forklift') for i in ids}
            # a camera the question names still constrains these reads
            cam = hard_cam if hard_cam and re.search(r'(?<![A-Za-z0-9_])'+re.escape(hard_cam)+r'(?![A-Za-z0-9_])',body.q,re.I) else None
            reads = []
            if person and forklift:
                reads.append(('distance','measure',{'scene_key':key,'t_ms':t,'a':refs[person].model_dump(),'b':refs[forklift].model_dump(),'kind':'distance','cam':cam},
                              lambda:tools.measure(store,key,t,refs[person],refs[forklift],'distance',cam=cam)))
            if forklift:
                reads.append(('speed','measure',{'scene_key':key,'t_ms':t,'a':refs[forklift].model_dump(),'b':None,'kind':'speed','cam':cam},
                              lambda:tools.measure(store,key,t,refs[forklift],None,'speed',cam=cam)))
            if person:
                reads.append(('visibility','visibility',{'scene_key':key,'t_ms':t,'target':person,'cam':cam},lambda:tools.visibility(store,key,t,person,cam=cam)))
                # the event window never leaves the question's own time scope
                lo, hi = max(0,t-2000), t+2000
                if parsed.t_range:
                    lo, hi = max(lo,parsed.t_range[0]), min(hi,parsed.t_range[1])
                reads.append(('events','events',{'scene_key':key,'t0_ms':lo,'t1_ms':hi,'selection':[person]},
                              lambda:tools.events(store,key,lo,hi,selection=[person])))
            if ids and {followup.ACTIVITY_TEXT,followup.GAZE_TEXT}&set(parts):
                reads.append(('model','situation',{'scene_key':key,'t_ms':t,'actor_refs':[r.model_dump() for r in refs.values()],'cam':cam,'t_range':None},
                              lambda:tools.situation(store,key,t,list(refs.values()),cam=cam,run=run,circuit=circuit)))
            results = {}
            with trace.traced(run,'assess',{'scene_key':key,'t_ms':t,'ids':ids},state='inspect') as step:
                yield step_frame(step,'inspect','Measure what is known at the focused moment')
                for kind, tool, args, fn in reads[:5]:
                    try:
                        results[kind] = yield from call(step,tool,'inspect',args,fn,budgeted=False)
                    except (BudgetExhausted, DeadlineExceeded):
                        break
                step.output({kind:[e.eid for e in res.evidence] for kind, res in results.items()})
            facts = []
            if results.get('distance') and results['distance'].evidence:
                ev = results['distance'].evidence[0]
                facts.append(cited('fact',lambda ref,name:'At '+ref(ev,'t0_ms',ev.t0_ms/1000)+' s, the worker '+name(person)+' was '+ref(ev,'value',ev.value)
                                   +' m (±'+ref(ev,'sigma',ev.sigma)+' m) from forklift '+name(forklift)+' (worker ground point to forklift center).'))
            if results.get('speed') and results['speed'].evidence:
                sp = results['speed'].evidence[0]
                facts.append(cited('fact',lambda ref,name:'Forklift '+name(forklift)+' was moving at '+ref(sp,'value',sp.value)+' m/s'
                                   +(' (±'+ref(sp,'sigma',sp.sigma)+' m/s)' if finite_sigma(sp) else '')+'.'))
            if results.get('visibility') and results['visibility'].evidence:
                vis = results['visibility'].evidence[0]
                observed, hidden = vis.value['person']['observed_in'], vis.value['floor_cell']['hidden_from']
                facts.append(cited('fact',lambda ref,name:('Cameras that detected '+name(person)+' at that time: '+name(', '.join(observed))+'.' if observed
                                                           else 'No camera detected '+name(person)+' at that time.')
                                   +(' The floor under '+name(person)+' is hidden from '+name(', '.join(hidden))+'.' if hidden else ''),[vis.eid]))
            if results.get('events') and results['events'].envelope.exec_state!='failed':
                recorded = [e for e in results['events'].evidence if e.kind=='measure']
                if recorded:
                    facts.append(cited('fact',lambda ref,name:' '.join(
                        'Recorded event: the worker '+VERBS.get(e.value['type'],'was involved in a recorded event').replace('{other}','the forklift')+' at '
                        +(ref(e,'value.t_s',e.value['t_s']) if 't_s' in e.value else ref(e,'value.t0_ms',e.value['t0_ms']/1000))+' s.' for e in recorded),
                        [e.eid for e in recorded]))
                elif not results['events'].evidence:
                    facts.append(cited('fact',lambda ref,name:'No recorded event involves '+name(person)+' within two seconds of that time.'))
            record = results['model'].envelope.output.get('record') if results.get('model') and results['model'].evidence else None
            if record:
                facts.append(cited('model',lambda ref,name:'Model description (Cosmos, unverified): '+record['payload']['label'].replace('_',' ')+'.',
                                   [results['model'].evidence[0].eid]))
            return [unmet,*facts], followup.suggestions('warehouse',{'scene_key':key,'t_ms':t,'person':person,'forklift':forklift})

        if corpus and claims:
            # an overall sentence, then one per level group
            overall=('At least one checked source supports the question; other sources may remain unresolved.' if final=='SUPPORTED'
                     else 'The checked sources leave the full question unresolved.' if final=='UNRESOLVED' else TEXT[final])
            answers = [dict(text=overall,claim_ids=[c.claim_id for c in claims],cites=[],measure_refs=[],evidence_level=None),
                       *(level_sentence(lv,[c for c in claims if c.evidence_level==lv]) for lv in LEVELS
                         if any(c.evidence_level==lv for c in claims))]
        elif corpus:
            answers = [{**compose(final,claims,read),'evidence_level':None}]
        elif claims and all(c.evidence_level=='L1' for c in claims) and not any(
                c.predicates and c.predicates[0].operator=='visual_observation' for c in claims):
            answers = [street_sentence(claims)]
        else:
            answers = [compose(final,claims,read)]
        if body.protocol_version==2 and observations:
            facts=[(record,i,fact) for record in observations for i,fact in enumerate(record['facts'])]
            if facts:
                unmet=list(dict.fromkeys(quantity_name(p) for c in claims for p in c.predicates
                                          if p.status=='unknown' and p.operator!='visual_observation'))
                zone_note=(' These observations cover full camera frames; the selected zone was not visually localized or verified.' if parsed.zone else '')
                if parsed.place and not place_verified:
                    zone_note+=' The requested place was not verified for these frames.'
                answers=[dict(text='Model observation (unverified): '+' '.join(fact['text'] for _,_,fact in facts)+'.'+zone_note+
                              ' These sampled visual observations do not independently verify the full question or a geometric measurement.'+
                              (' Unverified conditions: '+', '.join(unmet)+'.' if unmet else ''),
                              claim_ids=[c.claim_id for c in claims],cites=list(dict.fromkeys(record['eid'] for record,_,_ in facts)),
                              measure_refs=[],text_spans=[],model_refs=[{'observation_id':record['observation_id'],
                                  'eid':record['eid'],'field':'facts.'+str(i)+'.text'} for record,i,_ in facts])]
        suggested = []
        # an unmapped or unmeasured question answers with what is measured at its focus instead of a generic sentence
        if (not corpus and parsed is not None and parsed.workspace=='warehouse' and stop_reason!='error'
                and (final=='UNRESOLVED' or final=='UNSUPPORTED' and not re.search(llm.FORBIDDEN,body.q,re.I))
                # a template question keeps its own abstention; only words no tool measures, or a non-English question, add the analysis
                and (final=='UNSUPPORTED' or any(c.startswith('unconsumed:') for c in parsed.constraints)
                     or any(not ch.isascii() and ch.isalpha() for ch in body.q))
                and all(re.fullmatch(r'(?:Not measured: [^.]*\. The full question remains unresolved\.(?: |$))+',a['text'])
                        or a['text']==UNMAPPED for a in answers)):
            answers, suggested = yield from analyse()
        with trace.traced(run,'answer',{'final':final,'read_eids':sorted(run.read_eids)},state='answer') as t:
            yield step_frame(t,'answer','Generate an answer from the evidence read')
            if body.protocol_version==2:
                answers=[{'text_spans':[],'model_refs':[],**a} for a in answers]
            else:
                answers=[{k:v for k,v in a.items() if k not in {'text_spans','model_refs'}} for a in answers]
            answers = [{'sentence_id':'a'+str(i+1),**a} for i, a in enumerate(answers)]
            t.output(answers[0] if len(answers)==1 else answers)
            for answer in answers:
                yield frame('answer',**answer)
        if suggested:
            yield frame('suggest',questions=suggested)
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
    settings=getattr(request.app.state,'settings',None)
    if getattr(settings,'mode','dev')=='site' and getattr(request.app.state,'vss',None) is None:
        raise HTTPException(503,'site VSS is not configured')
    store=request.app.state.store
    if body.protocol_version==2 and body.session_id:
        # The durable service resolves inherited context before validating its
        # source; an omitted workspace must not be indexed here prematurely.
        service=request.app.state.investigations
        job=service.submit(body)
        return StreamingResponse(service.stream(job['job_id']),media_type='text/event-stream',
            headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no',
                     'X-SVA-Session':job['session_id'],'X-SVA-Turn':job['turn_id'],'X-SVA-Job':job['job_id']})
    context = body.context.model_dump(exclude_unset=True)
    key = context.get('scene_key') or (context['scene_id'].replace('/','-').lower() if context.get('scene_id') else None)
    if key:
        try:
            manifest = store.manifest(key)
        except FileNotFoundError:
            raise HTTPException(422,'context scene does not exist') from None
        if manifest['workspace']!=context['workspace']:
            raise HTTPException(422,'context scene does not belong to workspace')
    def sse(frame):
        return 'event: '+frame['event']+'\ndata: '+json.dumps(frame['data'],ensure_ascii=False)+'\n\n'
    def stream():
        hit=cache.replay(store,body.q,context) if body.protocol_version==1 else None
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
        if body.protocol_version==1 and status and status['stop_reason'] in {'enough_evidence','no_new_info','abstained'} and complete:
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
def list_runs(request: Request, scene_key: str | None = None, limit: int = Query(20, ge=1, le=200)):
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
