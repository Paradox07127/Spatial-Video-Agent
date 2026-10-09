"""Person root error on SDG runs: box bottom vs RTMPose ankle vs triangulation, paired on the same samples; forklift centre error.

  python pipeline/eval/wh_position_error.py [--out MD]

Truth: SDG bbox3d bottom-face centre of the worker / forklift (not world_xyz). A sample is (run, pose2d frame, camera) where
the camera's person box matches the GT 2D box (IoU > 0.3), that box has a confident ankle, and the person's skeleton frame is
triangulated (root_method tri, >= 2 views). On it: box bottom on z=0, ankle on the ankle plane (single view), and the
triangulated root of the same frame. Split by camera and by horizontal camera-to-worker distance. Forklift: forklift.json
track vs GT on runs 40-79 (run order = sorted run directory names), with the YOLO car/bus/truck recall that chose the source.
Reads _tmp/P-W2 products of every run that has them.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.warehouse import forklift, pose2d, sdg, triangulate  # noqa: E402

IOU_GT = 0.3
DIST_BINS = [0, 8, 12, 16, np.inf]  # m, camera-to-worker horizontal distance


def run_rows(rd: Path) -> list[dict]:
    key = pose2d.scene_key(rd)
    poses = pose2d.load(key, sdg.DISPLAY_CAMS)
    geos = {c: triangulate.Geo(e) for c, e in triangulate.sdg_cams(rd, sdg.DISPLAY_CAMS).items()}
    owner = {(o["cam_id"], o["local_track_id"]): e["entity_id"]
             for e in json.loads((pose2d.WORK / key / "persons.json").read_text())["entities"] for o in e["observations"]}
    sk = json.loads((pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / key / "skeleton.json").read_text())["persons"]
    tri = {(pid, t): xy for pid, p in sk.items() for t, xy, m in zip(p["t_ms"], p["root_world"], p["root_method"]) if m == "tri"}
    rows = []
    for cam, pz in poses.items():
        gt = forklift.gt_lines(rd, cam)
        for t, dets in pz["frames"]:
            w = forklift.agent(gt[round(t * forklift.GT_FPS / 1000)], "Worker")
            gb = forklift.box2d(w) if w else None
            people = [d for d in dets if d[1] == "person"]
            if gb is None or not people:
                continue
            d = max(people, key=lambda d: forklift.iou(gb, d[2:6]))
            pid = owner.get((cam, d[0]))
            if forklift.iou(gb, d[2:6]) <= IOU_GT or (pid, t) not in tri:
                continue
            ank, method = triangulate.root_view(geos[cam], d)
            box = geos[cam].ground((d[2] + d[4]) / 2, d[5], 0.0)
            if method != "ankle" or ank is None or box is None:
                continue
            truth = forklift.bottom(w)[0]
            rows.append({"cam": cam, "dist": float(np.linalg.norm(geos[cam].C[:2] - truth)),
                         "box": float(np.linalg.norm(box - truth)), "ankle": float(np.linalg.norm(ank - truth)),
                         "tri": float(np.linalg.norm(np.array(tri[(pid, t)]) - truth))})
    return rows


def pct(v: list[float]) -> str:
    return f"{np.median(v):.3f} / {np.percentile(v, 90):.3f}" if v else "–"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pose2d.OUT_ROOT / "_logs" / pose2d.OWNER / "wh_position_error.md"))
    a = ap.parse_args()
    runs = sorted(p for p in sdg.SDG_ROOT.iterdir() if sdg.RUN_RE.match(p.name))
    done = [(i, rd) for i, rd in enumerate(runs) if (pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / pose2d.scene_key(rd) / "forklift.json").is_file()]
    rows, fk_err, rec = [], [], []
    with lease.acquire(cpu=2, owner=pose2d.OWNER):
        for i, rd in done:
            key = pose2d.scene_key(rd)
            rows += run_rows(rd)
            fo = json.loads((pose2d.WORK / key / "forklift_obs.json").read_text())
            rec.append((i, fo["n_gt"], fo["n_hit"]))
            if 40 <= i <= 79:
                fk = json.loads((pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / key / "forklift.json").read_text())["forklifts"][0]
                truth = {round(q["t_ms"] * forklift.GT_FPS / 1000): q for q in fk["gt"]}
                for q in fk["track"]:
                    g = truth[round(q["t_ms"] * forklift.GT_FPS / 1000)]
                    fk_err.append((q["source"], float(np.hypot(q["x"] - g["x"], q["y"] - g["y"]))))
    L = ["# Person root and forklift position error (SDG)", "",
         f"Runs with products: {len(done)} of {len(runs)}. Paired samples: N = {len(rows)} (run x pose2d frame x camera; "
         "box matched to GT IoU > 0.3, confident ankle, triangulated root in the same frame). Error in m, median / p90.", "",
         "| split | N | box bottom | ankle (single view) | triangulated |", "|---|---|---|---|---|"]
    splits = [(c, [r for r in rows if r["cam"] == c]) for c in sdg.DISPLAY_CAMS]
    splits += [(f"dist {lo:g}-{hi:g} m", [r for r in rows if lo <= r["dist"] < hi]) for lo, hi in zip(DIST_BINS, DIST_BINS[1:])]
    for name, R in splits + [("all", rows)]:
        L.append(f"| {name} | {len(R)} | {pct([r['box'] for r in R])} | {pct([r['ankle'] for r in R])} | {pct([r['tri'] for r in R])} |")
    n10 = [x for x in rec if x[0] < 10]
    L += ["", "## Forklift", "",
          f"YOLO11m car/bus/truck recall vs GT forklift 2D boxes (IoU >= {forklift.IOU_MATCH}, pose2d frames, 3 cameras): "
          f"runs 0-9 {sum(x[2] for x in n10)}/{sum(x[1] for x in n10)} = {sum(x[2] for x in n10) / max(1, sum(x[1] for x in n10)):.3f}; "
          f"all {len(rec)} runs {sum(x[2] for x in rec)}/{sum(x[1] for x in rec)} = {sum(x[2] for x in rec) / max(1, sum(x[1] for x in rec)):.3f}; "
          f"best run {max(x[2] / max(1, x[1]) for x in rec):.3f} (threshold {forklift.RECALL_MIN}).",
          f"Centre error runs 40-79: N = {len(fk_err)}, sources {sorted({s for s, _ in fk_err})}, "
          f"median / p90 {pct([e for _, e in fk_err])} m (a gt-sourced track equals its GT by construction)."]
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
