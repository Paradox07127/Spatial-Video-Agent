"""P7: events.json for one scene: near_miss (rule) and, on SDG, dodge (gt).

  python pipeline/warehouse/events.py RUN

near_miss: a person root (skeleton.json) closer than 2 m to a moving forklift's footprint (forklift.json); frames closer
than GAP_MS apart form one event. dodge: start of each GT `Dodge` stretch of the worker, attached to the person entity
whose root follows the GT worker. Severity rules are written into each event's params.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.warehouse import forklift, pose2d, sdg  # noqa: E402

NEAR_M = 2.0
MOVING_MPS = 0.3  # footprint-corner speed; also catches turning on the spot (zone-clear-v3 forklift_pose_results.md)
GAP_MS = 500
SEV3_M, SEV2_M, FAST_MPS = 0.5, 1.0, 1.5
SEVERITY_RULE = (f"3 if min_dist_m < {SEV3_M} or (min_dist_m < {SEV2_M} and rel_speed_mps >= {FAST_MPS}); "
                 f"2 if min_dist_m < {SEV2_M} or rel_speed_mps >= {FAST_MPS}; else 1")
DODGE_SEVERITY_RULE = "1: GT Dodge label (a reaction; distance is judged by near_miss)"
MATCH_M = 1.5  # max median distance between a person root and the GT worker for the dodge actor


def severity(d: float, v: float) -> int:
    if d < SEV3_M or (d < SEV2_M and v >= FAST_MPS):
        return 3
    return 2 if d < SEV2_M or v >= FAST_MPS else 1


def _velocity(ts: list[int], xy: np.ndarray, i: int) -> np.ndarray:
    a, b = max(0, i - 1), min(len(ts) - 1, i + 1)
    return (xy[b] - xy[a]) / ((ts[b] - ts[a]) / 1000) if b > a else np.zeros(2)


def clip_ids(scene_key: str, cams: list[str], t0: int, t1: int) -> list[str]:
    return [f"{scene_key}:{c}:{k:05d}" for c in cams for k in range(t0 // 5000, (max(t1, t0 + 1) - 1) // 5000 + 1)]


def near_misses(persons: dict, forklifts: list[dict]) -> list[dict]:
    """Events without id / clip_ids, sorted by t0."""
    out = []
    for fk in forklifts:
        tr = fk["track"]
        fts = [p["t_ms"] for p in tr]
        corners = np.array([p["footprint"] for p in tr])
        centre = np.array([[p["x"], p["y"]] for p in tr])
        moving = [max(np.linalg.norm(_velocity(fts, corners[:, k], i)) for k in range(4)) >= MOVING_MPS for i in range(len(tr))]
        at = {t: i for i, t in enumerate(fts)}
        step = int(np.median(np.diff(fts))) if len(fts) > 1 else 0
        for pid, p in persons.items():
            root = np.array(p["root_world"])
            hits = [(i, at[t]) for i, t in enumerate(p["t_ms"]) if t in at and moving[at[t]]]
            hits = [(i, j, Polygon(tr[j]["footprint"]).distance(Point(root[i]))) for i, j in hits]
            hits = [h for h in hits if h[2] < NEAR_M]
            groups: list[list] = []
            for h in hits:
                if groups and p["t_ms"][h[0]] - p["t_ms"][groups[-1][-1][0]] <= GAP_MS:
                    groups[-1].append(h)
                else:
                    groups.append([h])
            for g in groups:
                i, j, d = min(g, key=lambda h: h[2])
                v = float(np.linalg.norm(_velocity(fts, centre, j) - _velocity(p["t_ms"], root, i)))
                out.append({"type": "near_miss", "t0_ms": p["t_ms"][g[0][0]], "t1_ms": p["t_ms"][g[-1][0]] + step,
                            "actor_ids": [pid, fk["entity_id"]], "place_id": None, "severity": severity(d, v), "source": "rule",
                            "params": {"min_dist_m": round(d, 3), "t_min_ms": p["t_ms"][i], "rel_speed_mps": round(v, 3),
                                       "threshold_m": NEAR_M, "moving_mps": MOVING_MPS, "distance_to": "forklift footprint",
                                       "severity_rule": SEVERITY_RULE}})
    return sorted(out, key=lambda e: e["t0_ms"])


def dodges(rd: Path, persons: dict) -> list[dict]:
    gt = forklift.gt_lines(rd)
    tasks = [(w := forklift.agent(g, "Worker")) and w["metro_agent_data"]["current_task_name"] for g in gt]
    out = []
    f = 0
    while f < len(gt):
        if tasks[f] != "Dodge":
            f += 1
            continue
        e = f
        while e < len(gt) and tasks[e] == "Dodge":
            e += 1
        t0, t1 = round(f * 1000 / forklift.GT_FPS), round(e * 1000 / forklift.GT_FPS)
        truth = {round(k * 1000 / forklift.GT_FPS): forklift.bottom(forklift.agent(gt[k], "Worker"))[0] for k in range(f, e)}
        best, best_d = None, MATCH_M
        for pid, p in persons.items():
            d = [np.linalg.norm(np.array(xy) - truth[t]) for t, xy in zip(p["t_ms"], p["root_world"]) if t in truth]
            if d and float(np.median(d)) < best_d:
                best, best_d = pid, float(np.median(d))
        out.append({"type": "dodge", "t0_ms": t0, "t1_ms": t1, "actor_ids": [best] if best else [], "place_id": None,
                    "severity": 1, "source": "gt", "params": {"label": "Dodge", "severity_rule": DODGE_SEVERITY_RULE}})
        f = e
    return out


def finish(events: list[dict], scene_key: str, cams: list[str]) -> list[dict]:
    events = sorted(events, key=lambda e: (e["t0_ms"], e["type"]))
    for n, e in enumerate(events, 1):
        e["id"] = f"E{n}"
        e["clip_ids"] = clip_ids(scene_key, cams, e["t0_ms"], e["t1_ms"])
        e["eids"] = []
    return [{"id": e.pop("id"), **e} for e in events]


def run(rd: Path) -> list[dict]:
    key = pose2d.scene_key(rd)
    out = pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / key
    persons = json.loads((out / "skeleton.json").read_text())["persons"]
    fks = json.loads((out / "forklift.json").read_text())["forklifts"]
    ev = finish(near_misses(persons, fks) + dodges(rd, persons), key, sdg.DISPLAY_CAMS)
    inputs = [out / "skeleton.json", out / "forklift.json", rd / "ceiling_00.object_detection.jsonl"]
    pose2d.write_json(out / "events.json", {**pose2d.header(inputs), "events": ev})
    return ev


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    a = ap.parse_args()
    rd = pose2d.run_dir(a.run)
    with lease.acquire(cpu=1, owner=pose2d.OWNER):
        ev = run(rd)
    sev = max((e["severity"] for e in ev), default=0)
    print(f"events {pose2d.scene_key(rd)}: {len(ev)} events, max severity {sev}", file=sys.stderr)


if __name__ == "__main__":
    main()
