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
                        {"rows": [[3333, *r[1:]] for r in asset_json(key, rel)["rows"] if r[0] == 3300]}
                        if rel.startswith("boxes/") else asset_json(key, rel))
    _, found = search.retrieve(store, search.SearchRequest(
        q="When did the worker start to dodge the forklift, and how far was it?", workspace="warehouse", scene_key=K))
    card = found.envelope.output["moments"][0]
    tm = card["t0_ms"]  # agent.ask_frames takes tm from the search card
    assert tm == 3333
    gt = {"task": [None] * 300, "dist": np.zeros(300)}
    gt["dist"][60] = 10
    times = cb.wh_moments(gt, [event, {**event, "t0_ms": 5800}], 10000)
    assert times == [2000, 3300, 5800]
    records = [_published_c1(t) for t in times]
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: _published_doc(records) if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C1"])
    rec = vlm.situation(K, tm, [EntityRef.model_validate(a) for a in card["actor_refs"]], run=_Run("r1"))
    assert rec == CosmosRecord.model_validate(records[1])
    assert calls["bodies"] == [] and steps[-1]["exec_state"] == "precomputed"


def _published_doc(records):
    return {'records':records,'input_artifact_hashes':{'scene:source_identity':vlm._source_identity(K)}}


def _published_c1(t, *, key=K, cam="ceiling_00", ids=None, task="C1", rev=None):
    refs = ids if ids is not None else [P1, F1]
    rev = rev or vlm._rev(task, key)
    return {"record_id": make_record_id(key, task, t, None, [a.entity_id for a in refs], rev),
            "task": task, "t_ms": t, "cam_id": cam, "actor_refs": [a.model_dump() for a in refs],
            "input_frame_refs": [{"cam_id": cam, "t_ms": t}], "prefix_only": False, "model_rev": "nvidia/cosmos3-nano-reasoner",
            "prompt_rev": rev, "payload": {"label": "yielding", "reason": "makes way", "uncertainty": 0},
            "verification": {"status": "verified", "method": "fixture", "ref": None}}


@pytest.mark.parametrize("offset", [-251, -250, 250, 251])
def test_situation_published_time_bound(calls, monkeypatch, offset):
    published = _published_c1(3300)
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: _published_doc([published]) if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C1"])
    rec = vlm.situation(K, 3300 + offset, [F1, P1])
    assert (rec.record_id == published["record_id"]) == (abs(offset) <= 250)
    assert len(calls["bodies"]) == (abs(offset) > 250)


@pytest.mark.parametrize("change", [{"key": "wh-other"}, {"ids": [P1]},
                                   {"task": "C2"}, {"rev": "old"}])
def test_situation_nearby_record_must_match_the_request(calls, monkeypatch, change):
    published = _published_c1(3300, **change)
    scene_json = vlm._json
    monkeypatch.setattr(vlm, "_json", lambda key, rel: _published_doc([published]) if rel.startswith("cosmos/") else scene_json(key, rel))
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


@pytest.mark.parametrize("base", ["http://h:8101", "http://h:8101/", "http://h:8101/v1", "http://h:8101/v1/"])
def test_chat_url_gets_v1_like_llm(calls, monkeypatch, base):
    urls = []
    def post(url, **kw):
        urls.append(url)
        return httpx.Response(200, json=REC["C4"], request=httpx.Request("POST", url))
    monkeypatch.setattr(vlm.httpx, "post", post)
    monkeypatch.setenv("VLM_URL", base)
    vlm._chat([], "p", {}, None)
    assert urls == ["http://h:8101/v1/chat/completions"]


@pytest.mark.parametrize("body", [{"json": {"choices": []}}, {"json": {"choices": [{}]}}, {"content": b"<html>not json"}])
def test_chat_malformed_envelope_raises_unavailable(calls, monkeypatch, body):
    monkeypatch.setattr(vlm.httpx, "post", lambda url, **kw: httpx.Response(200, request=httpx.Request("POST", url), **body))
    with pytest.raises(vlm.VLMUnavailable):
        vlm._chat([], "p", {}, None)


def test_payload_builder_error_raises_unavailable(calls):
    out = json.loads(REC["C1"]["choices"][0]["message"]["content"])
    del out["uncertainty"]  # situation's payload builder reads it
    calls["queue"].append(_resp(json.dumps(out)))
    with pytest.raises(vlm.VLMUnavailable, match="uncertainty"):
        vlm.situation(K, 4600, [P1, F1])


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
    monkeypatch.setattr(vlm, "_json", lambda key, rel: _published_doc([published]) if rel.startswith("cosmos/") else scene_json(key, rel))
    calls["queue"].append(REC["C2"])
    rec = vlm.predict_next(K, 3333, F1)
    assert rec.t_ms == 3333 and len(calls["bodies"]) == 1


ST = "st-fixture_cam_1-clip01"
ST_CAM = "fixture_cam-1"


@pytest.fixture
def street_dir(tmp_path):
    """ASSET_DIR with the L1 street scene; video=True keeps its camera video, False leaves only the chunk (site form)."""
    import shutil
    def make(video=True):
        root = tmp_path / ("video" if video else "chunk")
        shutil.copytree(ASSETS.parent / "l1" / ST, root / ST)
        if not video:
            m = json.loads((root / ST / "manifest.json").read_text())
            m["cams"][0]["video"] = None
            (root / ST / "manifest.json").write_text(json.dumps(m))
            (root / ST / "video" / f"{ST_CAM}.mp4").unlink()
        return root
    return make


@pytest.mark.parametrize("video", [True, False])
def test_street_localize_uses_the_street_prompt(calls, street_dir, monkeypatch, video):
    monkeypatch.setenv("ASSET_DIR", str(street_dir(video)))
    calls["queue"].append(REC["C4"])
    r = vlm.localize(ST, ST_CAM, 2000, 7000, "when does a car pass the person")
    assert r.prompt_rev == vlm.PROMPT_REV["C4-street"] != vlm.PROMPT_REV["C4"]
    assert r.record_id == make_record_id(ST, "C4", 4500, ST_CAM, ["when does a car pass the person", "2000-7000"], vlm.PROMPT_REV["C4-street"])
    text = calls["bodies"][0]["messages"][0]["content"][-1]["text"]
    assert f"street camera {ST_CAM}" in text and not any(w in text for w in ["dashcam", "warehouse", "forklift", "P = person"])
    ts = [f.t_ms for f in r.input_frame_refs]
    # 4 fps from the camera video, or from the chunk when the camera has no video
    assert len(ts) == 20 and 2000 <= min(ts) and max(ts) < 7000 and _images(calls["bodies"][0]) == 20
    assert schema_errors(r.model_dump(), "cosmos.json", "/$defs/record") == []


def test_street_ground_objects_has_its_own_prompt(calls, street_dir, monkeypatch):
    monkeypatch.setenv("ASSET_DIR", str(street_dir()))
    calls["queue"].append(_resp(json.dumps({"objects": [{"label": "car", "box_2d": [400, 500, 600, 700]}]})))
    r = vlm.ground_objects(ST, ST_CAM, 5000, ["car"])
    assert r.prompt_rev == vlm.PROMPT_REV["C3-street"] and r.payload["objects"][0]["box_0_1000"] == [400, 500, 600, 700]
    text = calls["bodies"][0]["messages"][0]["content"][-1]["text"]
    assert "hide a person" not in text and "street camera" in text
    monkeypatch.setenv("ASSET_DIR", str(ASSETS))
    assert vlm._rev("C3", K) == vlm.PROMPT_REV["C3"]  # warehouse C3 keeps its revision


def test_site_street_chunk_is_the_cached_copy_or_a_presigned_url(monkeypatch, tmp_path):
    from app.core import street
    uri = "s3://bucket/team/clip_chunk_0000.mp4"
    monkeypatch.setenv("RUNS_DIR", str(tmp_path))
    monkeypatch.setattr(vlm, "_s3", lambda: type("S3", (), {"generate_presigned_url": lambda self, op, ExpiresIn, Params: f"https://s3/{Params['Bucket']}/{Params['Key']}"})())
    assert vlm._chunk(ST, uri) == "https://s3/bucket/team/clip_chunk_0000.mp4"
    cached = tmp_path / "street" / "chunks" / (street._digest(uri) + ".mp4")
    cached.parent.mkdir(parents=True)
    cached.write_bytes(b"mp4")
    assert vlm._chunk(ST, uri) == str(cached)


def test_site_mode_reads_local_assets_when_asset_dir_is_set(monkeypatch, tmp_path):
    from app.core import vlm
    monkeypatch.setenv("MODE", "site")
    monkeypatch.setenv("ASSET_DIR", str(tmp_path))
    monkeypatch.setattr(vlm, "_s3", lambda: pytest.fail("site mode with ASSET_DIR must not call S3"))
    assert vlm._asset("wh-x", "manifest.json") == str(tmp_path / "wh-x" / "manifest.json")


@pytest.mark.parametrize('change',['model','endpoint','release','source'])
def test_runtime_cache_invalidates_model_release_and_source(calls,monkeypatch,tmp_path,change):
    import shutil
    root=tmp_path/'assets'
    shutil.copytree(ASSETS/K,root/K)
    monkeypatch.setenv('ASSET_DIR',str(root))
    calls['queue'] += [REC['C4'],REC['C4']]
    a=vlm.localize(K,'ceiling_04',1500,7500,'test source identity')
    if change=='model':
        monkeypatch.setenv('VLM_MODEL','a-different-model')
    elif change=='endpoint':
        monkeypatch.setenv('VLM_URL','http://other-model/v1')
    elif change=='release':
        monkeypatch.setenv('EVIDENCE_REVISION','new-release')
    else:
        path=root/K/'video/ceiling_04.mp4'
        # Update bytes while preserving size and mtime: content, not metadata, is the identity.
        old=path.stat()
        raw=path.read_bytes()
        path.write_bytes(raw[:-1]+bytes([raw[-1]^1]))
        __import__('os').utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
    b=vlm.localize(K,'ceiling_04',1500,7500,'test source identity')
    assert len(calls['bodies'])==2
    cached=json.loads((tmp_path/'vlm_cache'/f'{b.record_id}.json').read_text())
    assert cached['identity']['source']==vlm._source_identity(K)
    assert cached['record']['model_rev']==b.model_rev


@pytest.mark.parametrize('mismatch',['model','source','missing_source'])
def test_published_cache_requires_positive_model_and_source_match(calls,monkeypatch,mismatch):
    published=_published_c1(3300)
    doc=_published_doc([published])
    if mismatch=='model':
        published['model_rev']='old-model'
    elif mismatch=='source':
        doc['input_artifact_hashes']['scene:source_identity']='sha256:'+'0'*64
    else:
        doc['input_artifact_hashes']={}
    original=vlm._json
    monkeypatch.setattr(vlm,'_json',lambda key,rel:doc if rel.startswith('cosmos/') else original(key,rel))
    calls['queue'].append(REC['C1'])
    record=vlm.situation(K,3300,[P1,F1])
    assert len(calls['bodies'])==1 and record.verification.status=='unverified'


def test_frame_decode_stops_at_deadline_and_releases_capture(calls,steps,monkeypatch):
    import numpy as np
    from app.core import deadline
    clock=[0.]
    released=[]
    class Capture:
        def set(self,*args): pass
        def read(self):
            clock[0]+=.3
            return True,np.zeros((360,640,3),dtype=np.uint8)
        def get(self,*args): return clock[0]*1000
        def release(self): released.append(True)
    monkeypatch.setattr(deadline.time,'monotonic',lambda:clock[0])
    monkeypatch.setattr(vlm.cv2,'VideoCapture',lambda src:Capture())
    run=_Run('r-deadline')
    run.deadline=.5
    with pytest.raises(vlm.VLMUnavailable,match='budget exhausted'):
        vlm.situation(K,4600,[P1,F1],run=run)
    assert released==[True] and calls['bodies']==[]


def test_remote_material_requires_release_and_chunks_do_not_reuse_old_release(monkeypatch,tmp_path):
    uri='s3://bucket/clip.mp4'
    manifest={'workspace':'street','cams':[{'id':'cam','video':None,'chunks':[{'uri':uri}]}],'assets':{}}
    monkeypatch.setattr(vlm,'_manifest',lambda key:manifest)
    monkeypatch.delenv('ASSET_DIR',raising=False)
    monkeypatch.delenv('EVIDENCE_REVISION',raising=False)
    assert vlm._source_identity(ST) is None
    monkeypatch.setenv('RUNS_DIR',str(tmp_path))
    monkeypatch.setattr(vlm,'_s3',lambda:type('S3',(),{'generate_presigned_url':lambda *a,**kw:'https://asset/clip.mp4'})())
    old=tmp_path/'street/chunks'/(vlm.street_digest(uri)+'.mp4')
    old.parent.mkdir(parents=True)
    old.write_bytes(b'old media')
    monkeypatch.setenv('EVIDENCE_REVISION','r2')
    assert vlm._source_identity(ST).startswith('sha256:')
    assert vlm._chunk(ST,uri)=='https://asset/clip.mp4'
