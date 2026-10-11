"""Resolve explicit follow-up constraints without widening the user's evidence universe."""
from __future__ import annotations

import re
from app.core.task_spec import time_requirements
from dataclasses import dataclass

ENTITY = re.compile(r'^[PFV]\d+$')
MAX_TIME_MS = 2**53


@dataclass(frozen=True)
class Constraints:
    t_range: tuple[int, int] | None
    entities: list[str]
    zone: list | None
    reason: str | None = None


def resolve_constraints(q, context, parsed):
    ranges = [r for r in (parsed.t_range, context.get('t_range')) if r is not None]
    # A paused-frame follow-up addresses this instant, not an arbitrary nearby event.
    if context.get('t_ms') is not None:
        t = context['t_ms']
        ranges.append((t,t+1))
    ranges.extend(time_requirements(q)[0])
    if ranges:
        interval = (max(r[0] for r in ranges),min(r[1] for r in ranges))
        if interval[0]>=interval[1]:
            return Constraints(interval,parsed.entities,parsed.zone,'conflicting_time_constraints')
    else:
        interval = None
    selection = list(dict.fromkeys(context.get('selection') or []))
    if any(not ENTITY.fullmatch(e) for e in selection):
        return Constraints(interval,parsed.entities,parsed.zone,'unrecognized_selection')
    explicit = [e for e in parsed.entities if ENTITY.fullmatch(e)]
    for prefix in 'PFV':
        selected = {e for e in selection if e.startswith(prefix)}
        named = {e for e in explicit if e.startswith(prefix)}
        if selected and named and selected!=named:
            return Constraints(interval,parsed.entities,parsed.zone,'conflicting_entity_constraints')
    entities = list(dict.fromkeys([*explicit,*selection]))
    kind_prefix = {'person':'P','forklift':'F','vehicle':'V'}
    entities += [e for e in parsed.entities if not ENTITY.fullmatch(e) and
                 not any(v.startswith(kind_prefix.get(e,'!')) for v in entities)]
    if selection and not parsed.scene_key:
        return Constraints(interval,entities,parsed.zone,'selection_requires_scene')
    zone = context.get('zone') or parsed.zone
    if context.get('zone') and parsed.zone and context['zone']!=parsed.zone:
        return Constraints(interval,entities,zone,'conflicting_zone_constraints')
    return Constraints(interval,entities,zone)


def apply_constraints(q, context, parsed):
    constraints = resolve_constraints(q,context,parsed)
    return parsed.model_copy(update={'t_range':constraints.t_range,'entities':constraints.entities,
                                     'zone':constraints.zone}), constraints.reason
