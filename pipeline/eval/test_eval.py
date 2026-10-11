"""latency ask timing and C2 ghost endpoint exclusion.  .venv/bin/python -m pytest pipeline/eval/test_eval.py -q"""
import json

import httpx
import numpy as np

from pipeline.eval import cosmos_prediction_acc, latency, wh_position_error

ASK = {"ask_id": "a1", "q": "q", "context": {"workspace": "fleet"}, "budget": {}}


def _sse(cached: bool, drained: list):
    def body():
        for ev, data in (("step", {"run_id": "r"}), ("status", {"run_id": "r", "cached": cached}), ("done", {"run_id": "r"})):
            yield f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode()
        drained.append(True)  # the server stores its cache entry only after the last frame is taken
    return httpx.MockTransport(lambda req: httpx.Response(200, content=body(), headers={"content-type": "text/event-stream"}))


def test_ask_is_read_to_done_and_reports_the_cache_flag():
    for cached in (False, True):
        drained = []
        with httpx.Client(transport=_sse(cached, drained)) as c:
            first, total, got = latency.timed_ask(c, "http://x", ASK)
        assert drained == [True] and 0 <= first <= total and got is cached


def test_ghost_endpoint_past_the_run_end_is_excluded_not_clamped():
    n = 90  # 3 s of GT
    gt = {"task": [None] * n, "xy": np.stack([np.arange(n) * 0.1, np.zeros(n)], 1)}
    recs = [{"scene_key": "wh-a", "payload": {"ghost": [[1000, 3.0, 0.0], [2000, 6.0, 0.0]]}},
            {"scene_key": "wh-a", "payload": {"ghost": [[2000, 6.0, 0.0], [4000, 12.0, 0.0]]}}]
    err, excluded = cosmos_prediction_acc.ghost_errors(recs, lambda key: gt)
    assert excluded == 1 and err == [abs(6.0 - 60 * 0.1)]


def test_sigma_stats_recovers_isotropic_gaussian_sigma():
    s = wh_position_error.sigma_stats(np.random.default_rng(0).normal(0, 0.2, (20000, 2)))
    assert s["n"] == 20000
    for k, want in (("sigma_axis_rms", 0.2), ("sigma_axis_mad", 0.2), ("radial_median", 0.2 * np.sqrt(2 * np.log(2)))):
        assert abs(s[k] / want - 1) < 0.03, k


def _worker(x: float, y: float, box: list) -> dict:
    b3 = {"transform": np.eye(4).tolist(), "x_min": x - 0.2, "x_max": x + 0.2, "y_min": y - 0.2, "y_max": y + 0.2, "z_min": 0.0}
    b2 = dict(zip(("x_min", "y_min", "x_max", "y_max"), box))
    return {"w": {"metro_agent_data": {"agent_group": "Worker"},
                  "annotators": {"bounding_box_2d_tight_fast": b2, "bounding_box_3d_fast": b3}}}


def test_production_rows_carry_the_published_method_of_the_gt_matched_person():
    box = [100, 100, 150, 250]
    gts = {"c0": [_worker(1.0, 2.0, box)] * 3 + [_worker(0.5, 3.0, box)]}  # 30 fps: t=100 ms is GT frame 3
    poses = {"c0": {"frames": [[0, [[1, "person", *box], [2, "person", 400, 100, 450, 250]]],
                               [100, [[1, "person", *box]]]]}}
    owner = {("c0", 1): "p1", ("c0", 2): "p2"}
    sk = {"p1": {"t_ms": [0, 100], "root_world": [[1.1, 2.0], [0.0, 3.0]], "root_method": ["tri", "boxbottom"]},
          "p2": {"t_ms": [0], "root_world": [[5.0, 5.0]], "root_method": ["ankle"]}}
    rows = wh_position_error.pair_production(sk, owner, poses, gts, [np.array([1.0, -2.0])])
    assert [r["method"] for r in rows] == ["tri", "boxbottom"]
    assert np.allclose([(r["dx"], r["dy"]) for r in rows], [(0.1, 0.0), (-0.5, 0.0)])
    assert np.isclose(rows[0]["dist"], 4.0)
