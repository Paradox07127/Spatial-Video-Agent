"""P6: forklift.json for one SDG run, plus the SDG ground-truth readers shared by events.py and the eval scripts.

  python pipeline/warehouse/forklift.py RUN

The run first measures the recall of YOLO11 car / bus / truck boxes (pose2d.json) against the GT forklift 2D boxes of the
display cameras on the pose2d frames (IoU >= 0.5). Only recall >= 0.8 would justify the 3-view bottom-edge wedge
intersection; below that the run's track is the GT (source "gt"). GT = bbox3d bottom-face centre and local +x axis, never
world_xyz (off the box by up to metres in some runs) and no constant offset. Cosmos is not used for localisation.
Also writes _work/<scene_key>/forklift_obs.json = {recall, n_gt, n_hit, boxes: {cam: [[t_ms, 0, "forklift", x1, y1, x2, y2, 1.0]]}}.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.warehouse import pose2d, sdg  # noqa: E402

GT_FPS = 30
RECALL_MIN = 0.8
IOU_MATCH = 0.5
SPEED_HALF_S = 0.5  # speed = bottom-centre displacement over +-0.5 s
GT_TRACK_ID = 0  # local_track_id of the GT forklift box in every camera (ByteTrack ids start at 1)


def gt_lines(rd: Path, cam: str = "ceiling_00") -> list[dict]:
    """One dict of agents per 30 fps frame; 3D boxes are in the world frame, so any camera's file serves for 3D."""
    with open(rd / f"{cam}.object_detection.jsonl") as f:
        return [json.loads(line)["agents"] for line in f]


def agent(agents: dict, group: str) -> dict | None:
    """group: "Worker" or "forklift" (metro_agent_data.agent_group)."""
    return next((a for a in agents.values() if a["metro_agent_data"]["agent_group"] == group), None)


def box2d(a: dict) -> list[float] | None:
    """Tight GT 2D box; None when the agent is out of this camera's view (the annotator is then absent)."""
    b = a["annotators"].get("bounding_box_2d_tight_fast")
    return [b["x_min"], b["y_min"], b["x_max"], b["y_max"]] if b and b["x_max"] - b["x_min"] >= 2 else None


def bottom(a: dict) -> tuple[np.ndarray, float, np.ndarray]:
    """bbox3d bottom-face centre (xy), heading of the local +x axis (rad, CCW from world +x), footprint corners (4x2).
    The transform is row-vector form: world = [local, 1] @ T."""
    b = a["annotators"]["bounding_box_3d_fast"]
    T = np.array(b["transform"])
    loc = lambda x, y: (np.array([x, y, b["z_min"], 1.0]) @ T)[:2]  # noqa: E731
    centre = loc((b["x_min"] + b["x_max"]) / 2, (b["y_min"] + b["y_max"]) / 2)
    fp = np.array([loc(b["x_min"], b["y_min"]), loc(b["x_max"], b["y_min"]), loc(b["x_max"], b["y_max"]), loc(b["x_min"], b["y_max"])])
    return centre, math.atan2(T[0, 1], T[0, 0]), fp


def iou(a: list, b: list) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u > 0 else 0.0


def recall(rd: Path, poses: dict[str, dict]) -> tuple[int, int, dict]:
    """(GT boxes, GT boxes hit by a car/bus/truck box, GT boxes per cam on the pose2d frames)."""
    n = hit = 0
    boxes: dict[str, list] = {}
    for cam, pz in poses.items():
        gt = gt_lines(rd, cam)
        boxes[cam] = []
        for t, dets in pz["frames"]:
            a = agent(gt[round(t * GT_FPS / 1000)], "forklift")
            gb = box2d(a) if a else None
            if gb is None:
                continue
            boxes[cam].append([t, GT_TRACK_ID, "forklift", *gb, 1.0])
            n += 1
            hit += any(d[1] in ("car", "bus", "truck") and iou(gb, d[2:6]) >= IOU_MATCH for d in dets)
    return n, hit, boxes


def gt_track(rd: Path, ts: list[int]) -> tuple[list[dict], list[dict]]:
    """(forklift.json track with source gt, gt list) sampled at ts."""
    gt = gt_lines(rd)
    poses = [bottom(agent(g, "forklift")) for g in gt]
    h = round(SPEED_HALF_S * GT_FPS)
    track, ref = [], []
    for t in ts:
        f = round(t * GT_FPS / 1000)
        a, b = max(0, f - h), min(len(gt) - 1, f + h)
        c, hd, fp = poses[f]
        speed = float(np.linalg.norm(poses[b][0] - poses[a][0]) / ((b - a) / GT_FPS))
        track.append({"t_ms": t, "x": round(float(c[0]), 3), "y": round(float(c[1]), 3), "heading": round(hd, 4),
                      "speed": round(speed, 3), "footprint": [[round(float(v), 3) for v in p] for p in fp], "sigma_m": 0.0,
                      "source": "gt"})
        ref.append({"t_ms": t, "x": track[-1]["x"], "y": track[-1]["y"], "heading": track[-1]["heading"]})
    return track, ref


def run(rd: Path) -> dict:
    """Writes forklift.json and forklift_obs.json; returns forklift_obs (recall, n_gt, n_hit, boxes)."""
    key = pose2d.scene_key(rd)
    poses = pose2d.load(key, sdg.DISPLAY_CAMS)
    n, hit, boxes = recall(rd, poses)
    r = hit / n if n else 0.0
    if r >= RECALL_MIN:
        raise SystemExit(f"{key}: YOLO vehicle recall {r:.3f} >= {RECALL_MIN}; the yolo_wedge path is not implemented")
    track, ref = gt_track(rd, [t for t, _ in poses[sdg.DISPLAY_CAMS[0]]["frames"]])
    inputs = [rd / f"{c}.object_detection.jsonl" for c in sdg.DISPLAY_CAMS] + \
             [pose2d.WORK / key / "pose2d" / f"{c}.json" for c in sdg.DISPLAY_CAMS]
    pose2d.write_json(pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / key / "forklift.json",
                      {**pose2d.header(inputs), "forklifts": [{"entity_id": "F1", "source": "gt", "track": track, "gt": ref}]})
    obs = {"recall": round(r, 4), "n_gt": n, "n_hit": hit, "boxes": boxes}
    pose2d.write_json(pose2d.WORK / key / "forklift_obs.json", obs)
    return obs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    a = ap.parse_args()
    rd = pose2d.run_dir(a.run)
    with lease.acquire(cpu=1, owner=pose2d.OWNER):
        o = run(rd)
    print(f"forklift {pose2d.scene_key(rd)}: YOLO car/bus/truck recall {o['n_hit']}/{o['n_gt']} = {o['recall']:.3f} -> source gt",
          file=sys.stderr)


if __name__ == "__main__":
    main()
