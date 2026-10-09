"""Fisheye ground ranging of the fleet pipeline (ego_scene), two checks.

  python pipeline/eval/fl_ground_error.py [--out JSON]

(a) Calibration round trip: synthetic ground points -> cv2.fisheye.projectPoints (PIE K, D, height, pitch) ->
    cv2.fisheye.undistortPoints; error = |undistorted - true pinhole coordinates| in pinhole px (fx units), centre
    (within CENTRE_PX of the principal point) and off-axis; pass < 0.1 px.
(b) Real references: crosswalks boxed by PIE's human annotators (annotations/setNN/video_NNNN_annt.xml, label crosswalk).
    The near edge of a crosswalk ahead is the box bottom on the principal column. Truth at an earlier frame f1 = the
    ranged edge at the last frame f2 it is fully in view (a few metres, where flat-ground ranging is good) + the distance
    driven from f1 to f2 (OBD speed integral), on straight stretches. Reported per distance bin: bias, median, p90 and
    the share of references inside 1 and 2 sigma (ego_scene.sigma_m of the estimate combined with the truth's own).
"""
from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import ego_scene, pie  # noqa: E402

CENTRE_PX = 300.0
PASS_PX = 0.1
V_MAX = 950.0  # the bonnet hides the road below about this row
ANCHOR_MAX_M = 8.0
MAX_TURN_DEG = 5.0  # heading change between f1 and f2: odometry only measures straight-line range on a straight road
ODO_REL = 0.03  # OBD speedometer scale error
BINS = [(5, 10), (10, 15), (15, 20), (20, 30), (30, 50)]


def round_trip(cal: dict) -> dict:
    K, D = np.array(cal["K"], float), np.array(cal["D"], float).reshape(4)
    xs, ys = np.meshgrid(np.r_[2.0:10:1.0, 10:60:5.0], np.r_[-15.0:15.5:1.5])
    P = np.stack([xs.ravel(), ys.ravel(), np.zeros(xs.size)], 1)
    uv = ego_scene.project_ego(P, cal)
    inside = (uv[:, 0] >= 0) & (uv[:, 0] < cal["dim"][0]) & (uv[:, 1] >= 0) & (uv[:, 1] < cal["dim"][1])
    P, uv = P[inside], uv[inside]
    _, _, h, tilt = ego_scene._cam(cal)
    right, fwd, down = -P[:, 1], P[:, 0], h - P[:, 2]
    pc = np.stack([right, np.cos(tilt) * down - np.sin(tilt) * fwd, np.cos(tilt) * fwd + np.sin(tilt) * down], 1)
    true = pc[:, :2] / pc[:, 2:]
    est = cv2.fisheye.undistortPoints(uv.reshape(-1, 1, 2), K, D, criteria=ego_scene.CRIT).reshape(-1, 2)
    err = np.hypot(*((est - true) * K[0, 0]).T)
    ground = np.hypot(*(ego_scene.ground_flu(uv, cal) - P[:, :2]).T)
    centre = np.hypot(uv[:, 0] - K[0, 2], uv[:, 1] - K[1, 2]) <= CENTRE_PX
    out = {"n_centre": int(centre.sum()), "n_off_axis": int((~centre).sum()),
           "max_px_centre": float(err[centre].max()), "max_px_off_axis": float(err[~centre].max()),
           "max_ground_m": float(ground.max())}
    out["pass"] = out["max_px_centre"] < PASS_PX and out["max_px_off_axis"] < PASS_PX
    return out


def crosswalk_refs(vid: str, cal: dict) -> list[dict]:
    o = pie.obd(vid)
    driven = np.r_[0.0, np.cumsum(o["speed_kmh"] / 3.6 / pie.FPS)]
    cx = cal["K"][0][2]
    refs = []
    for tr in ET.parse(pie.annt_path(vid)).getroot().iter("track"):
        if tr.get("label") != "crosswalk":
            continue
        boxes = [(int(b.get("frame")), float(b.get("ybr"))) for b in tr.iter("box")
                 if b.get("outside") == "0" and b.get("occluded") == "0" and float(b.get("xtl")) < cx < float(b.get("xbr"))
                 and float(b.get("ybr")) < V_MAX]
        if len(boxes) < 2:
            continue
        cid = tr.find("box/attribute[@name='id']").text
        frames = np.array([f for f, _ in boxes])
        est = ego_scene.ground_flu(np.array([[cx, v] for _, v in boxes]), cal)[:, 0]
        f2, x2 = frames[-1], est[-1]
        if not x2 <= ANCHOR_MAX_M:
            continue
        sig2 = float(ego_scene.sigma_m(np.array([[x2, 0.0]]), cal)[0])
        odo = driven[f2] - driven[frames]
        turn = np.abs((o["heading"][frames] - o["heading"][f2] + 180) % 360 - 180)
        truth = x2 + odo
        for lo, hi in BINS:
            ok = np.nonzero((truth >= lo) & (truth < hi) & (turn <= MAX_TURN_DEG) & np.isfinite(est))[0]
            if not len(ok):
                continue
            i = ok[np.abs(truth[ok] - (lo + hi) / 2).argmin()]
            s_est = float(ego_scene.sigma_m(np.array([[est[i], 0.0]]), cal)[0])
            s_truth = float(np.hypot(sig2, ODO_REL * odo[i]))
            refs.append({"video": vid, "crosswalk": cid, "f1": int(frames[i]), "f2": int(f2), "truth_m": round(float(truth[i]), 2),
                         "est_m": round(float(est[i]), 2), "err_m": round(float(est[i] - truth[i]), 2),
                         "sigma_m": round(float(np.hypot(s_est, s_truth)), 2), "anchor_m": round(float(x2), 2),
                         "source": f"{pie.rel(pie.annt_path(vid))} crosswalk {cid} box bottom (human) + OBD speed integral"})
    return refs


def summary(refs: list[dict]) -> list[dict]:
    rows = []
    for lo, hi in BINS:
        r = [x for x in refs if lo <= x["truth_m"] < hi]
        if not r:
            continue
        e = np.array([x["err_m"] for x in r])
        s = np.array([x["sigma_m"] for x in r])
        t = np.array([x["truth_m"] for x in r])
        rows.append({"bin_m": [lo, hi], "n": len(r), "bias_m": round(float(np.median(e)), 2),
                     "median_abs_m": round(float(np.median(np.abs(e))), 2), "p90_abs_m": round(float(np.percentile(np.abs(e), 90)), 2),
                     "median_abs_pct": round(float(np.median(np.abs(e) / t) * 100), 1),
                     "within_1sigma": round(float(np.mean(np.abs(e) <= s)), 2), "within_2sigma": round(float(np.mean(np.abs(e) <= 2 * s)), 2)})
    return rows


def main(out: Path) -> None:
    with lease.acquire(cpu=1, owner=pie.CARD):
        cal = pie.calibration()
        rt = round_trip(cal)
        refs = [r for v in pie.video_ids() for r in crosswalk_refs(v, cal)]
    rows = summary(refs)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"round_trip": rt, "bins": rows, "references": refs}, indent=1))
    print(f"(a) round trip: centre max {rt['max_px_centre']:.2e} px (n={rt['n_centre']}), off-axis max "
          f"{rt['max_px_off_axis']:.2e} px (n={rt['n_off_axis']}), ground max {rt['max_ground_m']:.2e} m -> "
          f"{'PASS' if rt['pass'] else 'FAIL'} (< {PASS_PX} px)")
    print(f"(b) {len(refs)} crosswalk references from {len({r['video'] for r in refs})} videos")
    print("bin_m     n   bias_m  med|e|_m  p90|e|_m  med|e|%  in1sigma  in2sigma")
    for r in rows:
        print(f"{r['bin_m'][0]:>2}-{r['bin_m'][1]:<4} {r['n']:>4} {r['bias_m']:>8} {r['median_abs_m']:>9} {r['p90_abs_m']:>9} "
              f"{r['median_abs_pct']:>8} {r['within_1sigma']:>9} {r['within_2sigma']:>9}")
    print(f"-> {out}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(pie.OUT / "_logs" / pie.CARD / "fl_ground_error.json"))
    main(Path(ap.parse_args().out))
