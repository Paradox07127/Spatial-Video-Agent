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
    assert actor_points(store,'wh-sdg-fixture-01',4500,['P1','F1'])==[]

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
