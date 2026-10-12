"""Open-vocabulary conditions and every hard bound survive the deterministic parser."""
import copy
import pytest
from app.core import llm, tools
from app.core.task_spec import build_task_spec
from app.core.investigation_context import apply_constraints
from app.api.scenes import actor_points
from app.tests.unit.test_semantic_repairs import store,ask,final

@pytest.mark.parametrize('q,expected',[
    ('How far is P1 from F1 at 2 seconds?',(2000,2001)),
    ('How far is P1 from F1 after second 2 before second 1?',(2001,1000)),
    ('How far is P1 from F1 at 2 seconds and at second 3?',(3000,2001)),
    ('How far is P1 from F1 from 1 to 8 seconds between 3 and 5 seconds after second 4?',(4001,5000)),
    ('How far is P1 from F1 from 1 seconds to 8 seconds before second 4?',(1000,4000)),
])
def test_every_text_time_requirement_is_intersected(q,expected):
    ctx={'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'}
    parsed,reason=apply_constraints(q,ctx,llm.rule_parse(q,ctx))
    assert parsed.t_range==expected
    assert reason==('conflicting_time_constraints' if expected[0]>=expected[1] else None)
    spec=build_task_spec(q)
    assert all(q[r.start:r.end]==r.span for r in spec.requirements)

@pytest.mark.parametrize('descriptor',['purple','stationary','seated','magenta','injured','distracted','zigzagging','unusual-patterned'])
def test_unknown_modifiers_are_preserved_without_a_blacklist(descriptor):
    q=f'How far is the {descriptor} worker from F1?'
    spec=build_task_spec(q)
    assert any(r.kind=='unconsumed' and r.span.lower() in descriptor for r in spec.requirements)
    assert any(c.startswith('unconsumed:') for c in llm.rule_parse(q,{'workspace':'warehouse'}).constraints)

@pytest.mark.parametrize('q',[
    'How far is the purple P1 from F1?',
    'How far is P1 from F1 while crossing?',
    'How far is P1 from F1 while braking?',
    'Is P2 in the ceiling_00 blind spot while dodging the forklift?',
])
def test_fast_paths_cannot_consume_unmeasured_conditions(store,q):
    frames=ask(store,q)
    assert final(frames)=='UNRESOLVED'
    assert any(p['status']=='unknown' for f in frames if f['event']=='claim' for p in f['data']['predicates'])


def test_point_distance_uses_the_requested_sample_not_future_minimum(store):
    frames=ask(store,'How far is P1 from F1 at 2 seconds?')
    assert final(frames)=='SUPPORTED'
    measurements=[e for f in frames if f['event']=='tool_result' for e in f['data']['evidence'] if e['method']=='ground_distance']
    assert len(measurements)==1 and measurements[0]['t0_ms']==2000
    calls=[f['data'] for f in frames if f['event']=='tool_call']
    assert next(c for c in calls if c['name']=='closest_approach')['args']['t_range']==(2000,2001)


def test_actor_zone_points_reject_stale_samples(store,monkeypatch):
    original=store.asset_json
    def sparse(key,path):
        doc=copy.deepcopy(original(key,path))
        if path=='tracks.json':
            for tr in doc['tracks']: tr['pts']=[tr['pts'][0],tr['pts'][-1]]
        if path=='forklift.json':
            for tr in doc['forklifts']: tr['track']=[tr['track'][0],tr['track'][-1]]
        return doc
    monkeypatch.setattr(store,'asset_json',sparse)
    assert actor_points(store,'wh-sdg-fixture-01',4500,['P1','F1'])=={}

@pytest.mark.parametrize('method,residual',[('unknown',0),('known',10000),('known',float('nan'))])
def test_synchronized_evidence_rejects_bad_sync(store,monkeypatch,method,residual):
    original=store.manifest
    def bad(key):
        m=copy.deepcopy(original(key))
        for cam in m['cams']: cam.update(sync_method=method,sync_residual_ms=residual)
        return m
    monkeypatch.setattr(store,'manifest',bad)
    result=tools.get_synced_views(store,'wh-sdg-fixture-01',4600)
    assert result.envelope.coverage==0 and result.envelope.pts_read==0
    assert result.envelope.output['cams']==[] and all(g[2]=='unsynchronized' for g in result.envelope.gaps)


def test_corpus_anchor_consumes_only_its_actual_source_universe_grammar():
    from app.core.llm import rule_parse
    anchor='Show me every clip, from any camera in any site, where a person is close to a moving vehicle'
    parsed=rule_parse(anchor,{'workspace':'warehouse','scope':'corpus'})
    assert parsed.scope=='corpus'
    assert not [c for c in parsed.constraints if c.startswith('unconsumed:')]
    assert 'exhaustive_coverage' in parsed.constraints and 'action_modifier:moving' in parsed.constraints


@pytest.mark.parametrize('q,scope,expected',[
    ('Show me any person in any site','scene',True),
    ('Show me any person in any site','corpus',False),
    ('Show me any person in every site','corpus',True),
    ('Show me any person in any site and another site','corpus',True),
])
def test_source_universe_grammar_never_widens_a_scene_scope(q,scope,expected):
    from app.core.llm import rule_parse
    parsed=rule_parse(q,{'workspace':'warehouse','scope':scope})
    assert ('unconsumed:site' in parsed.constraints)==expected
    assert not {'unconsumed:me','unconsumed:any'}&set(parsed.constraints)


def test_verified_place_prefix_does_not_consume_matching_words_elsewhere():
    from app.core.task_spec import consume_verified_place_prefix
    assert consume_verified_place_prefix('ROM: did any pedestrian cross?','ROM',['unconsumed:ROM','requested_action:crossing'])==['requested_action:crossing']
    assert consume_verified_place_prefix('ROM: describe the ROM sign','ROM',['unconsumed:ROM','unconsumed:sign'])==['unconsumed:ROM','unconsumed:sign']
    assert consume_verified_place_prefix('Unknown: did any pedestrian cross?','ROM',['unconsumed:Unknown'])==['unconsumed:Unknown']


@pytest.mark.parametrize('place',['Queen and Spadina','Union Station'])
def test_place_span_does_not_consume_following_conditions(store,place):
    q=f'Did P1 cross near {place} with a seated pedestrian?'
    spec=build_task_spec(q)
    assert [r.span for r in spec.requirements if r.kind=='unconsumed']==['seated']
    parsed=llm.rule_parse(q,{'workspace':'fleet'})
    assert parsed.place==place.replace(' and ',' & ')
    if place=='Queen and Spadina':
        frames=ask(store,q,'fleet',scene_key='fl-pie-fixture-01')
        assert final(frames)=='UNRESOLVED'
        assert any(p['operator']=='unconsumed:seated' and p['status']=='unknown'
                   for f in frames if f['event']=='claim' for p in f['data']['predicates'])


def test_or_between_nouns_is_not_a_disjunction():
    for q in ['Find a truck or trailer backed up to a loading dock', 'Show a person or cyclist crossing',
              'Is the worker walking or standing at 3 s?']:
        assert not [r for r in build_task_spec(q).requirements if r.kind == 'disjunction' or r.span.lower() == 'or']
    assert [r.kind for r in build_task_spec('Find P1 or P2').requirements].count('disjunction') == 1


def test_link_between_ids_and_in_these_seconds_are_consumed():
    assert not [r for r in build_task_spec('Link P1 between ceiling_04 and eye_00').requirements if r.kind == 'unconsumed']
    rs = build_task_spec('Was anyone where ceiling_00 cannot see in these 10 seconds?').requirements
    assert [(r.kind, r.span) for r in rs if r.kind in ('time', 'unconsumed')] == [('time', 'in these 10 seconds')]


def test_ever_is_consumed_only_for_a_decided_comparison(store):
    # The same quantifier stays unknown for an event or a scalar measurement.
    for q in ['Did P1 ever dodge F1?', 'How far was P1 ever from F1?']:
        assert 'unconsumed:ever' in llm.rule_parse(q,{'workspace':'warehouse'}).constraints
        frames=ask(store,q)
        assert final(frames)=='UNRESOLVED'
        assert any(p['operator']=='unconsumed:ever' for f in frames if f['event']=='claim' for p in f['data']['predicates'])
    frames=ask(store,'Was P1 ever more than 3 m from F1?')
    assert final(frames)=='SUPPORTED'
    assert all(p['operator']!='unconsumed:ever' for f in frames if f['event']=='claim' for p in f['data']['predicates'])


@pytest.mark.parametrize('q,workspace,expected',[
    ('Was P1 ever more than 10 m from the recording car between 3 and 8 s?','fleet','SUPPORTED'),
    ('Was P1 at some point farther than 10 m from the recording car between 3 and 8 s?','fleet','SUPPORTED'),
    # fleet samples exist only while P1 is seen, so they can refute but not establish an always-comparison
    ('Was P1 always more than 3 m from the recording car between 3 and 8 s?','fleet','UNRESOLVED'),
    ('Was P1 always less than 10 m from the recording car between 3 and 8 s?','fleet','CONTRADICTED'),
    ('Was P1 always more than 3 m from F1?','warehouse','CONTRADICTED'),
    ('Was P1 always less than 3 m from F1?','warehouse','CONTRADICTED'),
])
def test_comparison_time_quantifiers_choose_the_required_extreme(store, q, workspace, expected):
    frames=ask(store,q,workspace,scene_key='fl-pie-fixture-01' if workspace=='fleet' else 'wh-sdg-fixture-01')
    assert final(frames)==expected
    answer=next(f['data'] for f in frames if f['event']=='answer')
    evidence={e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    measured=evidence[next(r['eid'] for r in answer['measure_refs'] if r['field'] in {'value','value.distance_m'})]
    minimum='always more than' in q
    expected_time=(7900 if minimum else 3000) if workspace=='fleet' else (4900 if minimum else 0)
    assert measured['t0_ms']==expected_time


def test_ever_speed_comparison_is_consumed_after_decision(store, monkeypatch):
    from app.tests.unit.test_agent import set_limits
    set_limits(store,monkeypatch,lambda key,t:5.)
    frames=ask(store,'Was this car ever speeding?','fleet',scene_key='fl-pie-fixture-01',t_range=(0,4000))
    assert final(frames)=='SUPPORTED'
    assert all(p['operator']!='unconsumed:ever' for f in frames if f['event']=='claim' for p in f['data']['predicates'])
