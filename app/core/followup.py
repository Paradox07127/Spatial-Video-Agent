"""Plain explanations and answerable follow-ups for a question no investigation template measures."""
from __future__ import annotations

import math
import re

from app.core import llm

ACTIVITY = {'doing','do','activity','action','behavior','behaviour','working','busy'}
GAZE = {'looking','watching','staring','notice','noticed','saw','see','seen'}
RANKING = {'best','better','worst','most','least'}
REFERENCE = {'moment','then','that','it','this','time'}
CAMERA = {'camera','cameras'}
QUESTION = {'which','what','who','how','when','where','is','was','did'}
ACTIVITY_TEXT = 'What a person is doing is not measured; below are the measured distance, speed, camera views and recorded events at that moment.'
GAZE_TEXT = 'Where a person is looking is not measured.'
RANKING_TEXT = 'There is no measured view-quality score; below are the cameras that detected the person.'
RESOLVED_TEXT = '“That moment” refers to the time and objects selected on the timeline.'
UNRESOLVED_TEXT = 'The earlier moment this question refers to could not be identified.'
EVENT_TEXT = 'The event detector records only dodge and near-miss events, so the event this question describes cannot be checked directly.'
ENGLISH_TEXT = ('This question did not match a supported question form. Supported questions are in English and ask about '
                'distance, speed, position, camera visibility, blind spots or recorded events.')


def unmet_parts(q, parsed, focus_resolved: bool) -> list[str]:
    """One sentence per part of the question that no tool measures."""
    words = [w.lower() for w in re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?",q)]
    unconsumed = [c.split(':',1)[1] for c in parsed.constraints if c.startswith('unconsumed:')]
    named = [c.split(':',1)[1] for c in parsed.constraints if c.startswith('attribute:')]
    camera = bool(CAMERA & set(words))
    parts = []
    if any(not ch.isascii() and ch.isalpha() for ch in q) and len(words)<3:
        parts.append(ENGLISH_TEXT)
    if ACTIVITY & set(words):
        parts.append(ACTIVITY_TEXT)
    if GAZE & set(words) and not camera:
        parts.append(GAZE_TEXT)
    if RANKING & {w.lower() for w in unconsumed} or camera and {'see','saw','seen'} & set(words):
        parts.append(RANKING_TEXT)
    if REFERENCE & set(words):
        parts.append(RESOLVED_TEXT if focus_resolved else UNRESOLVED_TEXT)
    if {'event','passing'} & set(parsed.constraints):
        parts.append(EVENT_TEXT)
    covered = ACTIVITY|GAZE|RANKING|REFERENCE|CAMERA|QUESTION
    parts += ['“'+w+'” is not something the tools measure.' for w in unconsumed+named
              if re.fullmatch(r"[A-Za-z]+(?:['’][A-Za-z]+)?",w) and w.lower() not in covered]
    return list(dict.fromkeys(parts))


def suggestions(workspace, focus) -> list[str]:
    """At most three questions about the focus that a template answers without unconsumed words."""
    if workspace!='warehouse' or not focus:
        return []
    t = focus['t_ms']/1000
    candidates = ['How close did the forklift come to a person between '+str(max(0,math.floor(t-2)))+' and '+str(math.ceil(t+2))+' seconds?']
    if focus.get('forklift'):
        candidates.append('How fast was '+focus['forklift']+' at '+format(t,'.1f')+' seconds?')
    if focus.get('person'):
        candidates.append('Where was '+focus['person']+' at '+format(t,'.1f')+' seconds?')
    ctx = {'workspace':workspace,'scene_key':focus.get('scene_key')}
    parsed = [(q,llm.rule_parse(q,ctx)) for q in candidates]
    return [q for q, p in parsed if p.template!='unsupported' and not any(c.startswith('unconsumed:') for c in p.constraints)][:3]
