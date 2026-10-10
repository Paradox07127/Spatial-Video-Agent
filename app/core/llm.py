"""Deterministic templates; the language model classifies only questions without a template keyword."""
from __future__ import annotations

import json
import os
import re

import httpx
from pydantic import ValidationError

from app.core.models import Query, scene_key_of

FORBIDDEN = r"\b(who is|who was|who are|who's|who drove|whose|identity|name of|intent|intend(?:ed|s|ing)?\b|meant to|on purpose|purpose|trying to|planning to|wants? to|motive|who caused|deliberately|why|fault|responsib)"


def rule_parse(q: str, ctx: dict) -> Query:
    text = q.lower()
    if re.search(FORBIDDEN, text):
        template = 'unsupported'
    elif re.search(r'\b(link|same person|same one|before.*after|enter before|entered before|prior to|related)\b', text):
        template = 'link'
    elif re.search(r'\b(what happened|describe|explain)\b', text):
        template = 'what_happened'
    elif re.search(r'\b(find|show|list)\b', text):
        template = 'find'
    elif re.search(r"\b(blind|can't see|cannot see)\b", text):
        template = 'find'
    elif re.search(r'\b(where|near|building|junction|intersection)\b', text):
        template = 'where_is'
    elif re.search(r'\b(find|show|list|when|did|was|is there|are there|distance|gap|closest|minimum|how close|how far|how many|busiest|peak|most crowded|nearest|close to|next to|speed|speeding|dodge|dodges|dodged|dodging|yield|yielding|give way|step back)\b', text):
        template = 'find'
    else:
        template = 'unsupported'
    entities = re.findall(r'\b[PFV]\d+\b', q.upper())
    if not entities:
        for word, pattern in [('person', r'worker|pedestrian|person|people|anyone|someone'),
                              ('forklift', r'forklift'), ('vehicle', r'car|vehicle')]:
            if re.search(pattern, text) and not (word == 'vehicle' and re.search(r'forklift', text)):
                entities.append(word)
    place = ctx.get('place')
    # Capitalised streets or an explicit junction avoid prose joins and units such as km/h.
    street = r"\b(?!(?:At|Near)\b)[A-Z][A-Za-z.']*(?:\s+[A-Z][A-Za-z.']*)*"
    word = r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z.']*(?:\s+[A-Za-z][A-Za-z.']*)*(?![A-Za-z0-9_])"
    junction = (re.search(rf'({street})\s*(?:&|/|\band\b)\s*({street})', q)
                or re.search(rf'({word})\s*(?:&|\band\b)\s*({word})\s+(?:junction|intersection)\b', q))
    # only a capitalised name is a place: "near a moving vehicle" is a distance, not a place
    named = re.search(r'(?i:near)\s+([A-Z][^?,]*)', q)
    if junction:
        place = junction[1]+' & '+junction[2]
    elif named:
        place = named.group(1)
    t_range = ctx.get('t_range')
    point = None
    during_span = re.search(r'\bduring\s+(?:(?:these|the)\s+)?(\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b)',text)
    interval = (re.search(r'(?:from)?\s*(\d+(?:\.\d+)?)\s*(?:to|[-–])\s*(\d+(?:\.\d+)?)\s*(?:s\b|seconds)', text)
                or re.search(r'\bbetween\s+(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b)',text))
    if interval:
        t_range = (int(float(interval[1])*1000), int(float(interval[2])*1000))
    elif during_span:
        t_range = (0,int(float(during_span[1])*1000))
    else:
        point = re.search(r'\bat\s+(?:(\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b)|second\s+(\d+(?:\.\d+)?))',text)
        if point:
            t0 = int(float(point[1] or point[2])*1000)
            t_range = (t0,t0+1000)
    # Keep conditions that the deterministic tools cannot verify as unknown predicates.
    constraints = []
    negated = re.search(r"\b(?:not|never|no|none|nobody|no one|without)\b|n['’]t\b", text)
    if negated and not re.search(r"can['’]?t see|cannot see|blind", text):
        constraints.append('negation')
    for attribute in re.findall(r'\b(red|blue|green|yellow|white|black|orange|hardhat|helmet|vest)\b', text):
        constraints.append('attribute:' + attribute)
    if re.search(r'\bwearing\b',text):
        constraints.append('visual_attribute')
    bound_pattern = r'\b(before|after)\s+(?:(\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b)|second\s+(\d+(?:\.\d+)?))'
    bounds = list(re.finditer(bound_pattern,text))
    temporal = re.findall(r'\b(?:after|before|until|since|unless)\b',text)
    if (len(temporal)>len(bounds)
            or re.search(r'\b\d{1,2}:\d{2}(?::\d{2})?\b',text)
            or re.search(r'\b(?:first|last)\s+\d+(?:\.\d+)?\s*(?:s\b|seconds?\b)',text)
            or re.search(r'\bduring\b',text) and not interval and not point and not during_span):
        constraints.append('temporal_relation')
    quantity = r'(?:two|three|four|five|six|seven|eight|nine|ten|\d+)'
    if re.search(r'\b'+quantity+r'\s+(?:people|workers|persons|pedestrians|vehicles|cars|forklifts)\b',text):
        constraints.append('entity_count')
    if re.search(r'\b(?:twice|thrice|again|repeatedly|'+quantity+r'\s+times)\b',text):
        constraints.append('occurrence_count')
    if re.search(r'\bfor\s+(?:one|'+quantity+r'|\d+(?:\.\d+)?)\s*(?:s\b|seconds?\b|minutes?\b)',text):
        constraints.append('duration')
    action_families = [bool(re.search(pattern,text)) for pattern in (r'\b(dodge\w*|yield\w*|give way|step back)\b',r'\bcross\w*\b',r'\bbrak\w*\b')]
    if sum(action_families)>1:
        constraints.append('multiple_requested_actions')
    subjects = r'(?:people|workers?|persons?|pedestrians?|vehicles?|cars?|forklifts?|clips?|videos?|recordings?)'
    universal = (r'\b(?:(?:all|both)(?:\s+of)?(?:\s+(?:the|these|those))?|'
                 r'(?:every|each)(?:\s+(?:one|single))?(?:\s+of(?:\s+(?:the|these|those))?)?)\s+'+subjects+r'\b')
    quantifier = ('all' if re.search(r'\b(?:everyone|everybody)\b|'+universal,text)
                  else 'none' if re.search(r'\b(?:(?:no|neither)\s+'+subjects+r'|none|nobody|no one)\b',text) else 'any')
    if quantifier != 'any':
        constraints.append('exhaustive_coverage')
    for modifier in re.findall(r'\b(moving|running|riding|carrying)\b',text):
        constraints.append('action_modifier:'+modifier)
    if re.search(r'\bhow many\s+(?:people|persons?|workers?|pedestrians?|vehicles?|cars?|forklifts?)\b', text):
        constraints.append('tracked_count')
        if re.search(r'\bhow many\s+(?:vehicles?|cars?|forklifts?)\b',text):
            constraints.append('unsupported_count_subject')
    if re.search(r'\b(busiest|peak|most crowded)\b', text):
        constraints.append('peak_count')
        if any(e in {'vehicle','forklift'} for e in entities):
            constraints.append('unsupported_count_subject')
    if not interval and not point:
        bound = bounds[0] if bounds else None
        if bound:
            ms = int(float(bound[2] or bound[3]) * 1000)
            t_range = (0, ms) if bound[1] == 'before' else (ms + 1, 2**53 - 1)
    key = ctx.get('scene_key') or (scene_key_of(ctx['scene_id']) if ctx.get('scene_id') else None)
    sources = ctx.get('sources') or None
    # entity ids, a time and a selection are local to one scene, and then the sources are ignored
    corpus = ((ctx.get('scope') == 'corpus' or bool(sources)) and not re.search(r'\b[PFV]\d+\b', q.upper())
              and ctx.get('t_ms') is None and not ctx.get('selection'))
    return Query(template=template, entities=list(dict.fromkeys(entities)), t_range=t_range, place=place,
                 zone=ctx.get('zone'), scene_key=key, workspace=ctx['workspace'],
                 abstain_reason='unsupported_question' if template == 'unsupported' else None,
                 scope='corpus' if corpus else 'scene', sources=sources if corpus else None,
                 constraints=list(dict.fromkeys(constraints)), quantifier=quantifier)


def complete_json(messages: list[dict], *, role: str, timeout: float = 5) -> dict:
    from app.core.deadline import remaining
    url = os.environ['LLM_URL'].rstrip('/')
    if not url.endswith('/v1'):
        url += '/v1'
    model = os.environ.get('LLM_MODEL_SMALL' if role == 'parse' else 'LLM_MODEL_LARGE', '')
    headers = {'Authorization': 'Bearer ' + os.environ['LLM_API_KEY']} if os.environ.get('LLM_API_KEY') else {}
    with httpx.Client(timeout=remaining(timeout)) as client:
        response = client.post(url+'/chat/completions', headers=headers,
                               json={'model': model, 'messages': messages, 'temperature': 0,
                                     'response_format': {'type': 'json_object'}})
        response.raise_for_status()
    return json.loads(response.json()['choices'][0]['message']['content'])


def needs_llm(q: str, rule: Query) -> bool:
    return rule.template == 'unsupported' and not re.search(FORBIDDEN, q.lower()) and bool(os.environ.get('LLM_URL'))


def parse(q: str, ctx: dict) -> Query:
    rule = rule_parse(q, ctx)
    if not needs_llm(q, rule):
        return rule
    try:
        result = complete_json([{'role': 'system', 'content': 'Classify the question as one of find, where_is, what_happened, link or unsupported '
                                 'and extract entities. Identity, intent and responsibility questions are unsupported. '
                                 'Return JSON {"template": string, "entities": [strings]}.'},
                                {'role': 'user', 'content': q}], role='parse')
        template = result['template']
        return Query.model_validate({**rule.model_dump(), 'template': template, 'entities': result['entities'],
                                     'abstain_reason': rule.abstain_reason if template == 'unsupported' else None})
    except (httpx.HTTPError, ValueError, KeyError, TypeError, ValidationError):
        return rule


def sanitize_numbers(text: str, values: list) -> str:
    # Numbers from model prose never gain a measurement reference by coinciding with one.
    return re.sub(r'\d+(?:\.\d+)?', '[uncited number]', text)
