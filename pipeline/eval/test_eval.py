"""latency ask timing and C2 ghost endpoint exclusion.  .venv/bin/python -m pytest pipeline/eval/test_eval.py -q"""
import json

import httpx
import numpy as np

from pipeline.eval import cosmos_prediction_acc, latency

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
