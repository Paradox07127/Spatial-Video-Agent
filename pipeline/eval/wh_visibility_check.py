"""Two separate visibility checks on SDG runs processed by depth_points.py + visibility.py (build plan §10.4).

  python pipeline/eval/wh_visibility_check.py RUN_DIR_NAME [RUN_DIR_NAME ...]

(a) Floor-cell self-check: random floor points inside each visible/occluded cell, classified from their own pixel's depth;
    agreement = share whose state equals the cell's state (the cell centre stands for the whole 0.5 m cell).
(b) Person geometric visibility: rays to the GT worker axis at HEIGHTS_M; visible when any ray reaches within BODY_M + tau of
    the axis point. Compared with "this camera has a GT 2D box of the worker": hit rate = P(visible | box),
    false alarm rate = P(visible | no box), coverage = share of samples with a decision.
"""
from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, publish, video  # noqa: E402
from pipeline.warehouse import sdg  # noqa: E402
from pipeline.warehouse.depth_points import CARD, CPU  # noqa: E402
from pipeline.warehouse.visibility import OCCLUDED, VISIBLE, cell_centers, floor_states, tau  # noqa: E402

SAMPLES_PER_CELL = 2
HEIGHTS_M = (0.3, 0.9, 1.5)
BODY_M = 0.3  # the axis point lies inside the body; the depth sees the body surface up to about this much nearer


def check_run(run: str, rng: np.random.Generator) -> dict:
    run_dir = sdg.SDG_ROOT / run
    out = publish.tmp_dir(CARD, sdg.scene_id(run).replace("/", "-").lower())
    part = json.loads((out / "manifest.part.json").read_text())
    vis = json.loads((out / "visibility.json").read_text())
    a, b = part["sources"]["depth_log_fit"]["a"], part["sources"]["depth_log_fit"]["b"]
    cams = {c["id"]: c for c in part["cams"]}
    centers = cell_centers(part["ground"])
    n_sec = math.ceil(video.probe(run_dir / f"{vis['cam_bit_order'][0]}.rgb.mp4")["duration_ms"] / 1000)
    idx = [round((s * 1000 + 500) * sdg.FPS / 1000) for s in range(n_sec)]
    r = {"cell_agree": 0, "cell_n": 0, "box_vis": 0, "box_n": 0, "nobox_vis": 0, "nobox_n": 0, "decided": 0, "n": 0}
    for k, cid in enumerate(vis["cam_bit_order"]):
        cam, res = cams[cid], part["sources"]["depth_residual_m"][cid]
        gray = sdg.frames(run_dir / f"{cid}.depth.mp4", idx)
        ag = sdg.agents(run_dir, cid)
        for s in range(len(gray)):
            st = np.frombuffer(base64.b64decode(vis["per_sec"][s]), np.uint8)[k::len(vis["cam_bit_order"])]
            cells = np.flatnonzero((st == VISIBLE) | (st == OCCLUDED))
            pts = np.repeat(centers[cells], SAMPLES_PER_CELL, 0)
            pts[:, :2] += rng.uniform(-0.5, 0.5, (len(pts), 2)) * vis["cell_m"]
            got = floor_states(cam, gray[s], pts, a, b, res)
            want = np.repeat(st[cells], SAMPLES_PER_CELL)
            ok = (got == VISIBLE) | (got == OCCLUDED)
            r["cell_agree"] += int((got[ok] == want[ok]).sum())
            r["cell_n"] += int(ok.sum())
            for w in (x for x in ag[idx[s]] if x["cls"] == "character"):
                seen = person_visible(cam, gray[s], np.asarray(w["world"]), a, b, res)
                r["n"] += 1
                if seen is None:
                    continue
                r["decided"] += 1
                key = "box" if w["box"] else "nobox"
                r[f"{key}_n"] += 1
                r[f"{key}_vis"] += int(seen)
    return r


def person_visible(cam: dict, gray: np.ndarray, feet: np.ndarray, a: float, b: float, res: float) -> bool | None:
    """True / False, or None when every in-view ray lands on a depth edge."""
    pts = feet + np.array([[0, 0, h] for h in HEIGHTS_M])
    uv = sdg.project(cam, pts)
    inside = sdg.in_front(cam, pts) & (uv[:, 0] >= 0) & (uv[:, 0] < cam["w"]) & (uv[:, 1] >= 0) & (uv[:, 1] < cam["h"])
    if not inside.any():
        return False
    u, v = uv[inside, 0].astype(int), uv[inside, 1].astype(int)
    edge = sdg.jumps(gray)[v, u]
    if edge.all():
        return None
    d_pt = np.linalg.norm(pts[inside] - sdg.center(cam), axis=1)
    reach = sdg.depth_m(gray[v, u], a, b) > d_pt - BODY_M - tau(res, d_pt, b)
    return bool((reach & ~edge).any())


def main() -> None:
    runs = sys.argv[1:]
    rng = np.random.default_rng(0)
    tot: dict[str, int] = {}
    with lease.acquire(cpu=CPU, owner=CARD):
        cv2.setNumThreads(CPU)
        for run in runs:
            r = check_run(run, rng)
            print(f"{sdg.scene_id(run)}: {r}", flush=True)
            for k, v in r.items():
                tot[k] = tot.get(k, 0) + v
    print(f"(a) floor-cell depth agreement = {tot['cell_agree'] / tot['cell_n']:.3f} (N={tot['cell_n']} samples, {len(runs)} runs)")
    print(f"(b) person geometric visibility vs GT 2D box: hit rate = {tot['box_vis'] / max(tot['box_n'], 1):.3f} "
          f"(N={tot['box_n']}), false alarm rate = {tot['nobox_vis'] / max(tot['nobox_n'], 1):.3f} (N={tot['nobox_n']}), "
          f"coverage = {tot['decided'] / tot['n']:.3f} (N={tot['n']} cam-seconds, {len(runs)} runs)")


if __name__ == "__main__":
    main()
