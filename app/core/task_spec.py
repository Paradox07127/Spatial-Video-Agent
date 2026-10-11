"""A lossless requirement ledger: unknown words never silently disappear."""
from __future__ import annotations
import re
from dataclasses import dataclass, asdict
MAX_TIME_MS = 2**53-1
N = r'\d+(?:\.\d+)?'
UNIT = r'(?:s\b|seconds?\b)'
TIME = rf'(?:(?P<number>{N})\s*{UNIT}|second\s+(?P<ordinal>{N}))'
PLACE_NAME = r"[A-Z][A-Za-z.']*\b(?:(?:\s+|\s*(?:&|/|\band\b)\s*)[A-Z][A-Za-z.']*\b)*"
@dataclass(frozen=True)
class Requirement:
    kind: str
    span: str
    start: int
    end: int
@dataclass(frozen=True)
class TaskSpec:
    question: str
    requirements: tuple[Requirement,...]
    time_ranges: tuple[tuple[int,int],...]
    consumed_time_spans: tuple[tuple[int,int],...]
    def as_dict(self):
        return asdict(self)
def time_requirements(q):
    ranges,spans=[],[]
    patterns=[(rf'\b(?:from\s+)?({N})\s*(?:{UNIT})?\s*(?:to|[-–])\s*({N})\s*{UNIT}','range'),
              (rf'\bbetween\s+({N})\s*(?:{UNIT})?\s+and\s+({N})\s*{UNIT}','range'),
              (rf'\bduring\s+(?:(?:these|the)\s+)?({N})\s*{UNIT}','during'),
              (rf'\bwithin\s+({N})\s*{UNIT}','during'),
              (rf'\bin\s+(?:these|the)\s+({N})\s*{UNIT}','during'),
              (rf'\b(at|before|after)\s+{TIME}','bound')]
    for pattern,kind in patterns:
        for m in re.finditer(pattern,q,re.I):
            if any(a<=m.start()<b for a,b in spans): continue
            if kind=='range': interval=(int(float(m[1])*1000),int(float(m[2])*1000))
            elif kind=='during': interval=(0,int(float(m[1])*1000))
            else:
                t=int(float(m['number'] or m['ordinal'])*1000)
                interval=(t,t+1) if m[1].lower()=='at' else (0,t) if m[1].lower()=='before' else (t+1,MAX_TIME_MS)
            ranges.append(interval); spans.append(m.span())
    return ranges,spans
# Explicitly consumed grammar/capability vocabulary. New descriptors stay unknown.
GRAMMAR=set('''a an the this that these those there it its they them their he she his her
is are was were be been being did do does have has had can could would will
find show me any list clips clip video videos recordings recording where when what happened happening
how far close closer closest minimum nearest distance gap meters meter m
less greater more than at least most to from of in on with and or as then
worker workers person persons people pedestrian pedestrians anyone someone bicycle warehouse fleet camera scene
forklift forklifts vehicle vehicles car cars p f v ego recording
dodge dodges dodged dodging yield yields yielded yielding give way step back
cross crosses crossed crossing brake brakes braked braking
pass passes passed passing past walk walks walked walking
near miss beside adjacent next center centre root ground point position coordinates
speed speeds speeding fast moving running riding carrying
blind spot spots see cannot
summary describe explain summarise summarize
count busiest peak crowded many tracked track tracks simultaneously
building buildings junction intersection location road street
red blue green yellow white black orange hardhat helmet vest wearing
not never no none nobody without all both each every everyone everybody
one two three four five six seven eight nine ten twice thrice again repeatedly
for start starts started get got gets getting
come came comes coming same link related prior entered enter observed sampled front
'''.split())
# "or" between two times, two entity ids or two place names cannot be one hard constraint; "a truck or van" can
_ALTERNATIVE = rf'(?:(?:at|before|after|around)\s+)?(?:{N}\s*{UNIT}|second\s+{N})|\b[PFV]\d+\b|{PLACE_NAME}'
DISJUNCTION = re.compile(rf'(?:{_ALTERNATIVE})\s*,?\s+or\s+(?:{_ALTERNATIVE})')
def has_disjunction(q):
    return bool(DISJUNCTION.search(q))
def build_task_spec(q):
    ranges,time_spans=time_requirements(q)
    requirements=[Requirement('time',q[a:b],a,b) for a,b in time_spans]
    for m in DISJUNCTION.finditer(q):
        requirements.append(Requirement('disjunction',m[0],m.start(),m.end()))
    masked=list(q)
    for a,b in time_spans: masked[a:b]=' '*(b-a)
    for m in re.finditer(r'\b(?:[PFV]\d+|[a-z]+_\d+(?::\d+)?|(?:wh|fl|st)-[a-z0-9_:-]+)\b',q,re.I):
        masked[m.start():m.end()]=' '*(m.end()-m.start())
    for m in re.finditer(rf'\b(?i:near|at)\s+(?:(?i:the)\s+)?({PLACE_NAME})',q):
        a,b=m.span(1); masked[a:b]=' '*(b-a)
    patterns=[r'^\s*please\s+(?:show|find|list)\b',
              r'\bhow\s+far\s+(?:away|apart)\s+(?:is|are|was|were)\b',
              rf'\b(?:(?:within|under|over|(?:less|greater|more|farther|closer)\s+than|at\s+(?:least|most))\s+)?{N}\s*(?:meters?\b|m\b)',
              r'\b(?:distance|gap)\s+between\b',
              # "between <cam or clip id> and <id>": the ids are already masked
              r'\bbetween\s+(?:(?:clips?|cameras?|views?)\s+)?and\b',
              r'\bbuildings?\s+on\s+(?:the\s+)?(?:right|left)(?:\s+side)?\b']
    for pattern in patterns:
        for m in re.finditer(pattern,''.join(masked),re.I):
            masked[m.start():m.end()]=' '*(m.end()-m.start())
    for m in re.finditer(rf"[A-Za-z]+(?:['’][A-Za-z]+)?|{N}",''.join(masked)):
        if m[0].lower() not in GRAMMAR:
            requirements.append(Requirement('unconsumed',q[m.start():m.end()],m.start(),m.end()))
    for label,pattern in [('dodge',r'\bdodg\w*\b|\byield\w*\b|\bgive way\b|\bstep back\b'),('crossing',r'\bcross\w*\b'),('braking',r'\bbrak\w*\b')]:
        for m in re.finditer(pattern,q,re.I): requirements.append(Requirement('action:'+label,q[m.start():m.end()],m.start(),m.end()))
    return TaskSpec(q,tuple(requirements),tuple(ranges),tuple(time_spans))


def consume_verified_place_prefix(q, place, constraints):
    """Consume a leading `place:` span only after the caller geocoded it."""
    prefix=re.match(r'\s*([^:?,]+)\s*:',q)
    if not prefix:
        return constraints
    def normalized(value):
        value=re.sub(r'\s+(?:junction|intersection)\s*$','',value.strip(),flags=re.I)
        return re.sub(r'\s*(?:&|/|\band\b)\s*',' & ',value,flags=re.I).casefold()
    if normalized(prefix[1])!=normalized(place):
        return constraints
    requirements=[r for r in build_task_spec(q).requirements if r.kind=='unconsumed']
    consumed={r.span.casefold() for r in requirements if r.end<=prefix.end()}
    elsewhere={r.span.casefold() for r in requirements if r.end>prefix.end()}
    return [c for c in constraints if not (c.startswith('unconsumed:') and
            c.split(':',1)[1].casefold() in consumed-elsewhere)]
