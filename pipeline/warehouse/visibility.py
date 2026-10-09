"""Floor cell visible to each display camera, per second (visibility.json, CONTRACT §4.2). Not person visibility.

  python pipeline/warehouse/visibility.py RUN_DIR_NAME   (after depth_points.py; reads its manifest.part.json)

Second s uses the frame at s*1000+500 ms. A cell centre on z=0 is visible when the observed range at its pixel matches the
floor range, |d_obs - d_floor| < tau with tau = max(0.25 m, 2 x the camera's floor residual, 2 gray levels at d_floor);
occluded when something is nearer by more than tau; unknown on depth edges or when the observed range is beyond the floor.
The gray-level term exists because depth.mp4 is 8-bit log range: one level is d/b (about 0.3 m at 11 m) and H.264 rings
at every level step, so a fixed 0.25 m marks the floor as occluded along iso-range arcs beyond about 9 m.
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
from pipeline.warehouse.depth_points import ANALYSIS_REV, CARD, CPU, producer_commit, sha256  # noqa: E402

UNKNOWN, VISIBLE, OCCLUDED, OUT_OF_FOV, NO_VIDEO = range(5)
TAU_MIN_M = 0.25


def tau(residual_m: float, d: np.ndarray, b: float) -> np.ndarray:
    """Match tolerance in m at range d (b of the run's log depth fit)."""
    return np.maximum(max(TAU_MIN_M, 2 * residual_m), 2 * d / b)


def cell_centers(ground: dict) -> np.ndarray:
    """(ny*nx, 3) cell centres on z=0, row-major (iy, ix)."""
    ox, oy = ground["origin"]
    c = ground["cell_m"]
    xs, ys = np.meshgrid(ox + (np.arange(ground["nx"]) + 0.5) * c, oy + (np.arange(ground["ny"]) + 0.5) * c)
    return np.c_[xs.ravel(), ys.ravel(), np.zeros(xs.size)]


def floor_states(cam: dict, gray: np.ndarray, pts: np.ndarray, a: float, b: float, residual_m: float) -> np.ndarray:
    """State code per world point on the floor for one depth frame (gray at source resolution)."""
    uv = sdg.project(cam, pts)
    inside = sdg.in_front(cam, pts) & (uv[:, 0] >= 0) & (uv[:, 0] < cam["w"]) & (uv[:, 1] >= 0) & (uv[:, 1] < cam["h"])
    out = np.full(len(pts), OUT_OF_FOV, np.uint8)
    u, v = uv[inside, 0].astype(int), uv[inside, 1].astype(int)
    d_obs = sdg.depth_m(gray[v, u], a, b)
    d_floor = np.linalg.norm(pts[inside] - sdg.center(cam), axis=1)
    t = tau(residual_m, d_floor, b)
    s = np.where(np.abs(d_obs - d_floor) < t, VISIBLE, np.where(d_obs < d_floor - t, OCCLUDED, UNKNOWN))
    out[inside] = np.where(sdg.jumps(gray)[v, u], UNKNOWN, s)
    return out


def main() -> None:
    run = sys.argv[1]
    run_dir = sdg.SDG_ROOT / run
    out = publish.tmp_dir(CARD, sdg.scene_id(run).replace("/", "-").lower())
    part = json.loads((out / "manifest.part.json").read_text())
    ground, fit, res = part["ground"], part["sources"]["depth_log_fit"], part["sources"]["depth_residual_m"]
    cams = {c["id"]: c for c in part["cams"]}
    order = ground["cam_bit_order"]
    pts = cell_centers(ground)
    with lease.acquire(cpu=CPU, owner=CARD):
        cv2.setNumThreads(CPU)
        info = video.probe(run_dir / f"{order[0]}.rgb.mp4")
        n_sec = math.ceil(info["duration_ms"] / 1000)
        grid = np.full((n_sec, len(pts), len(order)), NO_VIDEO, np.uint8)
        for k, cid in enumerate(order):
            idx = [round((s * 1000 + 500) * sdg.FPS / 1000) for s in range(n_sec)]
            gray = sdg.frames(run_dir / f"{cid}.depth.mp4", idx)
            for s in range(len(gray)):
                grid[s, :, k] = floor_states(cams[cid], gray[s], pts, fit["a"], fit["b"], res[cid])
    inputs = [run_dir / f"{c}.{x}" for c in order for x in ("depth.mp4", "camera_params.jsonl")]
    vis = {"schema_version": "1", "analysis_rev": ANALYSIS_REV, "producer_commit": producer_commit(),
           "input_artifact_hashes": {**{f"{run}/{p.name}": sha256(p) for p in inputs},
                                     "manifest.part.json": sha256(out / "manifest.part.json")},
           "cell_m": ground["cell_m"], "nx": ground["nx"], "ny": ground["ny"], "origin": ground["origin"], "cam_bit_order": order,
           "row_major": True, "encoding": "u8_cell_cam",
           "states": {"0": "unknown", "1": "visible", "2": "occluded", "3": "out_of_fov", "4": "no_video"}, "t0_s": 0,
           "per_sec": [base64.b64encode(g.tobytes()).decode() for g in grid], "visibility_revision": ANALYSIS_REV}
    (out / "visibility.json").write_text(json.dumps(vis))
    counts = np.bincount(grid.ravel(), minlength=5) / grid.size
    print(f"{part['scene_id']}: visibility {n_sec} s, {ground['nx']}x{ground['ny']}x{len(order)}, state shares "
          f"{dict(zip(vis['states'].values(), counts.round(3).tolist()))}", flush=True)


if __name__ == "__main__":
    main()
