"""Execution provenance across the tools present after B1 and B2."""
from types import SimpleNamespace

import pytest

from app.api import agent
from app.api.search import SearchRequest, retrieve
from app.core import embed, street, tools
from app.core.models import SearchFilters
from app.tests.unit.a5_fakes import L1_CAM, L1_KEY, fixture_l1_store, install_trace, install_vlm
from app.tests.unit.test_agent import corpus_store
from app.tests.unit.test_street import s3_with, sidecar

WH, FL = 'wh-sdg-fixture-01', 'fl-pie-fixture-01'


def test_street_s3_then_local_sidecar_provenance(tmp_path):
    s = fixture_l1_store(tmp_path,site=True)
    gets = []
    s.s3 = s3_with(lambda key:sidecar(50),gets)
    first = tools.detections_2d(s,L1_KEY,L1_CAM,0,5000,{'person':['person']})
    second = tools.detections_2d(s,L1_KEY,L1_CAM,0,5000,{'person':['person']})
    assert (first.envelope.exec_state,second.envelope.exec_state)==('live','precomputed')
    assert len(gets)==1 and first.evidence==second.evidence
    clip = s.search_clips(SearchFilters(
        workspace='street',scene_keys=[L1_KEY],t_range=(0,5000)),None,None)[0]
    card, _ = street.moment(s,clip,backend='duckdb')
    assert card.exec_state=='precomputed'


@pytest.mark.parametrize('live', [False,True])
def test_search_cards_and_envelope_follow_request_source(tmp_path, monkeypatch, live):
    s = corpus_store(tmp_path)
    monkeypatch.delenv('EMBED_URL',raising=False)
    monkeypatch.setattr(embed,'embed_text',lambda q:[1.0]*256)
    _, res = retrieve(s,SearchRequest(q='Find a person',scope='corpus'),vss=SimpleNamespace() if live else None)
    expected = 'live' if live else 'precomputed'
    assert res.envelope.exec_state==expected
    assert res.envelope.output['moments']
    assert all(m['exec_state']==expected for m in res.envelope.output['moments'])


def test_replay_marks_nested_results_and_hits_cached(tmp_path):
    import json
    from app.core import cache
    from app.tests.unit.a5_fakes import FIXTURES
    s = corpus_store(tmp_path)
    original = json.loads((FIXTURES/'runs/r_fixturewh0001.json').read_text())
    from app.core.models import AgentRun
    original = AgentRun.model_validate(original)
    rid = s.begin_run(original.q,original.context)
    s.add_evidence(rid,original.evidence)
    s.set_hits(rid,original.hits)
    s.finalize_run(rid,'done',original.claims,final=original.final)
    frames = json.loads((FIXTURES/'asks.json').read_text())['asks'][0]['events']
    cache.put(s,original.q,original.context,frames,'duckdb',rid)
    saved, replayed = cache.replay(s,original.q,original.context)
    assert saved.hits and all(m.exec_state=='cached' for m in saved.hits)
    for f in replayed:
        if f['event']=='tool_result':
            assert f['data']['envelope']['exec_state']=='cached'
        elif f['event']=='hits':
            assert all(m['exec_state']=='cached' for m in f['data']['moments'])


def test_street_relative_sidecar_fetched_by_asset_store_is_live(tmp_path):
    s = fixture_l1_store(tmp_path)
    m = s.manifest(L1_KEY)
    doc = s.asset_json(L1_KEY,f'dets/{L1_CAM}/0000.json')
    remote = SimpleNamespace(asset_dir=None,manifest=lambda key:m,asset_json=lambda *a:doc)
    assert street.detections(remote,L1_KEY,L1_CAM,0,5000)['exec_state']=='live'


def test_search_model_parse_is_live_even_without_candidates(tmp_path, monkeypatch):
    from app.core import llm
    s = corpus_store(tmp_path)
    monkeypatch.delenv('EMBED_URL',raising=False)
    monkeypatch.setenv('LLM_URL','http://llm.test')
    monkeypatch.setattr(llm,'complete_json',lambda *a,**kw:{'template':'unsupported','entities':[]})
    _, res = retrieve(s,SearchRequest(q='Could you evaluate this scene?',workspace='warehouse'))
    assert res.envelope.output['moments']==[]
    assert res.envelope.exec_state=='live'


MOMENT_CAP_CASES = [
    ('Was anyone at a distance less than 2 meters from the forklift?',(3.,3.)),
    ('Was every worker at a distance less than 2 meters from the forklift?',(1.,1.)),
]


def moment_cap_run(tmp_path,monkeypatch,q,values):
    from app.tests.unit.test_agent import b1_candidates
    s = corpus_store(tmp_path)
    install_trace(monkeypatch)
    install_vlm(monkeypatch)
    b1_candidates(s,monkeypatch,values)
    real_search = tools.search
    def many(*args, **kwargs):
        res = real_search(*args,**kwargs)
        first = res.envelope.output['moments'][0]
        res.envelope.output['moments'] = [{**first,'moment_id':f'm_repeat_{i}'} for i in range(5)]
        return res
    monkeypatch.setattr(tools,'search',many)
    frames = list(agent.ask_frames(s,agent.AskRequest(q=q,context={'workspace':'warehouse','scene_key':WH},
                                                    budget={'tools':16})))
    assert len(s.get_run(frames[0]['data']['run_id']).claims)==3
    assert frames[-2]['data']['final']=='UNRESOLVED'
    return frames


@pytest.mark.parametrize('q,values',MOMENT_CAP_CASES)
def test_moment_cap_keeps_unchecked_quantifiers_unresolved_with_full_policy(tmp_path,monkeypatch,q,values):
    frames = moment_cap_run(tmp_path,monkeypatch,q,values)
    if 'every worker' in q:
        # Unknown universal coverage prompts extra camera inspections. These
        # consume all 16 tool calls before the cap alone can end the investigation.
        assert sum(f['event']=='tool_call' for f in frames)==16
        assert frames[-2]['data']['stop_reason']=='budget_exhausted'
    else:
        assert frames[-2]['data']['stop_reason']=='no_new_info'


@pytest.mark.parametrize('q,values',MOMENT_CAP_CASES)
def test_moment_cap_stops_with_no_new_info_without_extra_policy_views(tmp_path,monkeypatch,q,values):
    # Isolate MAX_MOMENTS from the independent adaptive-camera/tool-budget policy.
    monkeypatch.setattr(agent.policy,'plan_next',lambda state:{'candidates':state['candidates'],'stop_reason':'no_new_info'})
    frames = moment_cap_run(tmp_path,monkeypatch,q,values)
    assert sum(f['event']=='tool_call' for f in frames)<16
    assert frames[-2]['data']['stop_reason']=='no_new_info'
