"""W0 signatures are the contract between cards: implementations must keep these exact signatures."""
import inspect

import pytest

from app.api import actions
from app.core import models, store, trace, vlm
from pipeline.common import lease, paths, publish, video

from .conftest import load

SIGS = {
    (store.Store, "__init__"): "(self, *, index_path: 'Path | None', app_path: 'Path', asset_dir: 'Path | None', backend: \"Literal['duckdb', 'vastdb']\" = 'duckdb', allow_duckdb_fallback: 'bool' = False) -> 'None'",
    (store.Store, "search_clips"): "(self, filters: 'SearchFilters', qvec_txt: 'list[float] | None', qvec_vis: 'list[float] | None') -> 'list[Clip]'",
    (store.Store, "events_in"): "(self, scene_key: 'str', t0_ms: 'int', t1_ms: 'int') -> 'list[Event]'",
    (store.Store, "moment"): "(self, scene_key: 'str', t_ms: 'int') -> 'Moment'",
    (store.Store, "entities"): "(self, scene_key: 'str') -> 'list[Entity]'",
    (store.Store, "index_counts"): "(self) -> 'dict[str, int]'",
    (store.Store, "manifest"): "(self, scene_key: 'str') -> 'dict'",
    (store.Store, "asset_json"): "(self, scene_key: 'str', relpath: 'str') -> 'Any'",
    (store.Store, "asset_url"): "(self, scene_key: 'str', relpath: 'str') -> 'str'",
    (store.Store, "list_scenes"): "(self, workspace: 'str | None' = None) -> 'list[dict]'",
    (store.Store, "begin_run"): "(self, q: 'str', ctx: 'dict', *, kind: \"Literal['ask', 'watch']\" = 'ask', source_run_id: 'str | None' = None) -> 'str'",
    (store.Store, "append_step"): "(self, run_id: 'str', step: 'RunStep') -> 'None'",
    (store.Store, "update_step"): "(self, run_id: 'str', step: 'RunStep') -> 'None'",
    (store.Store, "add_evidence"): "(self, run_id: 'str', evidence: 'list') -> 'None'",
    (store.Store, "set_hits"): "(self, run_id: 'str', hits: 'list[MomentCard]') -> 'None'",
    (store.Store, "finalize_run"): "(self, run_id: 'str', status: \"Literal['done', 'error', 'aborted']\", claims: 'list[Claim]', *, final: 'Final | None', weave_url: 'str | None' = None, elapsed_ms: 'int | None' = None) -> 'None'",
    (store.Store, "get_run"): "(self, run_id: 'str') -> 'AgentRun | None'",
    (store.Store, "list_runs"): "(self, scene_key: 'str | None' = None, limit: 'int' = 20) -> 'list[dict]'",
    (store.Store, "cache_get"): "(self, qhash: 'str') -> 'dict | None'",
    (store.Store, "cache_put"): "(self, qhash: 'str', q: 'str', context: 'dict', response: 'dict', backend: 'str', source_run_id: 'str') -> 'None'",
    (store.Store, "find_action"): "(self, scene_key: 'str', event_id: 'str') -> 'Action | None'",
    (store.Store, "insert_action"): "(self, action: 'Action') -> 'str'",
    (store.Store, "list_actions"): "(self, scene_key: 'str') -> 'list[Action]'",
    (store.Store, "insert_zone"): "(self, zone: 'Zone') -> 'str'",
    (store.Store, "list_zones"): "(self, scene_key: 'str') -> 'list[Zone]'",
    (store.Store, "write_vastdb"): "(self, tables: 'dict[str, Any]') -> 'dict[str, int]'",
    (trace, "start_run"): "(store: 'Any', q: 'str', context: 'dict', *, kind: \"Literal['ask', 'watch']\" = 'ask', source_run_id: 'str | None' = None) -> 'RunHandle'",
    (trace, "traced"): "(run: 'RunHandle', name: 'str', inputs: 'dict', *, state: 'AgentState', parent_step_id: 'str | None' = None) -> 'Iterator[StepHandle]'",
    (trace, "finish_run"): "(run: 'RunHandle', status: \"Literal['done', 'error', 'aborted']\", *, final: 'Final | None', claims: 'list[Claim]') -> 'str | None'",
    (trace, "flush_outbox"): "(max_items: 'int' = 100) -> 'int'",
    (vlm, "situation"): "(scene_key: 'str', t_ms: 'int', actor_refs: 'list[EntityRef]', *, run: 'Any' = None) -> 'CosmosRecord'",
    (vlm, "predict_next"): "(scene_key: 'str', t_ms: 'int', actor_ref: 'EntityRef', horizon_s: 'float' = 2, *, run: 'Any' = None) -> 'CosmosRecord'",
    (vlm, "ground_objects"): "(scene_key: 'str', cam: 'str', t_ms: 'int', vocab: 'list[str]', *, run: 'Any' = None) -> 'CosmosRecord'",
    (vlm, "localize"): "(scene_key: 'str', cam: 'str', t0_ms: 'int', t1_ms: 'int', query: 'str', *, run: 'Any' = None) -> 'CosmosRecord'",
    (vlm, "describe_region"): "(scene_key: 'str', cam: 'str', t_ms: 'int', region_ref: 'str', *, run: 'Any' = None) -> 'CosmosRecord'",
    (actions, "trigger"): "(store, req: 'TriggerRequest') -> 'TriggerResult'",
    (lease, "acquire"): f"(cpu: 'int', gpu: 'int' = 0, *, owner: 'str', timeout_s: 'float | None' = None, root: 'Path' = {paths.OUT / '_lease'!r}) -> 'Lease'",
    (lease, "vlm_slot"): f"(*, owner: 'str', root: 'Path' = {paths.OUT / '_lease'!r}) -> 'Iterator[None]'",
    (publish, "tmp_dir"): f"(card: 'str', scene_key: 'str', *, out_root: 'Path' = {paths.OUT!r}) -> 'Path'",
    (publish, "publish"): f"(card: 'str', scene_key: 'str', *, out_root: 'Path' = {paths.OUT!r}, check: 'bool' = True) -> 'Path'",
    (video, "probe"): "(src: 'Path') -> 'dict'",
    (video, "transcode"): "(src: 'Path', dst: 'Path', *, gop_s: 'float' = 1.0, crf: 'int' = 23, max_w: 'int | None' = None) -> 'Path'",
    (video, "thumbs"): "(src: 'Path', out_dir: 'Path', *, offset_ms: 'int' = 0, width: 'int' = 320) -> 'list[Path]'",
    (video, "sprite"): "(src: 'Path', dst: 'Path', *, every_s: 'float' = 1.0, tile_w: 'int' = 160, cols: 'int' = 10) -> 'dict'",
    (video, "frames_at"): "(src: 'Path | str', media_ms: 'list[int]', *, width: 'int | None' = None) -> 'list[bytes]'",
}


@pytest.mark.parametrize("owner,name", list(SIGS), ids=[f"{getattr(o, '__name__', o)}.{n}" for o, n in SIGS])
def test_signature(owner, name):
    fn = getattr(owner, name)
    fn = getattr(fn, "__wrapped__", fn)
    assert str(inspect.signature(fn)) == SIGS[(owner, name)]


def test_constants():
    assert (lease.CPU_TOTAL, lease.GPU_HOLDERS, lease.VLM_CONCURRENCY) == (48, 1, 8)
    assert trace.INLINE_LIMIT_BYTES == 8192
    assert issubclass(vlm.VLMUnavailable, RuntimeError)


def test_models_parse_fixture():
    """Pydantic contract models accept every fixture object the schemas accept."""
    run = models.AgentRun.model_validate(load("runs/r_fixturewh0001.json"))
    assert run.final == "SUPPORTED" and run.steps and run.hits
    for ask in load("asks.json")["asks"]:
        for f in ask["events"]:
            d = {k: v for k, v in f["data"].items() if k != "run_id"}
            if f["event"] == "tool_result":
                models.Envelope.model_validate(d["envelope"])
                [models.Evidence.model_validate(e) for e in d["evidence"]]
            elif f["event"] == "hits":
                [models.MomentCard.model_validate(m) for m in d["moments"]]
            elif f["event"] == "claim":
                models.Claim.model_validate(f["data"])
            elif f["event"] == "model_result":
                [models.CosmosRecord.model_validate(r) for r in d["results"]]


def test_eid_rules():
    o = [models.ObservationRef(scene_key="k", cam_id="c", local_track_id=1, t_ms=5)]
    a = models.make_eid("k", "c", 0, 10, o, "rev", "m1")
    assert a == models.make_eid("k", "c", 0, 10, list(reversed(o)), "rev", "m1")
    assert a != models.make_eid("k", "c", 0, 10, o, "rev", "m2")
    assert a != models.make_eid("k", "c", 0, 10, o, "rev2", "m1")
    e = models.Evidence.create(scene_key="k", cam_id="c", t0_ms=0, t1_ms=10, observation_ids=o, analysis_rev="rev", kind="measure",
                               method="m1", value=1.5, unit="m")
    assert e.eid == a
    with pytest.raises(Exception):
        e.value = 2  # evidence is immutable


def test_aggregate_rules():
    P = lambda s: models.Predicate(predicate_id="p", subject_refs=[], operator="lt", value=1, unit=None, status=s, eids=[])  # noqa: E731
    assert models.aggregate_of([P("supported"), P("supported")]) == "SUPPORTED"
    assert models.aggregate_of([P("supported"), P("contradicted")]) == "CONTRADICTED"
    assert models.aggregate_of([P("supported"), P("unknown")]) == "UNRESOLVED"
    assert models.aggregate_of([]) == "UNRESOLVED"
