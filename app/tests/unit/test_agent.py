import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agent, search
from app.core import embed, tools, trace, vlm
from app.tests.unit.a5_fakes import FIXTURES, fixture_store, install_trace, install_vlm
from pipeline.common.schema_check import check_sse_sequence, schema_errors

REAL_TRACE = (trace.start_run, trace.traced, trace.finish_run)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    store = fixture_store(tmp_path)
    install_trace(monkeypatch)
    install_vlm(monkeypatch)
    monkeypatch.setattr(embed, 'embed_text', lambda q: [0.0]*256)
    app = FastAPI()
    app.state.store = store
    app.state.settings = SimpleNamespace(index_duckdb=store.index_path)
    app.include_router(agent.router)
    app.include_router(search.router)
    return app, store


def read_frames(response):
    return [dict(event=b.splitlines()[0][7:], data=json.loads(b.splitlines()[1][6:]))
            for b in response.text.strip().split('\n\n')]


def test_fixture_asks_and_readback(setup):
    app, store = setup
    with TestClient(app) as client:
        for ask in json.loads((FIXTURES/'asks.json').read_text())['asks']:
            frames = read_frames(client.post('/api/agent/ask', json={k:ask[k] for k in ['q','context','budget']}))
            assert check_sse_sequence(frames) == []
            assert frames[-2]['data']['final'] == ask['expect']['final'], (ask['ask_id'], frames[-2])
            run_id = frames[0]['data']['run_id']
            saved = client.get('/api/agent/runs/'+run_id).json()
            assert schema_errors(saved, 'agent_run.json') == []
            assert all('input' in s and 'output' in s for s in saved['steps'])
            if ask['ask_id'] == 'wh-main':
                claims = [f['data'] for f in frames if f['event']=='claim']
                assert claims[0]['after']=='UNRESOLVED' and claims[-1]['after']=='SUPPORTED'
                plans = [s for s in saved['steps'] if s['name']=='policy.plan_next']
                assert plans[0]['output']['cam'] == 'ceiling_04'
                assert plans[0]['output']['candidates']
            if ask['expect']['final'] in ['NO_MATCH', 'UNSUPPORTED']:
                assert not saved['hits']
        assert client.get('/api/agent/runs').json()
        review = client.post('/api/agent/confirm', json={'run_id':run_id,'verdict':'confirm','note':'Review completed'}).json()
        assert review['record']['title'] == 'Event Review Record' and '<html' in review['html']


def test_first_step_and_budget(setup):
    _, store = setup
    start = time.monotonic()
    stream = agent.ask_frames(store, agent.AskRequest(q='Find workers', context={'workspace':'warehouse'}, budget={'tools':0,'seconds':25}))
    first = next(stream)
    assert first['event']=='step' and time.monotonic()-start < .3
    frames = [first, *stream]
    assert check_sse_sequence(frames)==[]
    assert frames[-2]['data']['stop_reason']=='budget_exhausted'


def test_vlm_failure_and_numbers(setup, monkeypatch):
    _, store = setup
    def fail(*a, **kw): raise vlm.VLMUnavailable('offline')
    monkeypatch.setattr(vlm, 'situation', fail)
    frames = list(agent.ask_frames(store, agent.AskRequest(q='When did the worker start to dodge the forklift, and how far was it?', context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[]
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert any(f['event']=='tool_result' and f['data']['envelope']['gaps'] for f in frames)
    from app.core.llm import sanitize_numbers
    assert sanitize_numbers('Distance 99 meters', []) == 'Distance [uncited number] meters'


def test_search_route(setup):
    app, _ = setup
    with TestClient(app) as client:
        response = client.post('/api/search', json={'q':'The worker dodges the forklift','workspace':'warehouse'})
        assert response.status_code==200 and schema_errors(response.json(),'search.json')==[]


def test_location_time_and_template_paths(setup):
    app, _ = setup
    with TestClient(app) as client:
        frames = read_frames(client.post('/api/agent/ask',json={'q':'Where is P2?','context':{'workspace':'warehouse','scene_key':'wh-sdg-fixture-01','t_ms':2100}}))
        assert check_sse_sequence(frames)==[]
        hits = [f['data']['moments'] for f in frames if f['event']=='hits']
        assert hits and hits[0][0]['t0_ms']==2100
        assert [a['entity_id'] for a in hits[0][0]['actor_refs']]==['P2']
        frames = read_frames(client.post('/api/agent/ask',json={'q':'Where is P1?','context':{'workspace':'warehouse','scene_key':'wh-sdg-fixture-01','t_ms':20000}}))
        assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='OUT_OF_RANGE'
        assert not any(f['event']=='hits' for f in frames)


def test_llm_numbers_and_ref_fields(setup, monkeypatch):
    from app.core import llm
    _,store=setup
    monkeypatch.setenv('LLM_URL','http://llm/v1')
    assert not hasattr(llm,'narrate')
    frames=list(agent.ask_frames(store,agent.AskRequest(q='The worker dodged the forklift: how far?',context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[]
    answer=next(f['data'] for f in frames if f['event']=='answer')
    assert 'uncited number' not in answer['text'] and '{{' not in answer['text']
    evidence={e.eid:e for e in store.get_run(frames[0]['data']['run_id']).evidence}
    assert answer['measure_refs']
    for ref in answer['measure_refs']:
        assert evidence[ref['eid']].kind=='measure'
        head,*rest=ref['field'].split('.')
        value=getattr(evidence[ref['eid']],head)
        for key in rest:
            value=value[key]
        assert value is not None


PRESETS = [('warehouse','When did the worker start to dodge the forklift, and how far was it?'),('warehouse','Is there anyone in the ceiling_00 blind spot during these 10 seconds?'),
           ('warehouse','Who is the person in eye_00?'),('fleet','Near ROM, did any pedestrian cross in front of the car?'),
           ('fleet','At Queen and Spadina, what is the building on the right?'),('fleet','Was this car speeding?')]


def test_llm_is_called_only_to_parse_questions_without_a_template_keyword(setup, monkeypatch):
    import httpx
    from app.core import llm
    _, store = setup
    monkeypatch.setenv('LLM_URL','http://llm/v1')
    calls = []
    def complete(messages, *, role, timeout=5):
        calls.append(role)
        raise httpx.ConnectError('offline')
    monkeypatch.setattr(llm,'complete_json',complete)
    asks = [(q, {'workspace':w,**({'scene_key':'wh-sdg-fixture-01'} if w=='warehouse' else {})}) for w,q in PRESETS]
    asks += [(a['q'],a['context']) for a in json.loads((FIXTURES/'asks.json').read_text())['asks']]
    for q, context in asks:
        frames = list(agent.ask_frames(store, agent.AskRequest(q=q, context=context)))
        assert check_sse_sequence(frames)==[]
        assert calls==[], q
    frames = list(agent.ask_frames(store, agent.AskRequest(q='The worker beside the forklift', context=WH_MAIN['context'])))
    assert calls==['parse'] and check_sse_sequence(frames)==[]


def test_answer_names_subjects_and_event_verb(setup):
    _, store = setup
    answer = next(f['data'] for f in wh_main(store) if f['event']=='answer')
    assert re.fullmatch(r'The worker started to dodge at [\d.]+ s, [\d.]+ m \(±[\d.]+ m\) from the forklift\.', answer['text']), answer['text']


BLIND_Q = 'Is there anyone in the ceiling_00 blind spot during these 10 seconds?'


def test_blind_zone_question_lists_persons_and_intervals(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q=BLIND_Q, context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[]
    assert [f['data']['name'] for f in frames if f['event']=='tool_call']==['blind_presence']
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    p = [f['data'] for f in frames if f['event']=='claim'][-1]['predicates'][0]
    assert (p['operator'],p['status'])==('present_in_blind_zone','supported')
    assert [(read[e]['kind'],read[e]['method'],read[e]['value']['entity_id'],read[e]['t0_ms'],read[e]['t1_ms']) for e in p['eids']]==[
        ('measure','blind_presence','P2',3600,9300),('measure','blind_presence','P1',4000,6000)]
    ui = [f['data'] for f in frames if f['event']=='ui']
    assert (ui[0]['cmd'],ui[0]['args'])==('seek',{'t_ms':3600,'scene_key':'wh-sdg-fixture-01'})
    assert ui[1]['cmd']=='highlight' and sorted(ui[1]['args']['ids'])==['P1','P2']
    assert frames[-2]['data']['final']=='SUPPORTED'
    answer = next(f['data'] for f in frames if f['event']=='answer')
    assert all(w in answer['text'] for w in ['ceiling_00','P1','P2','ceiling_04, eye_00']) and 'uncited number' not in answer['text']
    assert set(p['eids'])<=set(answer['cites']) and len(answer['measure_refs'])==4


def test_blind_zone_question_without_anyone_is_contradicted(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Is there anyone where ceiling_04 cannot see, from 4 to 10 seconds?', context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[]
    p = [f['data'] for f in frames if f['event']=='claim'][-1]['predicates'][0]
    assert (p['operator'],p['status'])==('present_in_blind_zone','contradicted')
    assert frames[-2]['data']['final']=='CONTRADICTED'
    assert 'ceiling_04' in next(f['data']['text'] for f in frames if f['event']=='answer')


def test_blind_zone_with_no_video_is_unresolved_not_supported(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Is there anyone where eye_00 cannot see, from 6 to 7 seconds?', context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[]
    p = [f['data'] for f in frames if f['event']=='claim'][-1]['predicates'][0]
    assert (p['operator'],p['status'],p['eids'])==('present_in_blind_zone','unknown',[])
    assert frames[-2]['data']['final']=='UNRESOLVED'


def two_blind_scenes(store, monkeypatch, other_coverage):
    from app.core import tools
    original_scenes, original_manifest, original_presence = store.list_scenes, store.manifest, tools.blind_presence
    other = 'wh-sdg-fixture-00'
    def list_scenes(workspace=None):
        scenes = original_scenes(workspace)
        return [*({**s,'scene_key':other} for s in scenes if s['scene_key']=='wh-sdg-fixture-01'), *scenes]
    def manifest(key):
        return {**original_manifest('wh-sdg-fixture-01'),'scene_key':other} if key==other else original_manifest(key)
    def blind_presence(store, key, cam, t0, t1):
        if key==other:
            return tools.result(key,t0,t1,{'cam':cam,'intervals':[],'seconds_checked':0},cam=cam,coverage=other_coverage)
        return original_presence(store,key,cam,t0,t1)
    monkeypatch.setattr(store, 'list_scenes', list_scenes)
    monkeypatch.setattr(store, 'manifest', manifest)
    monkeypatch.setattr(tools, 'blind_presence', blind_presence)


@pytest.mark.parametrize('q,other_coverage,final', [
    (BLIND_Q, 1, 'SUPPORTED'), ('Is there anyone where ceiling_04 cannot see, from 4 to 10 seconds?', .5, 'UNRESOLVED')])
def test_blind_zone_across_scenes_one_empty_scene_does_not_veto(setup, monkeypatch, q, other_coverage, final):
    _, store = setup
    two_blind_scenes(store, monkeypatch, other_coverage)
    frames = list(agent.ask_frames(store, agent.AskRequest(q=q, context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[]
    assert len([f for f in frames if f['event']=='tool_call'])==2
    assert frames[-2]['data']['final']==final


def test_blind_zone_skips_a_scene_outside_the_asked_time(setup, monkeypatch):
    _, store = setup
    two_blind_scenes(store, monkeypatch, 1)
    both = store.list_scenes
    # the other scene runs 20-30 s, so the asked 4-10 s does not reach it
    monkeypatch.setattr(store, 'list_scenes', lambda workspace=None: [
        {**s,'t0_ms':20000,'t1_ms':30000} if s['scene_key']=='wh-sdg-fixture-00' else s for s in both(workspace)])
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Is there anyone where ceiling_04 cannot see, from 4 to 10 seconds?', context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[]
    assert [f['data']['args']['scene_key'] for f in frames if f['event']=='tool_call']==['wh-sdg-fixture-01']
    assert frames[-2]['data']['final']=='CONTRADICTED'

def test_blind_zone_without_skeleton_samples_is_unresolved_not_contradicted(setup, monkeypatch):
    from app.tests.unit.test_tools import without_skeleton_after
    _, store = setup
    without_skeleton_after(store, monkeypatch, 7000)
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Is there anyone where ceiling_04 cannot see, from 7 to 10 seconds?', context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[]
    result = next(f['data'] for f in frames if f['event']=='tool_result')
    assert result['envelope']['coverage']<1 and result['envelope']['gaps']
    claim = next(f['data'] for f in frames if f['event']=='claim')
    assert claim['predicates'][0]['status']=='unknown'
    assert frames[-2]['data']['final']=='UNRESOLVED'


@pytest.mark.parametrize('q,final', [(BLIND_Q, 'SUPPORTED'), ('Is there anyone where ceiling_04 cannot see, from 4 to 10 seconds?', 'UNRESOLVED')])
def test_blind_zone_budget_end_keeps_the_scenes_already_checked(setup, monkeypatch, q, final):
    _, store = setup
    two_blind_scenes(store, monkeypatch, 1)
    both = store.list_scenes
    monkeypatch.setattr(store, 'list_scenes', lambda workspace=None: list(reversed(both(workspace))))
    frames = list(agent.ask_frames(store, agent.AskRequest(q=q, context={'workspace':'warehouse'}, budget={'tools':1,'seconds':25})))
    assert check_sse_sequence(frames)==[]
    assert [f['data']['args']['scene_key'] for f in frames if f['event']=='tool_call']==['wh-sdg-fixture-01']
    assert [c['claim_id'] for c in (f['data'] for f in frames if f['event']=='claim')]==['c1']
    assert (frames[-2]['data']['final'],frames[-2]['data']['stop_reason'])==(final,'budget_exhausted')


def test_blind_zone_emits_one_hit_per_interval_before_the_claim(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q=BLIND_Q, context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[]
    events = [f['event'] for f in frames]
    assert events.count('hits')==1 and events.index('hits')<events.index('claim')
    moments = next(f['data']['moments'] for f in frames if f['event']=='hits')
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    key = 'wh-sdg-fixture-01'
    assert [(m['scene_key'],m['t0_ms'],m['t1_ms'],m['cams'],m['primary_cam'],[a['entity_id'] for a in m['actor_refs']],m['why'],m['thumb'])
            for m in moments]==[
        (key,3600,9300,['ceiling_04','eye_00','ceiling_00'],'ceiling_04',['P2'],['blind_presence'],f'/api/assets/{key}/thumbs/ceiling_04/00003.jpg'),
        (key,4000,6000,['ceiling_04','eye_00','ceiling_00'],'ceiling_04',['P1'],['blind_presence'],f'/api/assets/{key}/thumbs/ceiling_04/00004.jpg')]
    assert [[(read[e]['method'],read[e]['value']['entity_id']) for e in m['evidence_ids']] for m in moments]==[
        [('blind_presence','P2')],[('blind_presence','P1')]]
    assert [m.moment_id for m in store.get_run(frames[0]['data']['run_id']).hits]==[m['moment_id'] for m in moments]


def test_auto_trigger_skips_speeding_and_event_only_claims(setup, monkeypatch):
    _, store = setup
    # 3.5 s lies inside the severity-2 ped_crossing event of fl-pie-fixture-01
    set_limits(store, monkeypatch, lambda key,t: 5. if (key,t)==('fl-pie-fixture-01',3500) else None)
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Was this car speeding?', context={'workspace':'fleet'})))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    assert 'actions.trigger' not in [f['data']['name'] for f in frames if f['event']=='tool_call']
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Did any pedestrian cross in front of the car?', context={'workspace':'fleet'})))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    assert 'actions.trigger' not in [f['data']['name'] for f in frames if f['event']=='tool_call']


def test_auto_trigger_skips_a_conflicting_claim(setup, monkeypatch):
    _, store = setup
    original = vlm.situation
    def conflict(*a, **kw):
        rec = original(*a, **kw)
        return rec.model_copy(update={'verification':rec.verification.model_copy(update={'status':'conflict'})})
    monkeypatch.setattr(vlm, 'situation', conflict)
    frames = wh_main(store)
    assert check_sse_sequence(frames)==[]
    assert any(f['event']=='claim' and (f['data'].get('explain') or {}).get('conflict') for f in frames)
    assert 'actions.trigger' not in [f['data']['name'] for f in frames if f['event']=='tool_call']


def test_auto_trigger_failure_keeps_the_decided_claim(setup, monkeypatch, caplog):
    from app.core import tools
    _, store = setup
    def boom(*a, **kw): raise ValueError('event has no participant positions')
    monkeypatch.setattr(tools, 'trigger', boom)
    frames = wh_main(store)
    assert check_sse_sequence(frames)==[]
    assert 'actions.trigger' in [f['data']['name'] for f in frames if f['event']=='tool_call']
    assert (frames[-2]['data']['final'],frames[-2]['data']['stop_reason'])==('SUPPORTED','enough_evidence')
    failed = [s for s in store.get_run(frames[0]['data']['run_id']).steps if s.exec_state=='failed']
    assert [s.note for s in failed]==['ValueError: event has no participant positions']
    assert any(r.levelname=='ERROR' and r.exc_info for r in caplog.records)


def test_tool_exception_is_logged_and_noted(setup, monkeypatch, caplog):
    from app.core import tools
    _, store = setup
    def boom(*a, **kw): raise RuntimeError('views offline')
    monkeypatch.setattr(tools,'get_synced_views',boom)
    frames = wh_main(store)
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['stop_reason']=='error'
    failed = [s for s in store.get_run(frames[0]['data']['run_id']).steps if s.exec_state=='failed']
    assert failed and max(failed,key=lambda s:s.i).note=='RuntimeError: views offline'
    assert any(r.levelname=='ERROR' and r.exc_info for r in caplog.records)


def test_budget_deadline_and_conflict(setup, monkeypatch):
    _,store=setup
    original=tools.search
    def slow(*a,**kw):
        time.sleep(.02)
        return original(*a,**kw)
    monkeypatch.setattr(tools,'search',slow)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Find worker',context={'workspace':'warehouse'},budget={'tools':8,'seconds':.01})))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['stop_reason']=='budget_exhausted'
    monkeypatch.setattr(tools,'search',original)
    original_situation=vlm.situation
    def conflict(*a,**kw):
        rec=original_situation(*a,**kw)
        return rec.model_copy(update={'payload':{**rec.payload,'label':'unrelated'}})
    monkeypatch.setattr(vlm,'situation',conflict)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='The worker dodged the forklift: how far?',context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='UNRESOLVED'
    assert any(f['event']=='claim' and f['data'].get('explain',{}).get('conflict') for f in frames if f['data'].get('explain'))


def test_multiple_moments_each_have_claim(setup):
    _,store=setup
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Find worker',context={'workspace':'warehouse'},budget={'tools':16,'seconds':25})))
    assert check_sse_sequence(frames)==[]
    moments=next(f['data']['moments'] for f in frames if f['event']=='hits')
    run=store.get_run(frames[0]['data']['run_id'])
    assert len(moments)>1 and len(run.claims)==len(moments)


def test_failed_semantic_stop_and_nullable_action(setup,monkeypatch):
    from app.api import actions
    from app.core.models import TriggerResult
    _,store=setup
    def fail(*a,**kw): raise vlm.VLMUnavailable('offline')
    original=vlm.situation
    monkeypatch.setattr(vlm,'situation',fail)
    frames=list(agent.ask_frames(store,agent.AskRequest(q='The worker dodged the forklift: how far?',context={'workspace':'warehouse'})))
    assert frames[-2]['data']['stop_reason']=='no_new_info'
    frames=list(agent.ask_frames(store,agent.AskRequest(q='Find worker',context={'workspace':'warehouse'},budget={'tools':16,'seconds':25})))
    assert frames[-2]['data']['final']=='UNRESOLVED' and frames[-2]['data']['stop_reason']=='no_new_info'
    monkeypatch.setattr(vlm,'situation',original)
    monkeypatch.setattr(actions,'trigger',lambda s,r:TriggerResult(action_id='a_fixture',delivered=False,review_task_id=None,zone_id=None,existing=False))
    frames=list(agent.ask_frames(store,agent.AskRequest(q='The worker dodged the forklift: how far?',context={'workspace':'warehouse'})))
    assert check_sse_sequence(frames)==[]


def test_repeat_ask_replays_cache(setup):
    app, store = setup
    ask = next(a for a in json.loads((FIXTURES/'asks.json').read_text())['asks'] if a['ask_id']=='wh-main')
    body = {k:ask[k] for k in ['q','context','budget']}
    with TestClient(app) as client:
        first = read_frames(client.post('/api/agent/ask', json=body))
        second = read_frames(client.post('/api/agent/ask', json=body))
    assert check_sse_sequence(second) == []
    status = second[-2]['data']
    assert status['cached'] is True and status['final'] == first[-2]['data']['final']
    run_id = second[0]['data']['run_id']
    assert run_id != first[0]['data']['run_id'] and all(f['data']['run_id']==run_id for f in second)
    assert store.get_run(run_id).source_run_id == first[0]['data']['run_id']


def test_cached_status_names_the_source_run_and_its_weave_url(setup):
    app, store = setup
    body = {k:WH_MAIN[k] for k in ['q','context','budget']}
    with TestClient(app) as client:
        first = read_frames(client.post('/api/agent/ask', json=body))
        source = store.get_run(first[0]['data']['run_id'])
        # the outbox flush can deliver the source trace after its status frame was cached
        source.weave_url = 'https://wandb.ai/team/project/weave/calls/source'
        second = read_frames(client.post('/api/agent/ask', json=body))
    status = second[-2]['data']
    assert status['cached'] is True
    assert status['source_run_id'] == source.run_id == store.get_run(second[0]['data']['run_id']).source_run_id
    assert status['weave_url'] == source.weave_url


@pytest.mark.parametrize('body', [
    {'budget':{'tools':17,'seconds':25}}, {'budget':{'tools':8,'seconds':46}}, {'q':'x'*501}])
def test_ask_rejects_requests_over_the_server_caps(setup, body):
    app, _ = setup
    with TestClient(app) as client:
        r = client.post('/api/agent/ask', json={'q':WH_MAIN['q'],'context':WH_MAIN['context'],**body})
    assert r.status_code == 422


def test_ask_accepts_requests_at_the_server_caps(setup):
    agent.AskRequest(q='x'*500, context={'workspace':'warehouse'}, budget={'tools':16,'seconds':45})


def test_run_list_limit_is_capped(setup):
    app, _ = setup
    with TestClient(app) as client:
        assert client.get('/api/agent/runs?limit=201').status_code == 422
        assert client.get('/api/agent/runs?limit=200').status_code == 200


WH_MAIN = next(a for a in json.loads((FIXTURES/'asks.json').read_text())['asks'] if a['ask_id']=='wh-main')


def wh_main(store):
    return list(agent.ask_frames(store, agent.AskRequest(q=WH_MAIN['q'], context=WH_MAIN['context'])))


@pytest.mark.parametrize('limit,status,final', [(40.,'contradicted','CONTRADICTED'),(5.,'supported','SUPPORTED'),(None,'unknown','UNRESOLVED')])
def test_speeding_compares_obd_speed_with_map_limit(setup, monkeypatch, limit, status, final):
    _, store = setup
    original = store.asset_json
    def asset_json(scene_key, relpath):
        doc = original(scene_key, relpath)
        if relpath == 'trip.geojson':
            for f in doc['features'][1:]:
                f['properties']['maxspeed_kmh'] = limit
        return doc
    monkeypatch.setattr(store, 'asset_json', asset_json)
    context = {'workspace':'fleet','scene_key':'fl-pie-fixture-01','t_range':[0,4500]}  # GPS samples cover this range
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Was this car speeding?', context=context)))
    assert check_sse_sequence(frames) == []
    read = {e['eid']:e for f in frames if f['event']=='tool_result' for e in f['data']['evidence']}
    claims = [f['data'] for f in frames if f['event']=='claim']
    assert bool(claims)==(limit is not None)
    for claim in claims:
        p = claim['predicates']
        assert len(p)==1 and (p[0]['operator'],p[0]['value'],p[0]['unit'],p[0]['status'])==('gt',limit,'km/h',status)
        assert sorted(read[e]['kind'] for e in p[0]['eids'])==['map','measure']
    assert frames[-2]['data']['final']==final
    if limit is None:
        assert 'speed limit' in next(f['data']['text'] for f in frames if f['event']=='answer')


def set_limits(store, monkeypatch, limit_at):
    original = store.asset_json
    def asset_json(scene_key, relpath):
        doc = original(scene_key, relpath)
        if relpath == 'trip.geojson':
            for f in doc['features'][1:]:
                f['properties']['maxspeed_kmh'] = limit_at(scene_key, f['properties']['t_ms'])
        return doc
    monkeypatch.setattr(store, 'asset_json', asset_json)


@pytest.mark.parametrize('limit_at,final,text', [
    (lambda key,t: 5. if (key,t)==('fl-pie-fixture-01',9500) else None, 'SUPPORTED', 'map speed limit 5 km/h'),
    (lambda key,t: 40., 'CONTRADICTED', 'not speeding'),
    (lambda key,t: None, 'UNRESOLVED', 'lacks map speed limit')])
def test_speeding_without_trip_ranks_points_by_speed_over_limit(setup, monkeypatch, limit_at, final, text):
    _, store = setup
    set_limits(store, monkeypatch, limit_at)
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Was this car speeding?', context={'workspace':'fleet'})))
    assert check_sse_sequence(frames)==[]
    assert 'search' not in [f['data']['name'] for f in frames if f['event']=='tool_call']
    if final=='SUPPORTED':
        seek = next(f['data']['args'] for f in frames if f['event']=='ui' and f['data']['cmd']=='seek')
        assert seek=={'t_ms':9500,'scene_key':'fl-pie-fixture-01'}
    assert frames[-2]['data']['final']==final
    answer = next(f['data'] for f in frames if f['event']=='answer')
    assert text in answer['text'] and 'uncited number' not in answer['text']
    if final!='UNRESOLVED':
        assert re.search(r'OBD speed [\d.]+ km/h', answer['text']) and len(answer['measure_refs'])==2


def test_event_alone_does_not_support_an_unrelated_question(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q='Find worker', context={'workspace':'warehouse'}, budget={'tools':16,'seconds':25})))
    claims = [f['data'] for f in frames if f['event']=='claim']
    assert claims and all(c['predicates'][0]['status']=='unknown' for c in claims)


def test_ask_passes_budget_deadline_to_vlm(setup, monkeypatch):
    _, store = setup
    seen = []
    original = vlm.situation
    def situation(*a, run=None, **kw):
        seen.append(run.deadline-time.monotonic())
        return original(*a, run=run, **kw)
    monkeypatch.setattr(vlm, 'situation', situation)
    list(agent.ask_frames(store, agent.AskRequest(q='The worker dodged the forklift: how far?', context={'workspace':'warehouse'})))
    assert seen and all(0 < s <= 25 for s in seen)


def test_budget_exhausted_run_is_not_cached(setup):
    app, _ = setup
    with TestClient(app) as client:
        first = read_frames(client.post('/api/agent/ask', json={'q':WH_MAIN['q'],'context':WH_MAIN['context'],'budget':{'tools':1,'seconds':25}}))
        second = read_frames(client.post('/api/agent/ask', json={k:WH_MAIN[k] for k in ['q','context','budget']}))
    assert first[-2]['data']['stop_reason']=='budget_exhausted'
    assert second[-2]['data']['cached'] is False and second[-2]['data']['final']=='SUPPORTED'


def test_measure_uses_the_vehicle_of_the_moment(setup, monkeypatch):
    _, store = setup
    original = store.entities
    def entities(key):
        found = original(key)
        f1 = next((e for e in found if e.entity_id=='F1'), None)
        return [f1.model_copy(update={'entity_id':'F2'}), *found] if f1 else found
    monkeypatch.setattr(store, 'entities', entities)
    frames = wh_main(store)
    measured = [f['data']['args']['b']['entity_id'] for f in frames if f['event']=='tool_call' and f['data']['name']=='measure']
    assert measured and set(measured)=={'F1'}
    assert frames[-2]['data']['final']=='SUPPORTED'


def test_measured_distance_highlights_the_person_and_the_vehicle(setup):
    _, store = setup
    frames = wh_main(store)
    assert check_sse_sequence(frames)==[]
    measured = max(i for i,f in enumerate(frames) if f['event']=='tool_result' and any(e['method']=='ground_distance' for e in f['data']['evidence']))
    ui = [f['data'] for f in frames[measured:] if f['event']=='ui' and f['data']['cmd']=='highlight']
    assert [sorted(u['args']['ids']) for u in ui]==[['F1','P1']]
    assert all(schema_errors(u,'ui_cmd.json')==[] for u in ui)


def test_unverified_situation_is_explained_not_a_predicate(setup, monkeypatch):
    _, store = setup
    original = vlm.situation
    def unverified(*a, **kw):
        rec = original(*a, **kw)
        return rec.model_copy(update={'verification':rec.verification.model_copy(update={'status':'unverified'})})
    monkeypatch.setattr(vlm, 'situation', unverified)
    frames = wh_main(store)
    claim = [f['data'] for f in frames if f['event']=='claim'][-1]
    assert [p['predicate_id'] for p in claim['predicates']]==['p1'] and claim['explain']['c1']
    assert frames[-2]['data']['final']=='SUPPORTED'


def test_hazard_zone_is_annotated_as_zone(setup):
    _, store = setup
    notes = [f['data']['args'] for f in wh_main(store) if f['event']=='ui' and f['data']['cmd']=='annotate']
    assert notes and all(n['kind']=='zone' and n['zone_id']=='z_fixture' and n['text'] and 'xy' not in n for n in notes)


def test_how_far_answers_with_the_measurement(setup):
    _, store = setup
    frames = wh_main(store)
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    p1 = [f['data'] for f in frames if f['event']=='claim'][-1]['predicates'][0]
    assert (p1['operator'],p1['status'])==('measured','supported')
    answer = next(f['data'] for f in frames if f['event']=='answer')
    assert 'contradicts' not in answer['text'] and '±' in answer['text']
    assert [r['field'] for r in answer['measure_refs']]==['value.t_s','value','sigma']
    frames = list(agent.ask_frames(store, agent.AskRequest(q='When the worker dodged the forklift, was the distance less than 1 meter?', context=WH_MAIN['context'])))
    p1 = [f['data'] for f in frames if f['event']=='claim'][-1]['predicates'][0]
    assert (p1['operator'],p1['value'])==('lt',1.)


CLOSEST_Q = 'What was the distance when the forklift and worker were closest?'


@pytest.mark.parametrize('q', [CLOSEST_Q,'What is the gap between the worker and forklift?','What is the minimum distance between the worker and forklift?','How many meters is the worker from the forklift?','How far is the worker from the forklift?',
                               'What is the closest approach of the worker and the forklift?',
                               'How close does the worker get to the forklift?',
                               'distance between the worker and the forklift','nearest forklift to the worker'])
def test_distance_words_choose_find_without_the_llm(monkeypatch, q):
    from app.core import llm
    monkeypatch.setenv('LLM_URL','http://llm/v1')
    calls = []
    monkeypatch.setattr(llm,'complete_json',lambda *a, **kw: calls.append(kw['role']) or {'template':'unsupported','entities':[]})
    assert llm.parse(q, WH_MAIN['context']).template=='find' and calls==[]
    assert llm.parse('Why '+q, WH_MAIN['context']).template=='unsupported' and calls==[]


def fixture_closest():
    import math
    scene = FIXTURES/'sva-out'/'wh-sdg-fixture-01'
    persons = json.loads((scene/'skeleton.json').read_text())['persons']
    forklifts = json.loads((scene/'forklift.json').read_text())['forklifts']
    return min((math.dist(r,(q['x'],q['y'])),t,eid,f['entity_id']) for eid,p in persons.items()
               for t,r in zip(p['t_ms'],p['root_world']) for f in forklifts for q in f['track'] if q['t_ms']==t)


def test_closest_approach_scans_the_tracks_and_measures_the_minimum(setup, monkeypatch):
    _, store = setup
    def no_vlm(*a, **kw): raise AssertionError('VLM called')
    monkeypatch.setattr(vlm, 'situation', no_vlm)
    moments = []
    original = store.moment
    monkeypatch.setattr(store, 'moment', lambda key, t: moments.append(t) or original(key, t))
    value, tm, person, forklift = fixture_closest()
    frames = list(agent.ask_frames(store, agent.AskRequest(q=CLOSEST_Q, context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    assert [f['data']['name'] for f in frames if f['event']=='tool_call']==['closest_approach','measure']
    assert moments==[tm]
    answer = next(f['data'] for f in frames if f['event']=='answer')
    found = re.fullmatch(r'The worker and forklift were closest at a distance of ([\d.]+) m \(±([\d.]+) m\), at ([\d.]+) s\.', answer['text'])
    assert found, answer['text']
    assert float(found[1])==pytest.approx(value,abs=5e-4) and float(found[3])==tm/1000
    evidence = {e.eid:e for e in store.get_run(frames[0]['data']['run_id']).evidence}
    measured = next(e for e in evidence.values() if e.method=='ground_distance')
    assert measured.eid in answer['cites'] and measured.t0_ms==tm and float(found[2])==pytest.approx(measured.sigma,abs=5e-4)
    assert [r['field'] for r in answer['measure_refs']]==['value','sigma','value.t_s']
    ui = [(f['data']['cmd'],f['data']['args']) for f in frames if f['event']=='ui']
    assert ui==[('seek',{'t_ms':tm,'scene_key':'wh-sdg-fixture-01'}),('highlight',{'ids':[person,forklift],'mode':'outline'})]
    hits = [f['data']['moments'] for f in frames if f['event']=='hits']
    assert len(hits)==1 and [m['t0_ms'] for m in hits[0]]==[tm]
    claim = [f['data'] for f in frames if f['event']=='claim'][-1]
    assert claim['predicates'][0]['eids']==[measured.eid]


def test_closest_approach_names_the_missing_kind(setup, monkeypatch):
    _, store = setup
    original = store.asset_json
    monkeypatch.setattr(store, 'asset_json', lambda key, rel: {**original(key, rel),'forklifts':[]} if rel=='forklift.json' else original(key, rel))
    frames = list(agent.ask_frames(store, agent.AskRequest(q=CLOSEST_Q, context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='UNRESOLVED'
    assert not any(f['event']=='hits' for f in frames)
    answer = next(f['data'] for f in frames if f['event']=='answer')
    assert 'No forklift' in answer['text'] and 'worker' not in answer['text'].split(',')[0], answer['text']


@pytest.mark.parametrize('limit,final', [(0.1, 'CONTRADICTED'), (5, 'SUPPORTED')])
def test_closest_approach_with_a_threshold_compares_against_it(setup, limit, final):
    _, store = setup
    q = f'Is the minimum distance between the worker and forklift less than {limit} meters?'
    frames = list(agent.ask_frames(store, agent.AskRequest(q=q, context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[]
    claim = [f['data'] for f in frames if f['event']=='claim'][-1]
    assert (claim['predicates'][0]['operator'], claim['predicates'][0]['value'])==('lt', limit)
    assert (frames[-2]['data']['final'], frames[-2]['data']['stop_reason'])==(final, 'enough_evidence')
    text = next(f['data']['text'] for f in frames if f['event']=='answer')
    assert text.endswith(', less than the requested distance.' if final=='SUPPORTED' else ', not less than the requested distance.'), text


def test_closest_approach_only_scans_people_inside_the_zone(setup):
    _, store = setup
    value, tm, person, forklift = fixture_closest()
    root = store.moment('wh-sdg-fixture-01', tm).persons
    x, y = next(p.root_world for p in root if p.entity_id==person)[:2]
    # a 1 m square away from the globally closest worker's position at that time
    zone = [[x+3, y+3], [x+4, y+3], [x+4, y+4], [x+3, y+4]]
    frames = list(agent.ask_frames(store, agent.AskRequest(q=CLOSEST_Q, context={**WH_MAIN['context'], 'zone': zone})))
    assert check_sse_sequence(frames)==[]
    hit = next((f['data']['moments'] for f in frames if f['event']=='hits'), [])
    assert all(not (m['t0_ms']==tm and person in [a['entity_id'] for a in m['actor_refs']]) for m in hit)


@pytest.mark.parametrize('behaviour', ['hits', 'hang', 'raise'])
def test_site_ask_reports_vss_native_after_the_answer(setup, monkeypatch, behaviour):
    import threading
    _, store = setup
    monkeypatch.setattr(agent, 'NATIVE_WAIT_S', .3)
    release = threading.Event()
    hit = {'rank':1,'source':'s3://seg/a.mp4','scene_key':None,'cam_id':None,'t_ms':None,'similarity':.42,'caption':'worker steps back'}
    def search(q, top_k, filters):
        if behaviour == 'raise':
            raise RuntimeError('vss down')
        if behaviour == 'hang':
            release.wait(60)
        return {'hits':[hit]*12,'sql_query':'SELECT 1'}
    try:
        frames = list(agent.ask_frames(store, agent.AskRequest(q=WH_MAIN['q'], context=WH_MAIN['context']),
                                       vss=SimpleNamespace(search=search)))
    finally:
        release.set()
    assert check_sse_sequence(frames)==[]
    events = [f['event'] for f in frames]
    assert events[-3:]==['status','vss_native','done'] and events.index('answer')<len(events)-3
    assert frames[-3]['data']['final']=='SUPPORTED'
    native = frames[-2]['data']
    status = {'hits':'ok','hang':'timeout','raise':'error'}[behaviour]
    assert native['status']==status and native['elapsed_ms']>=0
    if behaviour == 'hits':
        assert native['sql_query']=='SELECT 1' and native['hits']==[hit]*10
    else:
        assert native['sql_query'] is None and native['hits']==[]
    step = next(s for s in store.get_run(native['run_id']).steps if s.name=='vss.search')
    assert step.output['status']==status


def test_site_ask_sends_status_before_waiting_for_vss_native(setup, monkeypatch):
    import threading
    _, store = setup
    monkeypatch.delenv('WANDB_API_KEY', raising=False)
    start_run, traced, finish_run = REAL_TRACE
    finished = []
    def finish(run, status, **kw):
        finished.append(status)
        return finish_run(run, status, **kw)
    monkeypatch.setattr(trace, 'start_run', start_run)
    monkeypatch.setattr(trace, 'traced', traced)
    monkeypatch.setattr(trace, 'finish_run', finish)
    monkeypatch.setattr(agent, 'NATIVE_WAIT_S', .5)
    release = threading.Event()
    def search(q, top_k, filters):
        release.wait(60)
        return {'hits':[],'sql_query':'SELECT 1'}
    frames, at = [], {}
    try:
        for f in agent.ask_frames(store, agent.AskRequest(q=WH_MAIN['q'], context=WH_MAIN['context']), vss=SimpleNamespace(search=search)):
            frames.append(f)
            at[f['event']] = time.monotonic()
    finally:
        release.set()
    assert check_sse_sequence(frames)==[]
    assert [f['event'] for f in frames][-3:]==['status','vss_native','done'] and frames[-2]['data']['status']=='timeout'
    assert at['vss_native']-at['status']>=.4
    run = store.get_run(frames[0]['data']['run_id'])
    assert finished==['done'] and run.status=='done' and run.final=='SUPPORTED'
    step = next(s for s in run.steps if s.name=='vss.search')
    assert (step.exec_state, step.output['status'])==('live','timeout')


def test_ask_without_site_vss_has_no_vss_native(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q=WH_MAIN['q'], context=WH_MAIN['context'])))
    assert 'vss_native' not in [f['event'] for f in frames]
