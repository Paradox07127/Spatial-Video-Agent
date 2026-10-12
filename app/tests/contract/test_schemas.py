"""Every schema is valid; the fixture passes all schemas and cross-file checks; the checks catch broken references."""
import base64
import copy
import json
import shutil

import pytest
from jsonschema import Draft202012Validator

from pipeline.common import schema_check as sc

from .conftest import FIXTURES, SVA, SVA_OUT, WH_KEY, load

EXPECTED = {"manifest", "entities", "timeline", "events", "boxes", "skeleton", "visibility", "forklift", "tracks", "cosmos",
            "trip", "ego", "buildings", "search", "ask_sse", "ask_sse_v2", "ui_cmd", "agent_run", "situation_map", "dets2d"}
L1 = FIXTURES / "l1"
ST_KEY = "st-fixture_cam_1-clip01"


def test_all_contract_schemas_are_valid():
    names = {p.stem for p in (SVA / "schemas").glob("*.json")}
    assert names == EXPECTED
    for n in names:
        Draft202012Validator.check_schema(json.loads((SVA / "schemas" / f"{n}.json").read_text()))


def test_fixture_scenes_pass():
    scenes = sc.scene_dirs(SVA_OUT)
    assert [s.name for s in scenes] == ["fl-pie-fixture-01", "fl-pie-fixture-02", WH_KEY]
    for s in scenes:
        c = sc.SceneCheck(s).run()
        assert c.errors == [], c.errors
    assert [s.name for s in sc.scene_dirs(L1)] == [ST_KEY]
    c = sc.SceneCheck(L1 / ST_KEY).run()
    assert c.errors == [] and c.files == 2, c.errors
    errs, notes = sc.check_fixture_extras(FIXTURES)
    assert errs == [] and len(notes) == 6 and "l1/asks_corpus.json: 3 asks" in notes


def test_fixture_requirements():
    """W0 fixture content promised in §14.2."""
    m = load(f"sva-out/{WH_KEY}/manifest.json")
    assert "-" in m["scene_key"] and m["scene_key"] == sc.scene_key_of(m["scene_id"])
    assert any(c["offset_ms"] != 0 for c in m["cams"])
    sk = load(f"sva-out/{WH_KEY}/skeleton.json")["persons"]
    p1, p2 = set(sk["P1"]["t_ms"]), set(sk["P2"]["t_ms"])
    assert len(p1 & p2) > 50, "two people present at the same time"
    forced = [(x, m) for t, x, m in zip(sk["P2"]["t_ms"], sk["P2"]["xyz"], sk["P2"]["root_method"]) if 2000 <= t < 2300]
    assert forced and all(x is None and m == "ankle" for x, m in forced), "single-view frames carry root only"
    lanes = load(f"sva-out/{WH_KEY}/timeline.json")["lanes"]
    assert ["6000", "7000"] == [str(v) for v in [s for s in lanes["eye_00"] if s[2] == "no_video"][0][:2]]
    assert any(e["severity"] == 2 for e in load(f"sva-out/{WH_KEY}/events.json")["events"])
    gps = [f["properties"] for f in load("sva-out/fl-pie-fixture-01/trip.geojson")["features"][1:]]
    assert any(p["state"] == "last_known" and p["gap_ms"] > 2000 for p in gps)
    assert gps[-1]["t_ms"] >= 9500


@pytest.fixture
def scene_copy(tmp_path):
    dst = tmp_path / WH_KEY
    shutil.copytree(SVA_OUT / WH_KEY, dst, ignore=shutil.ignore_patterns("*.mp4", "*.jpg"))
    for c in ("ceiling_00", "ceiling_04", "eye_00"):
        (dst / "video").mkdir(exist_ok=True)
        (dst / f"video/{c}.mp4").write_bytes(b"")
        (dst / f"thumbs/{c}").mkdir(parents=True, exist_ok=True)
        (dst / f"thumbs/{c}/00000.jpg").write_bytes(b"")
    assert sc.SceneCheck(dst).run().errors == []
    return dst


def _edit(path, fn):
    d = json.loads(path.read_text())
    fn(d)
    path.write_text(json.dumps(d))


@pytest.mark.parametrize("case", ["unknown_entity", "c2_future_frame", "box_without_entity", "visibility_bytes",
                                  "scene_key_mismatch", "points_size", "fov"])
def test_cross_checks_catch(scene_copy, case):
    s = scene_copy
    if case == "unknown_entity":
        _edit(s / "events.json", lambda d: d["events"][0]["actor_ids"].append("P9"))
    elif case == "c2_future_frame":
        def f(d):
            r = next(r for r in d["records"] if r["task"] == "C2")
            r["input_frame_refs"].append({"cam_id": "ceiling_04", "t_ms": r["t_ms"] + 250})
        _edit(s / f"cosmos/{WH_KEY}.json", f)
    elif case == "box_without_entity":
        _edit(s / "boxes/ceiling_00/00000.json", lambda d: d["rows"].append([100, 99, "person", 1, 1, 50, 50, 0.9]))
    elif case == "visibility_bytes":
        _edit(s / "visibility.json", lambda d: d["per_sec"].__setitem__(0, base64.b64encode(b"\x01" * 10).decode()))
    elif case == "scene_key_mismatch":
        _edit(s / "manifest.json", lambda d: d.__setitem__("scene_key", "wh-sdg-other"))
    elif case == "points_size":
        (s / "points.bin").write_bytes((s / "points.bin").read_bytes()[:-3])
    elif case == "fov":
        _edit(s / "manifest.json", lambda d: d["cams"][0].__setitem__("fov_deg", 50.0))
    assert sc.SceneCheck(s).run().errors, case


def test_schema_rejects_extra_and_missing_fields():
    m = load(f"sva-out/{WH_KEY}/manifest.json")
    bad = copy.deepcopy(m)
    bad["cams"][0]["camera_basis"] = "opencv"
    assert sc.schema_errors(bad, "manifest.json")
    bad = copy.deepcopy(m)
    del bad["analysis_rev"]
    assert sc.schema_errors(bad, "manifest.json")
    ev = load(f"sva-out/{WH_KEY}/events.json")
    ev["events"][0]["surprise"] = 1
    assert sc.schema_errors(ev, "events.json")


def test_box_bottom_roots_are_not_publishable():
    sk = load(f"sva-out/{WH_KEY}/skeleton.json")
    sk["persons"][next(iter(sk["persons"]))]["root_method"][0] = "boxbottom"
    assert sc.schema_errors(sk, "skeleton.json")
    tr = load(f"sva-out/{WH_KEY}/tracks.json")
    tr["tracks"][0]["source"] = "boxbottom"
    assert sc.schema_errors(tr, "tracks.json")


@pytest.fixture
def street_copy(tmp_path):
    dst = tmp_path / ST_KEY
    shutil.copytree(L1 / ST_KEY, dst)
    assert sc.SceneCheck(dst).run().errors == []
    return dst


@pytest.mark.parametrize("case", ["row_outside_chunk", "chunk_index_mismatch", "chunk_missing", "chunks_overlap"])
def test_street_checks_catch(street_copy, case):
    s = street_copy
    dets = s / "dets/fixture_cam-1/0000.json"
    if case == "row_outside_chunk":
        _edit(dets, lambda d: d["rows"].append([10000, "car", 1, 1, 50, 50, 0.9]))
    elif case == "chunk_index_mismatch":
        _edit(dets, lambda d: d.__setitem__("chunk_index", 1))
    elif case == "chunk_missing":
        (s / "chunks/fixture_cam-1/0000.mp4").unlink()
    elif case == "chunks_overlap":
        _edit(s / "manifest.json",
              lambda d: d["cams"][0]["chunks"].append({"t0_ms": 5000, "t1_ms": 10000, "uri": "chunks/fixture_cam-1/0000.mp4"}))
    assert sc.SceneCheck(s).run().errors, case


def test_street_manifest_has_no_geometry():
    m = load(f"l1/{ST_KEY}/manifest.json")
    assert sc.schema_errors(m, "manifest.json") == []
    for edit in (lambda d: d["cams"][0].__setitem__("K", [500.0, 500.0, 320.0, 180.0]),
                 lambda d: d["cams"][0].pop("kind"),
                 lambda d: d.__setitem__("metric", True),
                 lambda d: d["assets"].pop("dets_dir")):
        bad = copy.deepcopy(m)
        edit(bad)
        assert sc.schema_errors(bad, "manifest.json")
    wh = load(f"sva-out/{WH_KEY}/manifest.json")
    wh["cams"][0]["kind"] = "cam2d"
    assert sc.schema_errors(wh, "manifest.json"), "a warehouse camera is never 2D"


def test_prefix_must_match_workspace(scene_copy):
    st = scene_copy.rename(scene_copy.parent / "st-sdg-fixture-01")
    _edit(st / "manifest.json", lambda d: d.update(scene_id="st/sdg/fixture-01", scene_key="st-sdg-fixture-01"))
    assert sc.schema_errors(json.loads((st / "manifest.json").read_text()), "manifest.json") == []
    errs = sc.SceneCheck(st).run().errors
    assert any("prefix" in e for e in errs), errs


def test_moment_card_level_fields_are_optional():
    mc = next(f for f in load("asks.json")["asks"][0]["events"] if f["event"] == "hits")["data"]["moments"][0]
    assert "workspace" not in mc and sc.schema_errors(mc, "ask_sse.json", "/$defs/moment_card") == []
    new = {**mc, "workspace": "street", "evidence_level": "L1", "actor_refs": []}
    assert sc.schema_errors(new, "ask_sse.json", "/$defs/moment_card") == []
    assert sc.schema_errors({**new, "evidence_level": "L4"}, "ask_sse.json", "/$defs/moment_card")
    assert sc.schema_errors({**new, "tag": "fixed", "title": "San Francisco street camera 4 (sf4)"}, "ask_sse.json", "/$defs/moment_card") == []
    assert sc.schema_errors({**new, "tag": "mobile"}, "ask_sse.json", "/$defs/moment_card")
    assert sc.schema_errors({**new, "tag": "street"}, "ask_sse.json", "/$defs/moment_card")


def test_hits_auto_open_and_search_sources():
    frames = load("asks.json")["asks"][0]["events"]
    hits = next(f for f in frames if f["event"] == "hits")
    assert sc.check_sse_sequence([f if f is not hits else {**hits, "data": {**hits["data"], "auto_open": None}} for f in frames]) == []
    assert sc.schema_errors({**hits, "data": {**hits["data"], "auto_open": "Bad Key"}}, "ask_sse.json")
    assert sc.schema_errors({"q": "a car", "sources": ["fixed"]}, "search.json", "/$defs/request") == []
    assert sc.schema_errors({"q": "a car", "sources": ["mobile"]}, "search.json", "/$defs/request")
    assert sc.schema_errors({"q": "a car", "sources": []}, "search.json", "/$defs/request")
    assert sc.schema_errors({"q": "a car", "sources": ["street"]}, "search.json", "/$defs/request")


def test_claim_abstain_and_ui_scene_key():
    claim = next(f for f in load("asks.json")["asks"][0]["events"] if f["event"] == "claim")["data"]
    ok = {**claim, "scene_key": WH_KEY, "moment_id": "m1", "evidence_level": "L3",
          "explain": {"c1": "c_x", "abstain": {"reason": "no_geometry", "text": "No geometry."}}}
    assert sc.schema_errors(ok, "ask_sse.json", "/$defs/claim") == []
    bad = copy.deepcopy(ok)
    bad["explain"]["abstain"]["reason"] = "too_dark"
    assert sc.schema_errors(bad, "ask_sse.json", "/$defs/claim")
    assert sc.schema_errors({"cmd": "seek", "args": {"t_ms": 0}, "scene_key": ST_KEY}, "ui_cmd.json") == []


def test_search_request_scope():
    assert sc.schema_errors({"q": "x", "workspace": "street"}, "search.json", "/$defs/request") == []
    assert sc.schema_errors({"q": "x", "scope": "corpus"}, "search.json", "/$defs/request") == []
    assert sc.schema_errors({"q": "x"}, "search.json", "/$defs/request")
    assert sc.schema_errors({"q": "x", "scope": "scene"}, "search.json", "/$defs/request")
