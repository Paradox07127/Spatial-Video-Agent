"""Leave-one-camera-out reprojection error of the triangulated keypoints (no joint ground truth: self-consistency).

  python pipeline/eval/wh_reprojection.py [--out MD]

For every SDG run with P-W2 products, person entity (persons.json) and frame where a joint is confident (RTMPose score
>= triangulate.KP_CONF) in all three display cameras: triangulate it from the other two (same DLT + RANSAC as
triangulate.py, before bone check and smoothing) and measure the pixel distance of its projection to the held-out
camera's keypoint. Pairs whose two-view point fails the 8 px gate are counted, not measured.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.warehouse import pose2d, sdg, triangulate  # noqa: E402


def run_errors(rd: Path) -> tuple[list[tuple[str, int, float]], int]:
    """([(held-out cam, joint, px error)], number of two-view fits rejected by the reprojection gate)."""
    key = pose2d.scene_key(rd)
    poses = pose2d.load(key, sdg.DISPLAY_CAMS)
    geos = {c: triangulate.Geo(e) for c, e in triangulate.sdg_cams(rd, sdg.DISPLAY_CAMS).items()}
    tracks = triangulate.person_tracks(poses)
    out, rejected = [], 0
    for e in json.loads((pose2d.WORK / key / "persons.json").read_text())["entities"]:
        by_t: dict[int, dict[str, list]] = {}
        for o in e["observations"]:
            for t, d in tracks[(o["cam_id"], o["local_track_id"])].items():
                by_t.setdefault(t, {})[o["cam_id"]] = d
        for views in by_t.values():
            if len(views) < 3:
                continue
            for j in range(17):
                if min(d[7][j][2] for d in views.values()) < triangulate.KP_CONF:
                    continue
                for h in views:
                    X, inl = triangulate.tri_joint([(geos[c], d[7][j]) for c, d in views.items() if c != h])
                    if X is None:
                        rejected += 1
                        continue
                    x, _ = geos[h].project(X)
                    out.append((h, j, float(np.hypot(*(x - views[h][7][j][:2])))))
    return out, rejected


def pct(v: list[float]) -> str:
    return f"{np.median(v):.1f} / {np.percentile(v, 90):.1f}" if v else "–"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pose2d.OUT_ROOT / "_logs" / pose2d.OWNER / "wh_reprojection.md"))
    a = ap.parse_args()
    runs = [rd for rd in sorted(p for p in sdg.SDG_ROOT.iterdir() if sdg.RUN_RE.match(p.name))
            if (pose2d.WORK / pose2d.scene_key(rd) / "persons.json").is_file()]
    errs, rej = [], 0
    with lease.acquire(cpu=2, owner=pose2d.OWNER):
        for rd in runs:
            e, r = run_errors(rd)
            errs += e
            rej += r
    L = ["# Leave-one-camera-out reprojection error (SDG, px at 1920x1080)", "",
         f"Runs: {len(runs)}. Joint-frames with all three cameras confident; held-out camera predicted from the other two. "
         f"Two-view fits rejected by the {triangulate.REPROJ_PX:g} px gate: {rej}. Median / p90 px.", "",
         "| held-out camera | N | error px |", "|---|---|---|"]
    for c in sdg.DISPLAY_CAMS:
        v = [x for h, _, x in errs if h == c]
        L.append(f"| {c} | {len(v)} | {pct(v)} |")
    L.append(f"| all | {len(errs)} | {pct([x for _, _, x in errs])} |")
    L += ["", "| joint group | N | error px |", "|---|---|---|"]
    for name, js in (("head 0-4", range(0, 5)), ("arms 5-10", range(5, 11)), ("hips 11-12", (11, 12)), ("legs 13-16", range(13, 17))):
        v = [x for _, j, x in errs if j in js]
        L.append(f"| {name} | {len(v)} | {pct(v)} |")
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text("\n".join(L) + "\n")
    print("\n".join(L))


if __name__ == "__main__":
    main()
