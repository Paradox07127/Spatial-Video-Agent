"""A lossless requirement ledger: unknown words never silently disappear."""
from __future__ import annotations
import re
from dataclasses import dataclass, asdict
MAX_TIME_MS = 2**53-1
N = r'\d+(?:\.\d+)?'
UNIT = r'(?:s\b|seconds?\b)'
TIME = rf'(?:(?P<number>{N})\s*{UNIT}|second\s+(?P<ordinal>{N}))'
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
find show list clips clip video videos recordings recording where when what happened happening
how far close closest minimum nearest distance gap meters meter m within under over
less greater more than at least most to from of in on with and or as then
worker workers person persons people pedestrian pedestrians anyone someone bicycle warehouse fleet camera scene
forklift forklifts vehicle vehicles car cars p f v ego recording
dodge dodges dodged dodging yield yields yielded yielding give way step back
cross crosses crossed crossing brake brakes braked braking
pass passes passed passing past walk walks walked walking
near miss beside adjacent next center centre root ground point position coordinates
speed speeds speeding fast moving running riding carrying
blind spot spots see cannot during second seconds s
before after between summary describe explain summarise summarize
count busiest peak crowded many tracked track tracks simultaneously
building buildings right left side junction intersection location road street
red blue green yellow white black orange hardhat helmet vest wearing
not never no none nobody without all both each every everyone everybody
one two three four five six seven eight nine ten twice thrice again repeatedly
first last until since unless for start starts started get got gets getting
come came comes coming same link related prior entered enter observed sampled front
'''.split())
def build_task_spec(q):
    ranges,time_spans=time_requirements(q)
    requirements=[Requirement('time',q[a:b],a,b) for a,b in time_spans]
    masked=list(q)
    for a,b in time_spans: masked[a:b]=' '*(b-a)
    for m in re.finditer(r'\b(?:[PFV]\d+|[a-z]+_\d+(?::\d+)?|(?:wh|fl|st)-[a-z0-9_:-]+)\b',q,re.I):
        masked[m.start():m.end()]=' '*(m.end()-m.start())
    for m in re.finditer(r'\b(?i:near|at)\s+([A-Z][A-Za-z.\' ]*(?:&\s*[A-Z][A-Za-z.\' ]*)?)(?=[?,.]|$)',q):
        a,b=m.span(1); masked[a:b]=' '*(b-a)
    for m in re.finditer(r"[A-Za-z]+(?:['’][A-Za-z]+)?",''.join(masked)):
        if m[0].lower() not in GRAMMAR:
            requirements.append(Requirement('unconsumed',q[m.start():m.end()],m.start(),m.end()))
    for label,pattern in [('dodge',r'\bdodg\w*\b|\byield\w*\b|\bgive way\b|\bstep back\b'),('crossing',r'\bcross\w*\b'),('braking',r'\bbrak\w*\b')]:
        for m in re.finditer(pattern,q,re.I): requirements.append(Requirement('action:'+label,q[m.start():m.end()],m.start(),m.end()))
    return TaskSpec(q,tuple(requirements),tuple(ranges),tuple(time_spans))
