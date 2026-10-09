"""cosmos_batch label merge and per-moment warehouse actor.  .venv/bin/python -m pytest pipeline/common/test_cosmos_batch.py -q"""
import numpy as np

from pipeline.common import cosmos_batch as cb


def _lab(key, t, task, eids, label):
    return cb._label(key, t, [{"scene_key": key, "entity_id": e, "kind": "person"} for e in eids], task, label, "r")


def test_a_batch_replaces_its_own_labels_and_keeps_other_scenes():
    old = [_lab("wh-a", 1000, "C1", ["P1", "F1"], "passing"), _lab("wh-a", 1000, "C2", ["F1"], "stop"),
           _lab("wh-b", 500, "C1", ["P2", "F1"], "yielding")]
    new = [_lab("wh-a", 1000, "C1", ["F1", "P1"], "interaction"), _lab("wh-a", 1000, "C1", ["P3", "F1"], "passing")]
    got = {(x["scene_key"], x["task"], x["t_ms"], tuple(sorted(r["entity_id"] for r in x["actor_refs"]))): x["label"]
           for x in cb.merge_labels(old, new)}
    assert got == {("wh-a", "C1", 1000, ("F1", "P1")): "interaction", ("wh-a", "C2", 1000, ("F1",)): "stop",
                   ("wh-a", "C1", 1000, ("F1", "P3")): "passing", ("wh-b", "C1", 500, ("F1", "P2")): "yielding"}


def _gt(worker_xy):
    n = len(worker_xy)
    return {"t_ms": np.array([cb.t_of(i) for i in range(n)]), "task": [None] * n, "wxy": np.array(worker_xy, float)}


def _track(eid, pts):
    return {"entity_id": eid, "cls": "person", "pts": [[t, x, y, 0.1, "observed"] for t, x, y in pts]}


def test_moment_actor_comes_from_the_event_then_the_person_nearest_the_gt_worker():
    gt = _gt([(5.0, 5.0)] * 90)  # 3 s, worker standing at (5, 5)
    tracks = [_track("P1", [(1000, 0.0, 0.0), (2000, 0.0, 0.0)]), _track("P2", [(1000, 5.4, 5.3), (2000, 9.0, 9.0)])]
    events = [{"type": "near_miss", "t0_ms": 1500, "actor_ids": ["F1", "P1"], "params": {"t_min_ms": 1500}}]
    assert cb.wh_actor(1500, events, tracks, gt) == "P1"
    assert cb.wh_actor(1000, events, tracks, gt) == "P2"
    assert cb.wh_actor(2000, events, tracks, gt) is None  # nearest root 5.7 m from the worker
    assert cb.wh_actor(1000, None, tracks, _gt([(np.nan, np.nan)] * 90)) is None


def test_staged_document_names_the_revision_of_each_task(tmp_path, monkeypatch):
    import json
    from app.core.models import CosmosRecord
    monkeypatch.setattr(cb, "OUT", tmp_path)
    rec = lambda task, rev: CosmosRecord.model_validate(  # noqa: E731
        {"record_id": "c_" + task, "task": task, "t_ms": 1000, "cam_id": "ceiling_00", "actor_refs": [], "input_frame_refs": [],
         "prefix_only": False, "model_rev": "m", "prompt_rev": rev, "payload": {},
         "verification": {"status": "unverified", "method": "x", "ref": None}})
    path = cb.stage_records("wh-a", [rec("C2", "px-1"), rec("C1", "px-2")], {})
    assert json.loads(path.read_text())["analysis_rev"] == "C1:px-2 C2:px-1"
    assert json.loads((path.parents[1] / cb.publish.PATCH).read_text())["models"]["vlm"].endswith("@C1:px-2 C2:px-1")


def test_eval_records_prefer_the_published_file_over_a_stale_staged_one(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(cb, "OUT", tmp_path)
    def doc(path, key, label):
        path.mkdir(parents=True)
        (path / f"{key}.json").write_text(json.dumps({"scene_key": key, "records": [{"task": "C1", "label": label}]}))
    doc(tmp_path / "wh-a" / "cosmos", "wh-a", "published")
    doc(tmp_path / "_tmp" / cb.CARD / "wh-a" / "cosmos", "wh-a", "staged")
    doc(tmp_path / "_tmp" / cb.CARD / "wh-b" / "cosmos", "wh-b", "staged-only")
    assert [(r["scene_key"], r["label"]) for r in cb.load_records("C1")] == [("wh-a", "published"), ("wh-b", "staged-only")]
