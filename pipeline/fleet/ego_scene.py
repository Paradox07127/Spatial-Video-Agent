"""F7: per-frame ego scene (10 fps) -> ego/<chunk>.json: ego pose, pedestrians and vehicles on the ground, visible
building spans.

  python pipeline/fleet/ego_scene.py SCENE_KEY

Ground positions: the fisheye pixel of a box's bottom-centre (a vehicle's lower edge; a pedestrian's feet unless an ankle
keypoint is confident, see ped_anchor) is undistorted
with cv2.fisheye.undistortPoints (PIE K, D) to a pinhole ray, which is intersected with the flat ground, as D2
`Ground.point` (sim/demos/claim-pack-v3/agent.py:387-407) does for a pinhole image. D2 returns (right, forward); here it
becomes the ego frame (x forward, y left) with origin on the ground below the camera.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import facades, pie  # noqa: E402

STEP_FRAMES = 3  # 10 fps of the 30 fps video
CHUNK_MS = 10000
# Pitch of the camera against the road (braking, grade, mounting) and its height (load, suspension): the two errors of a
# flat-ground range. d = h / tan(a) for depression angle a, so dd/dh = d / h and dd/da = -h / sin(a)^2.
SIGMA_PITCH_RAD = math.radians(1.0)
SIGMA_H_M = 0.05
MAX_RANGE_M = 40.0  # beyond, the 1-sigma range error (~ d * SIGMA_PITCH_RAD / h) exceeds half the range
PED_H_M = 1.7
KP_CONF = 0.5  # RTMPose SimCC score below which a keypoint is not used, as warehouse/triangulate.py
ANKLES = (15, 16)
ANKLE_H_M = 0.08  # ankle joint height above the ground (anthropometric)
TRUNC_PX = 2  # a box bottom this close to the image bottom is cut by the frame, so the feet are not in it
SIGMA_FOOT_M = 0.2  # box bottom vs ankle anchor: the quadrature gap of triangulate.ROOT_SIGMA boxbottom and ankle
DIMS = {"car": [4.5, 1.8, 1.5], "truck": [8.0, 2.5, 3.2], "bus": [12.0, 2.55, 3.2], "bicycle": [1.8, 0.6, 1.7],
        "motorcycle": [2.1, 0.8, 1.4]}  # length, width, height (m): class priors
YAW_DT_MS = 1000  # half window of the track displacement that gives a vehicle's heading
MIN_SPEED_MPS = 1.0  # slower vehicles keep yaw 0 (aligned with the ego lane)
CRIT = (cv2.TERM_CRITERIA_COUNT + cv2.TERM_CRITERIA_EPS, 100, 1e-12)


def _cam(cal: dict):
    """K, D, camera height (m), downward tilt (rad). PIE's cam_pitch_deg = -10 tilts the camera 10 deg up."""
    return (np.array(cal["K"], float), np.array(cal["D"], float).reshape(4), cal["cam_height_mm"] / 1000,
            math.radians(cal["cam_pitch_deg"]))


def flu(right: float, fwd: float) -> tuple[float, float]:
    """D2 Ground.point (right, forward) -> ego (forward, left)."""
    return fwd, -right


def ground_rf(uv: np.ndarray, cal: dict, z=0.0) -> np.ndarray:
    """Source pixels (n, 2) on planes z (m) above the ground -> (right, forward) metres; NaN at or above the horizon."""
    K, D, h, tilt = _cam(cal)
    xy = cv2.fisheye.undistortPoints(np.asarray(uv, float).reshape(-1, 1, 2), K, D, criteria=CRIT).reshape(-1, 2)
    x, y = xy.T
    den = y * math.cos(tilt) + math.sin(tilt)
    t = np.where(den > 1e-3, (h - np.asarray(z, float)) / np.where(den > 1e-3, den, 1.0), np.nan)
    return np.stack([t * x, t * (math.cos(tilt) - y * math.sin(tilt))], 1)


def ground_flu(uv: np.ndarray, cal: dict, z=0.0) -> np.ndarray:
    rf = ground_rf(uv, cal, z)
    return np.stack(flu(rf[:, 0], rf[:, 1]), 1)


def ped_anchor(box, kp, img_h: float) -> tuple[float, float, bool] | None:
    """(u, v, ankle) ground anchor pixel of a pedestrian box (x1, y1, x2, y2) with COCO keypoints kp: the mean confident
    ankle (on the ANKLE_H_M plane), else the box bottom centre; None when the box is cut by the image bottom and no ankle
    is confident."""
    a = [kp[j][:2] for j in ANKLES if kp and kp[j][2] >= KP_CONF]
    if a:
        u, v = np.mean(a, axis=0)
        return float(u), float(v), True
    x1, _, x2, y2 = box
    return None if y2 >= img_h - TRUNC_PX else ((x1 + x2) / 2, y2, False)


def project_ego(P: np.ndarray, cal: dict) -> np.ndarray:
    """Ego-frame points (n, 3) -> source pixels (n, 2) through the fisheye."""
    K, D, h, tilt = _cam(cal)
    P = np.asarray(P, float)
    right, fwd, down = -P[:, 1], P[:, 0], h - P[:, 2]
    pc = np.stack([right, math.cos(tilt) * down - math.sin(tilt) * fwd, math.cos(tilt) * fwd + math.sin(tilt) * down], 1)
    return cv2.fisheye.projectPoints(pc.reshape(-1, 1, 3), np.zeros(3), np.zeros(3), K, D)[0].reshape(-1, 2)


def cam2world(cal: dict) -> list[float]:
    """Camera -> ego 4x4, OpenGL basis (+X right, +Y up, looks -Z), column-major."""
    _, _, h, tilt = _cam(cal)
    p = -tilt  # upward pitch
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = [0, -1, 0], [-math.sin(p), 0, math.cos(p)], [-math.cos(p), 0, -math.sin(p)], [0, 0, h]
    return [round(float(v), 9) for v in m.flatten(order="F")]


def sigma_m(fl: np.ndarray, cal: dict) -> np.ndarray:
    """1-sigma error (m) of flat-ground positions fl (n, 2) from SIGMA_PITCH_RAD and SIGMA_H_M."""
    h = cal["cam_height_mm"] / 1000
    d = np.hypot(fl[:, 0], fl[:, 1])
    a = np.arctan2(h, d)
    return np.hypot(d / h * SIGMA_H_M, h / np.sin(a) ** 2 * SIGMA_PITCH_RAD)


def _yaw(pos: dict, tid: int, t: int, ego_speed: float) -> float:
    """rad CCW from ego +x: the track's displacement over +-YAW_DT_MS plus the ego's own motion (rotation ignored)."""
    a, b = pos.get((tid, t - YAW_DT_MS)), pos.get((tid, t + YAW_DT_MS))
    if a is None or b is None:
        return 0.0
    vx = (b[0] - a[0]) / (2 * YAW_DT_MS / 1000) + ego_speed
    vy = (b[1] - a[1]) / (2 * YAW_DT_MS / 1000)
    return round(math.atan2(vy, vx), 4) if math.hypot(vx, vy) >= MIN_SPEED_MPS else 0.0


def run(key: str) -> Path:
    vid, out, work = pie.vid_of(key), pie.tmp_dir(key), pie.work_dir(key)
    with lease.acquire(cpu=2, owner=pie.CARD):
        cal, o, osm = pie.calibration(), pie.obd(vid), pie.osm()
        ents = pie.entity_of(out)
        rows = [r for r in pie.load_boxes(out) if r[1] in ents]
        kp = {(t, tid): k for t, tid, k in json.loads((work / "pose2d.json").read_text())["rows"]}
        anc = [ped_anchor(r[3:7], kp.get((r[0], r[1])), cal["dim"][1]) if ents[r[1]][1] == "person"
               else ((r[3] + r[5]) / 2, r[6], None) for r in rows]
        rows, anc = [r for r, a in zip(rows, anc) if a], [a for a in anc if a]
        uv = np.array([a[:2] for a in anc], float).reshape(-1, 2)
        fl = ground_flu(uv, cal, np.array([ANKLE_H_M if a[2] else 0.0 for a in anc])) if len(rows) else np.zeros((0, 2))
        sig = sigma_m(fl, cal)
        sig = np.where([a[2] is False for a in anc], np.hypot(sig, SIGMA_FOOT_M), sig)
        pos = {(r[1], r[0]): (fl[i, 0], fl[i, 1]) for i, r in enumerate(rows)}
        kpts = {tk: [[round(u), round(v), c] for u, v, c in k] for tk, k in kp.items()}
        by_t: dict[int, list[int]] = {}
        for i, r in enumerate(rows):
            if np.isfinite(fl[i]).all() and fl[i, 0] > 0 and math.hypot(*fl[i]) <= MAX_RANGE_M:
                by_t.setdefault(r[0], []).append(i)
        frames_idx = list(range(0, len(o["lat"]), STEP_FRAMES))
        gps = pie.gps_at(o, frames_idx)
        chunks: dict[int, list[dict]] = {}
        for f, g in zip(frames_idx, gps):
            t = pie.t_ms(f)
            peds, vehs = [], []
            for i in by_t.get(t, []):
                r = rows[i]
                eid, kind = ents[r[1]]
                x, y, s = round(float(fl[i, 0]), 3), round(float(fl[i, 1]), 3), round(float(sig[i]), 3)
                if kind == "person":
                    peds.append({"entity_id": eid, "x": x, "y": y, "h": PED_H_M, "sigma_m": s, "kpts2d": kpts.get((t, r[1])),
                                 "source": "fisheye_ground"})
                else:
                    vehs.append({"entity_id": eid, "x": x, "y": y, "yaw": _yaw(pos, r[1], t, g["speed"]), "cls": r[2],
                                 "dims": DIMS[r[2]], "sigma_m": s})
            blds = [{"bid": bid, "u0": u0, "u1": u1}
                    for bid, (u0, u1, _) in sorted(facades.facades(osm, g["lat"], g["lon"], g["heading"], cal).items(),
                                                   key=lambda kv: kv[1][0])]
            ego = {"lat": round(g["lat"], 7), "lon": round(g["lon"], 7), "heading": round(g["heading"], 2),
                   "sigma_m": round(g["sigma_m"], 2), "state": g["state"]}
            chunks.setdefault(t // CHUNK_MS * CHUNK_MS, []).append(
                {"t_ms": t, "ego": ego, "peds": peds, "vehs": vehs, "buildings": blds})
        hdr = pie.header([pie.obd_path(vid), pie.PIE / "calibration_data.json", pie.OSM_JSON, out / "entities.json",
                          work / "pose2d.json"])
        for c0, frames in chunks.items():
            pie.write_json(out / "ego" / f"{c0 // CHUNK_MS:05d}.json",
                           {**hdr, "trip_id": key, "chunk_t0_ms": c0, "frames": frames})
    print(f"ego_scene {key}: {sum(map(len, chunks.values()))} frames, {len(chunks)} chunks", file=sys.stderr)
    return out / "ego"


if __name__ == "__main__":
    run(sys.argv[1])
