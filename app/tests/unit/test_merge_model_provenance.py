"""Protect live/precomputed provenance across material-aware cache misses."""
import json
from types import SimpleNamespace

import pytest

from app.api.search import SearchRequest, retrieve
from app.core import llm, tools, vlm
from app.core.deadline import DeadlineExceeded
from app.core.models import CosmosRecord
from app.tests.unit.a5_fakes import fixture_store
from app.tests.unit.test_vlm import K, P1, F1, _published_c1


@pytest.mark.parametrize('source',['published','runtime','new_live'])
def test_cosmos_reports_the_source_actually_used_despite_existing_stale_file(tmp_path,monkeypatch,source):
    model='nvidia/cosmos3-nano-reasoner'
    identity={'model':model,'endpoint':'fixture','source':'sha256:'+'1'*64}
    monkeypatch.setattr(vlm,'_rev',lambda *a:'merged')
    record=_published_c1(4600)
    monkeypatch.setattr(vlm,'_cache_identity',lambda key:identity)
    monkeypatch.setattr(vlm,'_cache_dir',lambda:tmp_path)
    manifest={'t1_ms':10000,'analysis_rev':'r1','assets':{'cosmos':'cosmos/record.json'}}
    monkeypatch.setattr(vlm,'_manifest',lambda key:manifest)
    published={'records':[record], 'input_artifact_hashes':{'scene:source_identity':identity['source']}}
    monkeypatch.setattr(vlm,'_json',lambda *a:published if source=='published' else None)
    monkeypatch.setattr(vlm,'_live',lambda *a:CosmosRecord.model_validate(record))
    cached_identity=identity if source=='runtime' else {**identity,'source':'stale'}
    (tmp_path/(record['record_id']+'.json')).write_text(json.dumps({'identity':cached_identity,'record':record}))
    store=SimpleNamespace(manifest=lambda key:manifest)
    result=tools.situation(store,K,4600,[P1,F1])
    assert result.envelope.exec_state=={'published':'precomputed','runtime':'cached','new_live':'live'}[source]
    assert '_sva_exec_state' not in result.envelope.output['record']


def test_missing_ego_snapshot_does_not_block_live_distance_and_claim_whole_window(tmp_path,monkeypatch):
    store=fixture_store(tmp_path)
    old=store.asset_json
    def missing(key,path):
        if path.startswith('ego/'):
            raise FileNotFoundError('missing ego snapshot')
        return old(key,path)
    monkeypatch.setattr(store,'asset_json',missing)
    monkeypatch.setattr(tools.live_measure,'measure',lambda *a,**kw:{'value_m':7.5,'sigma_m':.3,'t_ms':3400,
                        'source_uris':['s3://fixture/detections'],'n_frames':1})
    result=tools.ego_distance(store,'fl-pie-fixture-01','dashcam',3000,4000,rows=[])
    assert result.envelope.exec_state=='live' and result.evidence[0].value==7.5
    assert result.envelope.output['precomputed'] is None and result.envelope.output['precomputed_gaps']
    assert result.envelope.coverage is None
    assert result.envelope.output['minimum_basis']=='valid observed samples'


def test_ego_live_deadline_is_not_swallowed_by_precomputed_fallback(tmp_path,monkeypatch):
    store=fixture_store(tmp_path)
    def expired(*a,**kw):
        raise DeadlineExceeded('budget exhausted')
    monkeypatch.setattr(tools.live_measure,'measure',expired)
    with pytest.raises(DeadlineExceeded):
        tools.ego_distance(store,'fl-pie-fixture-01','dashcam',3000,4000,rows=[])


def test_search_constraint_abstention_retains_live_parser_provenance(tmp_path,monkeypatch):
    store=fixture_store(tmp_path)
    monkeypatch.setenv('LLM_URL','http://fixture')
    monkeypatch.setattr(llm,'complete_json',lambda *a,**kw:{'template':'find','entities':['P1']})
    _,result=retrieve(store,SearchRequest(q='Could you evaluate this scene?',workspace='warehouse',
                                        scene_key=K,selection=['P2']))
    assert result.envelope.output['abstain']=='UNRESOLVED'
    assert result.envelope.output['reason']=='conflicting_entity_constraints'
    assert result.envelope.exec_state=='live'
