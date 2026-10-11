import json
import re
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import agent, search
from app.core import embed, tools, trace, vlm
from app.core.models import ABSTAIN_TEXT
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
                assert claims[0]['after']=='SUPPORTED' and claims[-1]['after']=='SUPPORTED'
                plans = [s for s in saved['steps'] if s['name']=='policy.plan_next']
                assert plans[0]['output'].get('cam') is None
                assert saved['hits'][0]['primary_cam']=='ceiling_04'
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
    assert frames[-2]['data']['final']=='SUPPORTED'
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
    assert frames[-2]['data']['stop_reason']=='enough_evidence'
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
    context = {'workspace':'fleet','scene_key':'fl-pie-fixture-01','t_range':[0,4000]}  # GPS samples cover this range
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
    (lambda key,t: 40. if key=='fl-pie-fixture-01' else None, 'UNRESOLVED', 'Maximum measured speed'),
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


def test_stream_ends_cleanly_when_the_answer_cache_write_fails(setup, monkeypatch):
    app, store = setup
    def fail(*a, **kw):
        raise RuntimeError('TabularValueTooLarge')
    monkeypatch.setattr(store, 'cache_put', fail)
    with TestClient(app) as client:
        frames = read_frames(client.post('/api/agent/ask', json={k:WH_MAIN[k] for k in ['q','context','budget']}))
    assert check_sse_sequence(frames) == [] and frames[-1]['event'] == 'done'


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


# ---------------------------------------------------------------- corpus scope and street (L1) scenes

L1_KEY = 'st-fixture_cam_1-clip01'
CORPUS_ASKS = {a['ask_id']:a for a in json.loads((FIXTURES/'l1'/'asks_corpus.json').read_text())['asks']}
ANCHOR = CORPUS_ASKS['corpus-anchor']
CORPUS = {'workspace':'warehouse','scope':'corpus'}


def corpus_store(tmp_path):
    """The fixture scenes plus the committed L1 street scene, as one asset dir and index."""
    import shutil
    from fixtures.gen_fixture import build_index
    from app.tests.unit.a5_fakes import FixtureStore
    assets = shutil.copytree(FIXTURES/'sva-out', tmp_path/'sva-out')
    shutil.copytree(FIXTURES/'l1'/L1_KEY, assets/L1_KEY)
    build_index(tmp_path/'index.duckdb', FIXTURES, l1=True)
    return FixtureStore(index_path=tmp_path/'index.duckdb', app_path=tmp_path/'app.duckdb', asset_dir=assets)


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    """Corpus store; street C4 answers with an unverified record (the fixture has no street cosmos file)."""
    from app.core.models import CosmosRecord, make_record_id
    store = corpus_store(tmp_path)
    install_trace(monkeypatch)
    install_vlm(monkeypatch)
    monkeypatch.setattr(embed, 'embed_text', lambda q: [0.0]*256)
    recorded = vlm.localize
    def localize(scene_key, cam, t0_ms, t1_ms, query, *, run=None):
        if scene_key != L1_KEY:
            return recorded(scene_key, cam, t0_ms, t1_ms, query, run=run)
        t = (t0_ms+t1_ms)//2
        return CosmosRecord(record_id=make_record_id(scene_key,'C4',t,cam,[query,f'{t0_ms}-{t1_ms}'],vlm.PROMPT_REV['C4-street']),
                            task='C4',t_ms=t,cam_id=cam,actor_refs=[],input_frame_refs=[{'cam_id':cam,'t_ms':t}],prefix_only=False,
                            model_rev='m',prompt_rev=vlm.PROMPT_REV['C4-street'],payload={'query':query,'segments':[]},
                            verification={'status':'unverified','method':'fixture','ref':None})
    monkeypatch.setattr(vlm, 'localize', localize)
    return store


def ask(store, q, context=CORPUS, budget=None):
    return list(agent.ask_frames(store, agent.AskRequest(q=q, context=context, **({'budget':budget} if budget else {}))))


def of(frames, event):
    return [f['data'] for f in frames if f['event']==event]


def assert_ui_frames(store, frames):
    for u in of(frames, 'ui'):
        assert u['scene_key'] and schema_errors(u, 'ui_cmd.json')==[], u
        if u['cmd']=='seek':
            assert u['args']['scene_key']==u['scene_key']
        if u['cmd']=='highlight':
            assert store.manifest(u['scene_key'])['workspace'] in {'warehouse','fleet'}, u


def test_corpus_asks_run_through_the_real_agent(corpus):
    for ask_id, a in CORPUS_ASKS.items():
        frames = ask(corpus, a['q'], a['context'], a['budget'])
        assert check_sse_sequence(frames)==[], ask_id
        assert_ui_frames(corpus, frames)
        assert all(e['evidence_level'] for r in of(frames, 'tool_result') for e in r['evidence']), ask_id
        # the fixture frames are recorded from this agent; the test fakes differ only in the street Cosmos record
        assert frames[-2]['data']['final']==a['expect']['final']==a['events'][-2]['data']['final'], ask_id
        assert [h['auto_open'] for h in of(frames, 'hits')]==[h['auto_open'] for h in of(a['events'], 'hits')], ask_id
    assert {k:(a['expect']['final'],[h['auto_open'] for h in of(a['events'], 'hits')]) for k, a in CORPUS_ASKS.items()}=={
        'corpus-anchor':('SUPPORTED',[None]),'corpus-street-distance':('UNRESOLVED',[L1_KEY]),'corpus-nomatch':('NO_MATCH',[])}
    frames = ask(corpus, ANCHOR['q'], ANCHOR['context'], ANCHOR['budget'])
    assert frames[-2]['data']['final']==ANCHOR['expect']['final']
    hits = of(frames, 'hits')[0]['moments']
    assert {m['evidence_level'] for m in hits}=={'L1','L2','L3'}


def test_anchor_measures_on_the_l3_hit_and_abstains_on_l1(corpus):
    frames = ask(corpus, ANCHOR['q'], ANCHOR['context'], ANCHOR['budget'])
    read = {e['eid']:e for r in of(frames, 'tool_result') for e in r['evidence']}
    claims = {c['claim_id']:c for c in of(frames, 'claim')}.values()
    l3 = next(c for c in claims if c['evidence_level']=='L3')
    assert l3['predicates'][0]['operator']=='measured' and l3['predicates'][0]['status']=='supported'
    assert [read[e]['method'] for e in l3['predicates'][0]['eids']]==['ground_distance']
    l1 = next(c for c in claims if c['evidence_level']=='L1')
    assert l1['scene_key']==L1_KEY and l1['moment_id'].startswith('m_'+L1_KEY)
    assert l1['explain']['abstain']=={'reason':'no_geometry','text':ABSTAIN_TEXT['no_geometry']}
    assert [(p['operator'],p['status']) for p in l1['predicates']]==[('detected','supported'),('measured','unknown')]
    answers = {a['evidence_level']:a for a in of(frames, 'answer')}
    assert list(answers)==[None,'L3','L2','L1']
    assert answers['L1']['measure_refs']==[] and '(2D)' in answers['L1']['text'] and ABSTAIN_TEXT['no_geometry'] in answers['L1']['text']
    assert answers['L3']['measure_refs'] and 'uncited number' not in answers['L3']['text']
    assert all(e.evidence_level for e in corpus.get_run(frames[0]['data']['run_id']).evidence)


def test_l1_claims_do_not_count_toward_the_final(corpus):
    q = 'How far was the pedestrian from the car on the street?'
    frames = ask(corpus, q, {'workspace':'street','scene_key':L1_KEY})
    assert check_sse_sequence(frames)==[]
    assert {m['scene_key'] for m in of(frames, 'hits')[0]['moments']}=={L1_KEY}
    claims = of(frames, 'claim')
    assert claims and all(c['explain']['abstain']['reason']=='no_geometry' for c in claims)
    assert (frames[-2]['data']['final'],frames[-2]['data']['stop_reason'])==('UNRESOLVED','abstained')
    answer, = of(frames, 'answer')
    assert answer['measure_refs']==[] and 'person and a car in the same frame (2D)' in answer['text']
    assert [f['data']['cmd'] for f in frames if f['event']=='ui']==['seek','seek']


def test_street_presence_is_supported_by_2d_detections_only(corpus):
    frames = ask(corpus, 'Find a person and a car', {'workspace':'street','scene_key':L1_KEY})
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    read = {e['eid']:e for r in of(frames, 'tool_result') for e in r['evidence']}
    p = of(frames, 'claim')[0]['predicates']
    assert [(x['operator'],x['value'],x['status']) for x in p]==[('detected',['person','car'],'supported')]
    assert [(read[e]['kind'],read[e]['method']) for e in p[0]['eids']]==[('measure','yolo_coco_2d')]
    frames = ask(corpus, 'Find a person and a horse', {'workspace':'street','scene_key':L1_KEY})
    # a 2D miss is not a contradiction
    assert all(c['predicates'][0]['status']=='unknown' for c in of(frames, 'claim')) and frames[-2]['data']['final']=='UNRESOLVED'


def test_cosmos_runs_on_the_top_street_moment_only(corpus):
    frames = ask(corpus, 'Find a person and a car', {'workspace':'street','scene_key':L1_KEY}, {'tools':16,'seconds':45})
    calls = [c for c in of(frames, 'tool_call') if c['name']=='localize']
    assert len(calls)==1 and calls[0]['args']['scene_key']==L1_KEY
    assert [r['scene_key'] for r in of(frames, 'model_result')]==[L1_KEY]
    assert any((c['explain'] or {}).get('c4') for c in of(frames, 'claim'))


SPATIAL_QS = [ANCHOR['q'], 'Was this car speeding?', 'At Queen and Spadina, what is the building on the right?',
              BLIND_Q, 'Where is the person?', 'Is this the same person in these two clips?', CLOSEST_Q]


@pytest.mark.parametrize('q', SPATIAL_QS)
@pytest.mark.parametrize('context', [CORPUS, {'workspace':'street','scene_key':L1_KEY},
                                     {'workspace':'fleet','scope':'corpus'}, {'workspace':'street','scope':'corpus'}])
def test_street_scenes_never_reach_geometry_tools(corpus, monkeypatch, q, context):
    seen = []
    def guard(name, scene_of, fleet_only=False):
        original = getattr(tools, name)
        def spy(store, *a, **kw):
            for key in scene_of(*a):
                workspace = store.manifest(key)['workspace']
                assert workspace!='street', (name, key)
                assert not fleet_only or workspace=='fleet', (name, key)
            seen.append(name)
            return original(store, *a, **kw)
        monkeypatch.setattr(tools, name, spy)
    for name in ['measure','visibility','speed_limit','get_synced_views','blind_presence']:
        guard(name, lambda key, *a: [key])
    guard('closest_approach', lambda scenes, *a: [s['scene_key'] for s in scenes])
    guard('speeding_candidates', lambda scenes, *a: [s['scene_key'] for s in scenes], fleet_only=True)
    guard('facade', lambda passes, *a: [p['trip_id'] for p in passes], fleet_only=True)
    frames = ask(corpus, q, context, {'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[], q
    assert frames[-2]['data']['stop_reason']!='error', q
    assert_ui_frames(corpus, frames)


@pytest.mark.parametrize('q,cap,levels', [(BLIND_Q,'blind_spot',{'L2':'no_visibility','L1':'no_geometry'}),
                                          ('Was this car speeding?','speed',{'L3':'no_gps','L1':'no_gps'})])
def test_special_paths_in_corpus_scope_abstain_once_per_other_level(corpus, monkeypatch, q, cap, levels):
    set_limits(corpus, monkeypatch, lambda key,t: 5. if (key,t)==('fl-pie-fixture-01',9500) else None)
    frames = ask(corpus, q, CORPUS, {'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[]
    abstains = [c for c in of(frames, 'claim') if (c['explain'] or {}).get('abstain')]
    assert {c['evidence_level']:c['explain']['abstain']['reason'] for c in abstains}==levels
    assert all(c['scene_key'] is None and c['predicates'][0]['operator']==cap for c in abstains)
    assert frames[-2]['data']['final']=='SUPPORTED'
    texts = {a['evidence_level']:a['text'] for a in of(frames, 'answer')}
    assert all(ABSTAIN_TEXT[r] in texts[lv] for lv, r in levels.items())


def test_scene_scope_is_unchanged_when_scope_is_absent(corpus):
    # the corpus store holds a street scene too; without scope the warehouse ask never sees it
    frames = ask(corpus, WH_MAIN['q'], WH_MAIN['context'])
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    assert {m['scene_key'] for m in of(frames, 'hits')[0]['moments']}=={'wh-sdg-fixture-01'}
    assert all('evidence_level' not in m for m in of(frames, 'hits')[0]['moments'])
    assert of(frames, 'hits')[0]['auto_open']=='wh-sdg-fixture-01' and {'seek','highlight'}<={u['cmd'] for u in of(frames, 'ui')}
    assert [a['sentence_id'] for a in of(frames, 'answer')]==['a1'] and 'evidence_level' not in of(frames, 'answer')[0]


@pytest.mark.parametrize('context', [{**CORPUS,'t_ms':4600,'scene_key':'wh-sdg-fixture-01'},
                                     {**CORPUS,'selection':['P1'],'scene_key':'wh-sdg-fixture-01'}])
def test_scope_is_forced_to_scene_by_time_or_selection(corpus, context):
    frames = ask(corpus, 'Find the worker', context)
    assert check_sse_sequence(frames)==[]
    assert corpus.get_run(frames[0]['data']['run_id']).steps[0].output['scope']=='scene'
    assert {m['scene_key'] for f in of(frames, 'hits') for m in f['moments']}<={'wh-sdg-fixture-01'}


def test_entity_ids_force_scene_scope(corpus):
    frames = ask(corpus, 'Find P1', {**CORPUS,'scene_key':'wh-sdg-fixture-01'})
    assert corpus.get_run(frames[0]['data']['run_id']).steps[0].output['scope']=='scene'
    assert all(m['scene_key']=='wh-sdg-fixture-01' for f in of(frames, 'hits') for m in f['moments'])


def test_corpus_budget_keeps_a_tool_for_each_level(corpus):
    frames = ask(corpus, ANCHOR['q'], CORPUS, {'tools':4,'seconds':25})
    assert check_sse_sequence(frames)==[]
    calls = [c['name'] for c in of(frames, 'tool_call')]
    assert calls[-1]=='detections_2d' and len(calls)==4, calls
    assert frames[-2]['data']['stop_reason']=='budget_exhausted'


def test_noun_follows_the_scene_workspace():
    assert [agent.noun('person', w) for w in ['warehouse','fleet','street']]==['worker','pedestrian','person']
    assert agent.noun('vehicle','street')=='vehicle'


def test_ask_route_accepts_the_street_workspace(corpus):
    app = FastAPI()
    app.state.store = corpus
    app.include_router(agent.router)
    with TestClient(app) as client:
        frames = read_frames(client.post('/api/agent/ask', json={'q':'Find a person','context':{'workspace':'street','scene_key':L1_KEY}}))
        assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
        assert client.post('/api/agent/ask', json={'q':'Find a person','context':{'workspace':'ocean'}}).status_code==422


# ---------------------------------------------------------------- source tags and the candidate list

def test_search_sources_limit_scene_filters(corpus):
    _,result=search.retrieve(corpus,search.SearchRequest(q='Find a person',workspace='warehouse',sources=['fleet','fixed'],k=1))
    assert {m['scene_key'] for m in result.envelope.output['moments']}=={'fl-pie-fixture-01',L1_KEY}


def test_sources_limit_the_corpus_hits(corpus):
    frames = ask(corpus, ANCHOR['q'], {**CORPUS,'sources':['fleet','fixed']})
    assert check_sse_sequence(frames)==[]
    assert {m['tag'] for m in of(frames, 'hits')[0]['moments']}=={'fleet','fixed'}


def test_one_scene_of_hits_opens_and_is_navigated(corpus):
    frames = ask(corpus, ANCHOR['q'], {**CORPUS,'sources':['warehouse']})
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    hits, = of(frames, 'hits')
    assert hits['auto_open']=='wh-sdg-fixture-01'=={m['scene_key'] for m in hits['moments']}.pop()
    ui = of(frames, 'ui')
    assert {'seek','highlight'}<={u['cmd'] for u in ui} and {u['scene_key'] for u in ui}=={'wh-sdg-fixture-01'}


def test_hits_across_scenes_send_no_navigation(corpus):
    frames = ask(corpus, ANCHOR['q'], {**CORPUS,'sources':['warehouse','fleet','fixed']}, {'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    hits, = of(frames, 'hits')
    assert hits['auto_open'] is None and len({m['scene_key'] for m in hits['moments']})==3
    assert [u['cmd'] for u in of(frames, 'ui') if u['cmd']!='annotate']==[]
    # the moments are still inspected, claimed and answered
    assert {c['evidence_level'] for c in of(frames, 'claim')}=={'L1','L2','L3'} and len(of(frames, 'answer'))==4


@pytest.mark.parametrize('q,context,special', [
    (ANCHOR['q'], CORPUS, False), (WH_MAIN['q'], WH_MAIN['context'], False), (BLIND_Q, CORPUS, True),
    (CLOSEST_Q, {'workspace':'warehouse','scene_key':'wh-sdg-fixture-01'}, True),
    ('At Queen and Spadina, what is the building on the right?', {'workspace':'fleet'}, True)])
def test_every_hit_carries_its_tag_and_title(corpus, q, context, special):
    from app.core.models import level_of, tag_of
    frames = ask(corpus, q, context, {'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[]
    moments = [m for h in of(frames, 'hits') for m in h['moments']]
    assert moments, q
    for m in moments:
        manifest = corpus.manifest(m['scene_key'])
        assert (m['tag'],m['title'])==(tag_of(manifest),manifest['title'])
        if special:
            assert (m['workspace'],m['evidence_level'])==(manifest['workspace'],level_of(manifest))
    assert [m.title for m in corpus.get_run(frames[0]['data']['run_id']).hits]==[m['title'] for m in moments]


@pytest.mark.parametrize('q,tool', [(BLIND_Q,'blind_presence'), (CLOSEST_Q,'closest_approach')])
def test_special_paths_scan_only_the_scenes_of_the_sources(corpus, q, tool):
    frames = ask(corpus, q, {**CORPUS,'sources':['warehouse']}, {'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[] and tool in [c['name'] for c in of(frames, 'tool_call')]
    # levels outside the sources are not reported as unchecked
    assert all(c['scene_key'] for c in of(frames, 'claim'))
    frames = ask(corpus, q, {**CORPUS,'sources':['fleet','fixed']}, {'tools':16,'seconds':45})
    assert check_sse_sequence(frames)==[] and tool not in [c['name'] for c in of(frames, 'tool_call')]


@pytest.mark.parametrize('q', ['How close did the forklift come to a person?',
    'How close does the worker get to the forklift?', 'How close did the forklift get to the worker from 0 to 4 seconds?'])
def test_b1_how_close_measures_the_interval_minimum(setup, q):
    _, store = setup
    frames = ask(store, q, WH_MAIN['context'])
    calls = of(frames, 'tool_call')
    assert [c['name'] for c in calls]==['closest_approach','measure']
    scan, measured = of(frames, 'tool_result')
    nearest = scan['envelope']['output']['closest']
    assert calls[1]['args']['t_ms']==nearest['t_ms']
    if 'from' not in q:
        assert nearest['t_ms']==4900 and measured['evidence'][0]['value']==pytest.approx(1.409,abs=.001)
    else:
        assert nearest['t_ms']<4000
    assert frames[-2]['data']['final']=='SUPPORTED' and check_sse_sequence(frames)==[]


@pytest.mark.parametrize('relation', ['within 2 meters of', 'over 2 m from', 'at least 2 meters from'])
def test_b1_metric_units_gate_l1(corpus, relation):
    frames = ask(corpus, 'Did a car pass '+relation+' a person?', {'workspace':'street','scene_key':L1_KEY})
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert all(c['explain']['abstain']['reason']=='no_geometry' for c in of(frames, 'claim'))
    assert ABSTAIN_TEXT['no_geometry'] in of(frames, 'answer')[0]['text']


@pytest.mark.parametrize('phrase,operator,final', [('greater than','gt','CONTRADICTED'), ('more than','gt','CONTRADICTED'),
    ('over','gt','CONTRADICTED'), ('at least','ge','CONTRADICTED'), ('farther than','gt','CONTRADICTED'),
    ('less than','lt','SUPPORTED'), ('under','lt','SUPPORTED'), ('within','lt','SUPPORTED'),
    ('closer than','lt','SUPPORTED'), ('at most','le','SUPPORTED')])
@pytest.mark.parametrize('closest', [False,True])
def test_b1_distance_comparison_direction(setup, phrase, operator, final, closest):
    _, store = setup
    q = f'Was the {"minimum " if closest else ""}distance between the worker and forklift {phrase} 3 meters?'
    frames = ask(store, q, WH_MAIN['context'])
    p = of(frames, 'claim')[-1]['predicates'][0]
    assert p['operator']==operator and frames[-2]['data']['final']==final


def test_b1_point_time_limits_investigation(setup):
    _, store = setup
    frames = ask(store, 'How close did the forklift come to a person at 5 seconds?', WH_MAIN['context'])
    calls = of(frames, 'tool_call')
    assert calls[0]['args'].get('t_range')==(5000,6000)
    assert all(5000<=e['t0_ms']<6000 for r in of(frames, 'tool_result') for e in r['evidence'] if e['method']=='ground_distance')


def b1_candidates(store, monkeypatch, values=(1.,3.)):
    from app.core.models import EntityRef, MomentCard
    key = 'wh-sdg-fixture-01'
    cards = [MomentCard(moment_id=f'm_b1_{i}',scene_key=key,t0_ms=4300+i*100,t1_ms=4400+i*100,
                        cams=['ceiling_04'],primary_cam='ceiling_04',
                        actor_refs=[EntityRef(scene_key=key,entity_id=f'P{i+1}',kind='person'),
                                    EntityRef(scene_key=key,entity_id='F1',kind='forklift')],
                        evidence_ids=[],score=1,why=[],thumb=None) for i in range(len(values))]
    monkeypatch.setattr(tools, 'search', lambda *a, **kw: tools.result(key,0,10000,{'moments':[m.model_dump() for m in cards]}))
    def measure(store, key, tm, a, b, kind, *, cam=None):
        ev = tools.evidence(store,key,cam,tm,tm+1,'measure','ground_distance',values[int(a.entity_id[1:])-1],
                            sigma=.1,unit='m',params={'a':a.entity_id,'b':b.entity_id})
        return tools.result(key,tm,tm+1,{'value':ev.value},evidence=[ev],coverage=1)
    monkeypatch.setattr(tools, 'measure', measure)
    original = vlm.situation
    def unverified(*a, **kw):
        record = original(*a, **kw)
        return record.model_copy(update={'verification':record.verification.model_copy(update={'status':'unverified'})})
    monkeypatch.setattr(vlm, 'situation', unverified)
    return cards


@pytest.mark.parametrize('scope', ['scene','corpus'])
def test_b1_candidates_use_the_same_existential_aggregation(setup, monkeypatch, scope):
    _, store = setup
    b1_candidates(store, monkeypatch)
    frames = ask(store, 'Was anyone at a distance less than 2 meters from the forklift?',
                 {**WH_MAIN['context'],'scope':scope}, {'tools':16,'seconds':25})
    assert frames[-2]['data']['final']=='SUPPORTED'
    assert all('contradicts' not in a['text'] for a in of(frames, 'answer'))


@pytest.mark.parametrize('scope', ['scene','corpus'])
def test_b1_unchecked_candidates_cannot_establish_absence(setup, monkeypatch, scope):
    _, store = setup
    b1_candidates(store, monkeypatch, (3.,3.))
    frames = ask(store, 'Was anyone at a distance less than 2 meters from the forklift?',
                 {**WH_MAIN['context'],'scope':scope}, {'tools':5,'seconds':25})
    assert frames[-2]['data']['stop_reason']=='budget_exhausted'
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert all('contradicts' not in a['text'] for a in of(frames, 'answer'))


def test_b1_answer_uses_only_the_assessed_claim_eids(setup, monkeypatch):
    _, store = setup
    original = tools.search
    def search(*a, **kw):
        res = original(*a, **kw)
        key = 'wh-sdg-fixture-01'
        wrong = tools.evidence(store,key,None,1000,1001,'measure','event_gt',
                               {'type':'dodge','t_s':1.,'event_id':key+':discarded'})
        distance = tools.evidence(store,key,None,1000,1001,'measure','ground_distance',99.,sigma=.1,unit='m')
        res.evidence[:0] = [wrong,distance]
        return res
    monkeypatch.setattr(tools, 'search', search)
    frames = wh_main(store)
    claims = {c['claim_id']:c for c in of(frames, 'claim')}
    for answer in of(frames, 'answer'):
        allowed = {e for cid in answer['claim_ids'] for p in claims[cid]['predicates'] for e in p['eids']}
        assert set(answer['cites'])<=allowed
        assert {r['eid'] for r in answer['measure_refs']}<=allowed
        assert '99' not in answer['text'] and 'at 1 s' not in answer['text']


def test_b1_explicit_model_conflict_is_unresolved(setup, monkeypatch):
    _, store = setup
    original = vlm.situation
    def conflict(*a, **kw):
        record = original(*a, **kw)
        return record.model_copy(update={'verification':record.verification.model_copy(update={'status':'conflict'})})
    monkeypatch.setattr(vlm, 'situation', conflict)
    frames = wh_main(store)
    assert of(frames, 'claim')[-1]['explain']['conflict']
    assert of(frames, 'claim')[-1]['aggregate']=='UNRESOLVED'
    assert frames[-2]['data']['final']=='UNRESOLVED'


def test_b1_optional_description_failure_does_not_veto_distance(setup, monkeypatch):
    _, store = setup
    def fail(*a, **kw): raise vlm.VLMUnavailable('offline')
    monkeypatch.setattr(vlm, 'situation', fail)
    frames = wh_main(store)
    assert frames[-2]['data']['final']=='SUPPORTED'
    claim = of(frames, 'claim')[-1]
    assert all(p['operator']!='situation' for p in claim['predicates'])
    assert claim['explain']['note']


def test_b1_measurement_capability_reason_reaches_claim_and_answer(setup, monkeypatch):
    _, store = setup
    original = store.manifest
    monkeypatch.setattr(store, 'manifest', lambda key: {**original(key),'metric':False})
    frames = wh_main(store)
    claim = of(frames, 'claim')[-1]
    assert claim['explain']['abstain']=={'reason':'scene_not_metric','text':ABSTAIN_TEXT['scene_not_metric']}
    assert frames[-2]['data']['final']=='UNRESOLVED'
    assert ABSTAIN_TEXT['scene_not_metric'] in of(frames, 'answer')[0]['text']
    assert any(e['kind']=='model' for r in of(frames, 'tool_result') for e in r['evidence'])


@pytest.mark.parametrize('failure', ['vlm','detections','action'])
def test_b1_transient_failures_are_not_cached(corpus, monkeypatch, failure):
    store = corpus
    app = FastAPI()
    app.state.store = store
    app.include_router(agent.router)
    if failure=='detections':
        body = {'q':'Find a person and a car','context':{'workspace':'street','scene_key':L1_KEY}}
        original = tools.detections_2d
        def unavailable(store, key, cam, t0, t1, groups):
            return tools.result(key,t0,t1,{'frames_together':0},gaps=[(t0,t1,'sidecar_unavailable')])
        monkeypatch.setattr(tools, 'detections_2d', unavailable)
    else:
        body = {k:WH_MAIN[k] for k in ['q','context','budget']}
        original = vlm.situation if failure=='vlm' else tools.trigger
        def fail(*a, **kw):
            if failure=='vlm': raise vlm.VLMUnavailable('offline')
            raise RuntimeError('action offline')
        monkeypatch.setattr(vlm if failure=='vlm' else tools, 'situation' if failure=='vlm' else 'trigger', fail)
    with TestClient(app) as client:
        first = read_frames(client.post('/api/agent/ask',json=body))
        monkeypatch.setattr(tools if failure=='detections' else vlm if failure=='vlm' else tools,
                            'detections_2d' if failure=='detections' else 'situation' if failure=='vlm' else 'trigger', original)
        second = read_frames(client.post('/api/agent/ask',json=body))
    assert first[-2]['data']['cached'] is False
    assert second[-2]['data']['cached'] is False
    assert second[-2]['data']['final']=='SUPPORTED'


@pytest.mark.parametrize('boundary', ['deadline','tools'])
def test_b1_completed_measurement_survives_budget_boundary(setup, monkeypatch, boundary):
    _, store = setup
    clock, offset = time.monotonic, [0]
    original = tools.measure
    def measure(*a, **kw):
        res = original(*a, **kw)
        if res.evidence and boundary=='deadline':
            offset[0]=30
        return res
    monkeypatch.setattr(agent.time, 'monotonic', lambda: clock()+offset[0])
    monkeypatch.setattr(tools, 'measure', measure)
    frames = ask(store, WH_MAIN['q'], WH_MAIN['context'], {'tools':4 if boundary=='tools' else 16,'seconds':25})
    assert any(e['method']=='ground_distance' for r in of(frames, 'tool_result') for e in r['evidence'])
    assert of(frames, 'claim')[-1]['predicates'][0]['status']=='supported'
    assert frames[-2]['data']['final']=='SUPPORTED' and frames[-2]['data']['stop_reason']=='budget_exhausted'
    assert check_sse_sequence(frames)==[]


FLEET_Q = 'Did a pedestrian cross in front of the car?'


def tool_results(frames):
    names = {f['data']['call_id']:f['data']['name'] for f in frames if f['event']=='tool_call'}
    return [(names[f['data']['call_id']],f['data']) for f in frames if f['event']=='tool_result']


@pytest.mark.parametrize('embed', [True, False])
def test_steps_and_tools_are_live_only_for_live_calls(setup, monkeypatch, tmp_path, embed):
    _, store = setup
    monkeypatch.setenv('RUNS_DIR', str(tmp_path/'runs'))
    if embed:
        monkeypatch.setenv('EMBED_URL', 'http://embed.test')
    else:
        monkeypatch.delenv('EMBED_URL', raising=False)
    frames = list(agent.ask_frames(store, agent.AskRequest(q=WH_MAIN['q'], context=WH_MAIN['context'])))
    assert check_sse_sequence(frames)==[] and frames[-2]['data']['final']=='SUPPORTED'
    retrieval = 'live' if embed else 'precomputed'
    states = [(name,r['envelope']['exec_state']) for name, r in tool_results(frames)]
    # the measured distance, the Cosmos record and the action read only scene asset files in dev
    assert states[0]==('search',retrieval) and {s for _, s in states[1:]}=={'precomputed'}
    assert {'measure','situation','actions.trigger'}<={n for n, _ in states}
    assert {f['data']['exec_state'] for f in frames if f['event']=='step'}=={'waiting'}
    steps = [s for s in store.get_run(frames[0]['data']['run_id']).steps if not s.name.startswith('tool.')]
    assert [s.exec_state for s in steps if s.name=='retrieve']==[retrieval]
    assert {s.exec_state for s in steps if s.name!='retrieve'}=={'precomputed'}


def test_special_path_hits_are_precomputed(setup):
    _, store = setup
    frames = list(agent.ask_frames(store, agent.AskRequest(q=BLIND_Q, context=WH_MAIN['context'])))
    moments = next(f['data']['moments'] for f in frames if f['event']=='hits')
    assert moments and {m['exec_state'] for m in moments}=={'precomputed'}


@pytest.mark.parametrize('site', [False, True])
def test_fleet_top_hit_measures_the_ego_distance_live(setup, monkeypatch, site):
    _, store = setup
    seen = []
    def measure(store, key, cam, t0, t1, pair=('person','forklift'), *, rows):
        seen.append((key,cam,pair,rows))
        return {'value_m':7.5,'sigma_m':.325,'t_ms':t0,'track_ids':[3],'n_frames':30,'n_boxes':12,
                'source_uris':['s3://bucket/detections/seg.json.gz'],'timing_ms':{'total':120.0}}
    monkeypatch.setattr(tools.live_measure, 'measure', measure)
    monkeypatch.setattr(agent, 'site_rows', lambda: ['site row'])
    vss = SimpleNamespace(search=lambda q, k, f: {'sql_query':'SELECT 1','hits':[]}) if site else None
    frames = list(agent.ask_frames(store, agent.AskRequest(q=FLEET_Q, context={'workspace':'fleet'}), vss=vss))
    assert check_sse_sequence(frames)==[] and next(f for f in frames if f['event']=='status')['data']['final']=='SUPPORTED'
    assert seen==[('fl-pie-fixture-01','dashcam',('person','vehicle'),['site row'] if site else [])]
    (_, r), = [(n, r) for n, r in tool_results(frames) if n=='ego_distance']
    ev, = r['evidence']
    assert r['envelope']['exec_state']=='live' and (ev['method'],ev['value'],ev['unit'])==('ego_distance',7.5,'m')
    assert r['envelope']['output']['live']['source_uris']==['s3://bucket/detections/seg.json.gz']
    run = store.get_run(frames[0]['data']['run_id'])
    assert ev['eid'] in [e.eid for e in run.evidence]
    assert [s.exec_state for s in run.steps if s.name=='assess' and s.state=='inspect']==['live']


def test_fleet_ego_distance_without_a_live_value_is_the_precomputed_one(setup, monkeypatch):
    _, store = setup
    monkeypatch.setattr(tools.live_measure, 'measure', lambda *a, **kw: {'value_m':None,'reason':'no live segment covers it'})
    frames = list(agent.ask_frames(store, agent.AskRequest(q=FLEET_Q, context={'workspace':'fleet'})))
    (_, r), = [(n, r) for n, r in tool_results(frames) if n=='ego_distance']
    assert r['envelope']['exec_state']=='precomputed' and r['envelope']['output']['reason']=='no live segment covers it'
    assert r['evidence'][0]['method']=='ego_distance' and r['evidence'][0]['value']==r['envelope']['output']['precomputed']['value_m']


def test_scene_question_assesses_at_most_three_moments_and_stops_with_a_reason(setup, monkeypatch):
    _, store = setup
    original = tools.search
    def five(*a, **kw):
        res = original(*a, **kw)
        m = res.envelope.output['moments'][0]
        res.envelope.output['moments'] = [{**m,'moment_id':f"{m['moment_id']}_{i}",'t0_ms':m['t0_ms']+100*i} for i in range(5)]
        return res
    monkeypatch.setattr(tools, 'search', five)
    frames = list(agent.ask_frames(store, agent.AskRequest(q=FLEET_Q, context={'workspace':'fleet'})))
    assert check_sse_sequence(frames)==[]
    assert len(next(f['data']['moments'] for f in frames if f['event']=='hits'))==5
    assert len({f['data']['moment_id'] for f in frames if f['event']=='claim'})==3
    assert frames[-2]['data']['stop_reason']!='budget_exhausted'
