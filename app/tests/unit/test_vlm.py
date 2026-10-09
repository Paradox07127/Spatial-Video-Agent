"""vlm.py against the fixture scene, driven by responses recorded from ws :8101 (nvidia/cosmos3-nano-reasoner)."""
import json
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from app.core import vlm
from app.core.models import CosmosRecord, EntityRef, make_record_id
from pipeline.common.schema_check import schema_errors

K = "wh-sdg-fixture-01"
P1 = EntityRef(scene_key=K, entity_id="P1", kind="person")
F1 = EntityRef(scene_key=K, entity_id="F1", kind="forklift")
ASSETS = Path(__file__).resolve().parents[3] / "fixtures" / "sva-out"


def _resp(content: str) -> dict:
    return {"id": "chatcmpl-rec", "model": "nvidia/cosmos3-nano-reasoner", "usage": {},
            "choices": [{"index": 0, "message": {"role": "assistant", "content": content}, "finish_reason": "stop"}]}


# recorded 2026-10-09 with these inputs: situation(K, 4600, [P1, F1]), predict_next(K, 4600, F1), localize(K, ceiling_04, 1500, 7500)
REC = {
    "C1": _resp(json.dumps({"subjects": ["P1", "F1"], "action": "interaction",
                            "state_before": "The person is standing near the forklift, which is stationary.",
                            "state_after": "The forklift begins to move forward, and the person steps back slightly to maintain a safe distance.",
                            "visible_interactions": ["The forklift is moving forward, and the person is stepping back to avoid collision."],
                            "source_intervals": [], "label": "interaction",
                            "reason": "The forklift is moving forward, and the person is stepping back to avoid collision.",
                            "uncertainty": 0.0})),
    "C2": _resp(json.dumps({"subjects": ["F1"], "action": "continue",
                            "state_before": "F1 is moving forward and slightly to the right, maintaining its trajectory.",
                            "state_after": "F1 will continue moving forward and slightly to the right, maintaining its trajectory.",
                            "visible_interactions": [], "source_intervals": [], "actions": ["continue"],
                            "reason": "F1's movement is consistent with a straight path, as indicated by the steady progression of its position across the frames."})),
    "C4": _resp(json.dumps({"segments": [{"start_s": 1.52, "end_s": 7.25,
                                          "text": "The worker starts yielding at 1.52s and continues until 7.25s."}]})),
}


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Fake endpoint: answers each request with the queued response (a dict, an int status or an exception)."""
    monkeypatch.setenv("ASSET_DIR", str(ASSETS))
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("VLM_URL", "http://vlm.test/v1")
    monkeypatch.setenv("VLM_MODEL", "nvidia/cosmos3-nano-reasoner")
    monkeypatch.setattr(vlm, "_fails", {})
    log = {"bodies": [], "queue": []}

    def post(url, *, json, timeout, headers):
        log["bodies"].append(json)
        r = log["queue"].pop(0)
        if isinstance(r, Exception):
            raise r
        req = httpx.Request("POST", url)
        return httpx.Response(r, request=req) if isinstance(r, int) else httpx.Response(200, json=r, request=req)
    monkeypatch.setattr(vlm.httpx, "post", post)
    return log


def _images(body: dict) -> int:
    return sum(p["type"] == "image_url" for p in body["messages"][0]["content"])


def test_situation_parses_and_validates(calls):
    calls["queue"].append(REC["C1"])
    r = vlm.situation(K, 4600, [P1, F1])
    assert schema_errors(r.model_dump(), "cosmos.json", "/$defs/record") == []
    assert r.task == "C1" and r.payload == {"label": "interaction", "uncertainty": 0.0,
                                            "reason": "The forklift is moving forward, and the person is stepping back to avoid collision."}
    assert r.raw["subjects"] == ["P1", "F1"] and r.verification.status == "unverified"
    assert r.cam_id == "ceiling_00"  # observes P1 and F1 over the whole window
    ts = [f.t_ms for f in r.input_frame_refs]
    assert len(ts) == 24 and 1600 <= min(ts) and max(ts) < 7600
    body = calls["bodies"][0]
    assert _images(body) == 24 and body["response_format"]["type"] == "json_schema"
    assert "P1, F1" in body["messages"][0]["content"][-1]["text"]


@pytest.mark.parametrize("t", [4600, 1100])
def test_predict_next_sees_only_the_prefix(calls, t):
    calls["queue"].append(REC["C2"])
    r = vlm.predict_next(K, t, F1)
    ts = [f.t_ms for f in r.input_frame_refs]
    assert r.prefix_only and r.payload["actions"] == ["continue"] and r.payload["horizon_s"] == 2
    assert all(max(t - 2000, 0) <= x < t for x in ts) and r.payload["max_frame_ms"] == max(ts)
    assert _images(calls["bodies"][0]) == len(ts)
    assert schema_errors(r.model_dump(), "cosmos.json", "/$defs/record") == []


def test_records_are_cached_by_record_id(calls, tmp_path):
    calls["queue"].append(REC["C4"])
    a = vlm.localize(K, "ceiling_04", 1500, 7500, "when does the worker start yielding")
    b = vlm.localize(K, "ceiling_04", 1500, 7500, "when does the worker start yielding")
    assert a == b and len(calls["bodies"]) == 1 and (tmp_path / "vlm_cache" / f"{a.record_id}.json").is_file()
    assert a.t_ms == 4500 and a.payload["segments"][0]["start_s"] == 1.52


def test_situation_finds_the_batch_moment_at_the_agents_detection_time(calls, steps, monkeypatch, tmp_path):
    import numpy as np
    from app.api import search
    from app.tests.unit.a5_fakes import fixture_store
    from pipeline.common import cosmos_batch as cb

    store = fixture_store(tmp_path)
    asset_json = store.asset_json
    event = {"id": "E2", "type": "dodge", "t0_ms": 3300, "t1_ms": 5600, "actor_ids": ["P1", "F1"],
             "place_id": None, "severity": 1, "source": "gt", "params": {}, "clip_ids": [], "eids": []}
    monkeypatch.setattr(store, "asset_json", lambda key, rel: {"events": [event]} if rel == "events.json" else
                        {"rows": [[3267, *r[1:]] for r in asset_json(key, rel)["rows"] if r[0] == 3300]}
                        if rel.startswith("boxes/") else asset_json(key, rel))
    _, found = search.retrieve(store, search.SearchRequest(
        q="When did the worker start to dodge the forklift, and how far was it?", workspace="warehouse", scene_key=K))
    card = found.envelope.output["moments"][0]
    tm = card["t0_ms"]  # agent.ask_frames takes tm from the search card
    assert tm == 3267
    gt = {"task": [None] * 300, "dist": np.zeros(300)}
    gt["dist"][60] = 10
    times = cb.wh_moments(gt, [event, {**event, "t0_ms": 5800}], 10000)
    assert times == [2000, 3300, 5800]
    records = [_published_c1(t) for t in times]
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: {"records": records} if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C1"])
    rec = vlm.situation(K, tm, [EntityRef.model_validate(a) for a in card["actor_refs"]], run=_Run("r1"))
    assert rec == CosmosRecord.model_validate(records[1])
    assert calls["bodies"] == [] and steps[-1]["exec_state"] == "cached"


def _published_c1(t, *, key=K, cam="ceiling_00", ids=None, task="C1", rev=None):
    refs = ids if ids is not None else [P1, F1]
    rev = rev or vlm._rev(task, key)
    return {"record_id": make_record_id(key, task, t, None, [a.entity_id for a in refs], rev),
            "task": task, "t_ms": t, "cam_id": cam, "actor_refs": [a.model_dump() for a in refs],
            "input_frame_refs": [{"cam_id": cam, "t_ms": t}], "prefix_only": False, "model_rev": "m",
            "prompt_rev": rev, "payload": {"label": "yielding", "reason": "makes way", "uncertainty": 0},
            "verification": {"status": "verified", "method": "fixture", "ref": None}}


@pytest.mark.parametrize("offset", [-251, -250, 250, 251])
def test_situation_published_time_bound(calls, monkeypatch, offset):
    published = _published_c1(3300)
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: {"records": [published]} if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C1"])
    rec = vlm.situation(K, 3300 + offset, [F1, P1])
    assert (rec.record_id == published["record_id"]) == (abs(offset) <= 250)
    assert len(calls["bodies"]) == (abs(offset) > 250)


@pytest.mark.parametrize("change", [{"key": "wh-other"}, {"ids": [P1]},
                                   {"task": "C2"}, {"rev": "old"}])
def test_situation_nearby_record_must_match_the_request(calls, monkeypatch, change):
    published = _published_c1(3300, **change)
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: {"records": [published]} if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C1"])
    rec = vlm.situation(K, 3267, [P1, F1])
    assert rec.t_ms == 3267 and len(calls["bodies"]) == 1


def test_prompt_revision_is_per_task(calls, monkeypatch):
    calls["queue"] += [REC["C1"], REC["C2"], REC["C1"]]
    c1, c2 = vlm.situation(K, 4600, [P1, F1]), vlm.predict_next(K, 4600, F1)
    monkeypatch.setitem(vlm.PROMPT_REV, "C1", "px-test")
    c1b, c2b = vlm.situation(K, 4600, [P1, F1]), vlm.predict_next(K, 4600, F1)
    assert c1b.record_id != c1.record_id and c1b.prompt_rev == "px-test"
    assert c2b == c2 and c2.prompt_rev == "px-1" and len(calls["bodies"]) == 3  # C2 still found by its old record id
    assert vlm._rev("C1", "fl-pie-fixture-01") == vlm.PROMPT_REV["C1-fleet"]  # the fleet C1 prompt has its own revision


def test_invalid_output_raises(calls):
    calls["queue"].append(_resp(json.dumps({**json.loads(REC["C1"]["choices"][0]["message"]["content"]), "label": "collision"})))
    with pytest.raises(vlm.VLMUnavailable):
        vlm.situation(K, 4600, [P1, F1])


class _Run:
    def __init__(self, run_id):
        self.run_id = run_id


@pytest.fixture
def steps(monkeypatch):
    """Stand-in for trace.traced (A1): collects (name, state, exec_state, output)."""
    out = []

    @contextmanager
    def traced(run, name, inputs, *, state, parent_step_id=None):
        s = {"name": name, "state": state, "inputs": inputs}
        h = type("H", (), {"exec_state": lambda self, x: s.update(exec_state=x), "output": lambda self, x: s.update(output=x)})()
        yield h
        out.append(s)
    monkeypatch.setattr(vlm.trace, "traced", traced)
    return out


def test_run_records_vlm_substep(calls, steps):
    calls["queue"].append(REC["C4"])
    for _ in range(2):
        vlm.localize(K, "ceiling_04", 1500, 7500, "when does the worker start yielding", run=_Run("r1"))
    assert [(s["name"], s["exec_state"]) for s in steps] == [("vlm.C4", "live"), ("vlm.C4", "cached")]
    assert steps[0]["output"]["task"] == "C4"


def test_chat_timeout_follows_run_deadline(calls, steps, monkeypatch):
    timeouts = []
    def respond(request):
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json=REC["C1"])
    monkeypatch.setattr(vlm.httpx, "post", httpx.Client(transport=httpx.MockTransport(respond)).post)
    run = _Run("r1")
    run.deadline = time.monotonic() + 5
    vlm.situation(K, 4600, [P1, F1], run=run)
    assert 0 < timeouts[0] <= 5
    run.deadline = time.monotonic() - 1
    with pytest.raises(vlm.VLMUnavailable):
        vlm.situation(K, 4800, [P1, F1], run=run)
    assert len(timeouts) == 1  # an exhausted budget sends nothing
    vlm.situation(K, 4800, [P1, F1])
    assert timeouts[1] == vlm.TIMEOUT_S


def test_circuit_opens_after_two_failures(calls, steps):
    calls["queue"] += [httpx.ReadTimeout("slow"), 502]
    for _ in range(2):
        with pytest.raises(vlm.VLMUnavailable):
            vlm.situation(K, 4600, [P1, F1], run=_Run("r1"))
    with pytest.raises(vlm.VLMUnavailable, match="circuit open"):
        vlm.situation(K, 4600, [P1, F1], run=_Run("r1"))
    assert len(calls["bodies"]) == 2  # the open circuit sends nothing
    calls["queue"].append(REC["C1"])
    assert vlm.situation(K, 4600, [P1, F1], run=_Run("r2")).payload["label"] == "interaction"


def test_fleet_situation_prompt_is_the_px1_text(monkeypatch):
    seen = {}
    monkeypatch.setattr(vlm, "_call", lambda task, key, cam, t, refs, win, extra, prompt, *a, **k: seen.setdefault(key, prompt))
    fl = "fl-pie-fixture-01"
    vlm.situation(fl, 1000, [EntityRef(scene_key=fl, entity_id="P1", kind="person"), EntityRef(scene_key=fl, entity_id="V1", kind="vehicle")])
    assert "unrelated: no relation between them in this window." in seen[fl] and "forklift" not in seen[fl]


def test_prediction_never_takes_a_nearby_record(calls, monkeypatch):
    # C2 sees only the frames before its own t_ms, so a record made at another time is not a valid answer
    published = {**_published_c1(3300, ids=[F1], task="C2"), "prefix_only": True, "payload": {"actions": ["continue"]},
                 "record_id": make_record_id(K, "C2", 3300, None, ["F1", "h2"], vlm._rev("C2", K))}
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: {"records": [published]} if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C2"])
    rec = vlm.predict_next(K, 3333, F1)
    assert rec.t_ms == 3333 and len(calls["bodies"]) == 1


def test_site_mode_reads_local_assets_when_asset_dir_is_set(monkeypatch, tmp_path):
    from app.core import vlm
    monkeypatch.setenv("MODE", "site")
    monkeypatch.setenv("ASSET_DIR", str(tmp_path))
    monkeypatch.setattr(vlm, "_s3", lambda: pytest.fail("site mode with ASSET_DIR must not call S3"))
    assert vlm._asset("wh-x", "manifest.json") == str(tmp_path / "wh-x" / "manifest.json")
