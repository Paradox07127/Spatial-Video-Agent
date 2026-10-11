"""Generate the W0 contract fixture.

  .venv/bin/python fixtures/gen_fixture.py [--out fixtures] [--no-media] [--index PATH]

Scenes (under <out>/sva-out/):
  wh-sdg-fixture-01  warehouse, 3 cams, 10 s: P1 and P2 present together; P2 seen by ceiling_00 only in [2000, 2300);
                     eye_00 has no video in [6000, 7000); ceiling_04 offset_ms = 120; near_miss P1-F1 severity 2;
                     forklift F1 hides P1's floor cell from ceiling_00 around t = 4600.
  fl-pie-fixture-01  fleet, fisheye dashcam, 10 s: pedestrian P1 crosses, GPS gap (4000, 7000) > 2000 ms -> last_known.
  fl-pie-fixture-02  fleet, GPS only (overview), 60 s.
Under <out>/l1/ (kept out of sva-out, whose readers know two workspaces only):
  st-fixture_cam_1-clip01  street (L1), one 2D camera, 10 s, one chunk: a person walks along the sidewalk while a car
                     passes in [2000, 8000); 2D detections only, no geometry, no track ids.
Also <out>/asks.json, <out>/l1/asks_corpus.json (corpus asks recorded from the real agent), <out>/runs/*.json, <out>/labels/*.json.
JSON and points.bin are deterministic; videos and thumbnails are rendered with Pillow + ffmpeg unless --no-media.
--index writes an index.duckdb from schemas/index.sql (sva-out scenes only; build_index(..., l1=True) adds the L1 scene).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import shutil
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import cv2
import numpy as np

SVA = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SVA))
from app.core.models import CosmosRecord, Evidence, ObservationRef, make_record_id, scene_key_of  # noqa: E402

REV = "fx-0001"
COMMIT = "w0-fixture"
HEADER = {"schema_version": "1", "analysis_rev": REV, "producer_commit": COMMIT, "input_artifact_hashes": {}}
W, H, FPS = 640, 360, 30
T1 = 10_000
STEP = 100  # sampling period (ms) of boxes, skeleton, tracks, ego frames

# ---------------------------------------------------------------- geometry

def lookat(eye, target, up=(0.0, 0.0, 1.0)) -> np.ndarray:
    """camera -> parent 4x4, OpenGL basis: +X right, +Y up, looks along -Z."""
    eye, target, up = (np.asarray(v, float) for v in (eye, target, up))
    z = eye - target
    z /= np.linalg.norm(z)
    x = np.cross(up, z)
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = x, y, z, eye
    return m


def col_major(m: np.ndarray) -> list[float]:
    return [round(float(v), 9) for v in m.flatten(order="F")]


class Cam:
    def __init__(self, cid, eye, target, K, model="pinhole", dist=(), offset_ms=0, sync="sdg_frame_index"):
        self.id, self.K, self.model, self.dist, self.offset_ms, self.sync = cid, list(K), model, list(dist), offset_ms, sync
        self.c2w_list = col_major(lookat(eye, target))
        self.c2w = np.array(self.c2w_list).reshape(4, 4, order="F")
        self.w2c = np.linalg.inv(self.c2w)
        self.center = self.c2w[:3, 3]

    def to_cv(self, P) -> np.ndarray:
        """world points -> OpenCV camera coords (x right, y down, z forward)."""
        P = np.atleast_2d(np.asarray(P, float))
        pc = (self.w2c @ np.c_[P, np.ones(len(P))].T).T[:, :3]
        return pc * np.array([1.0, -1.0, -1.0])

    def project(self, P) -> tuple[np.ndarray, np.ndarray]:
        """pixels (N, 2) and in-front mask."""
        pc = self.to_cv(P)
        front = pc[:, 2] > 0.1
        fx, fy, cx, cy = self.K
        if self.model == "fisheye":
            uv = cv2.fisheye.projectPoints(pc.reshape(-1, 1, 3).astype(np.float64), np.zeros(3), np.zeros(3),
                                           np.array([[fx, 0, cx], [0, fy, cy], [0, 0, 1.0]]), np.array(self.dist, float))[0]
            return uv.reshape(-1, 2), front
        z = np.where(front, pc[:, 2], 1.0)
        return np.c_[fx * pc[:, 0] / z + cx, fy * pc[:, 1] / z + cy], front

    def manifest(self, has_video=True, thumbs=True) -> dict:
        return {"id": self.id, "w": W, "h": H, "K": self.K, "model": self.model, "dist": self.dist, "camera_basis": "opengl",
                "cam2world": self.c2w_list, "fov_deg": round(math.degrees(2 * math.atan(H / (2 * self.K[1]))), 6),
                "video": f"video/{self.id}.mp4" if has_video else None,
                "frame_ref": {"video_w": W, "video_h": H, "thumb_w": 320, "thumb_h": 180},
                "offset_ms": self.offset_ms, "thumbs": f"thumbs/{self.id}" if thumbs else None,
                "sync_method": self.sync, "sync_residual_ms": 0.0 if self.sync != "unknown" else None}


def box_corners(cx, cy, length, width, height, heading=0.0) -> np.ndarray:
    c, s = math.cos(heading), math.sin(heading)
    out = []
    for dx in (-length / 2, length / 2):
        for dy in (-width / 2, width / 2):
            for z in (0.0, height):
                out.append([cx + c * dx - s * dy, cy + s * dx + c * dy, z])
    return np.array(out)


def footprint(cx, cy, length, width, heading) -> list[list[float]]:
    c, s = math.cos(heading), math.sin(heading)
    return [[round(cx + c * dx - s * dy, 4), round(cy + s * dx + c * dy, 4)]
            for dx, dy in ((length / 2, width / 2), (length / 2, -width / 2), (-length / 2, -width / 2), (-length / 2, width / 2))]


def segment_hits_box(p0, p1, box) -> bool:
    """Segment p0->p1 (excluding the last 2 cm) intersects an oriented box {cx, cy, l, w, h, heading}."""
    c, s = math.cos(-box["heading"]), math.sin(-box["heading"])
    def local(p):
        dx, dy = p[0] - box["cx"], p[1] - box["cy"]
        return np.array([c * dx - s * dy, s * dx + c * dy, p[2]])
    a, b = local(p0), local(p1)
    d = b - a
    lo = np.array([-box["l"] / 2, -box["w"] / 2, 0.0])
    hi = np.array([box["l"] / 2, box["w"] / 2, box["h"]])
    t0, t1 = 0.0, 1.0 - 0.02 / max(np.linalg.norm(d), 1e-9)
    for k in range(3):
        if abs(d[k]) < 1e-12:
            if a[k] < lo[k] or a[k] > hi[k]:
                return False
            continue
        u, v = (lo[k] - a[k]) / d[k], (hi[k] - a[k]) / d[k]
        t0, t1 = max(t0, min(u, v)), min(t1, max(u, v))
        if t0 > t1:
            return False
    return True


def in_image(uv) -> bool:
    return 0 <= uv[0] < W and 0 <= uv[1] < H


def bbox_of(cam: Cam, corners) -> list[float] | None:
    uv, front = cam.project(corners)
    if not front.all():
        return None
    x1, y1 = np.clip(uv.min(0), 0, [W - 1, H - 1])
    x2, y2 = np.clip(uv.max(0), 0, [W - 1, H - 1])
    if (x2 - x1) * (y2 - y1) < 20:
        return None
    return [round(float(v), 1) for v in (x1, y1, x2, y2)]


COCO17 = np.array([  # standing template, local frame x forward, y left, z up (m)
    [0.08, 0.0, 1.65], [0.07, 0.03, 1.68], [0.07, -0.03, 1.68], [0.0, 0.07, 1.66], [0.0, -0.07, 1.66],
    [0.0, 0.19, 1.42], [0.0, -0.19, 1.42], [0.02, 0.22, 1.12], [0.02, -0.22, 1.12], [0.05, 0.22, 0.85],
    [0.05, -0.22, 0.85], [0.0, 0.11, 0.92], [0.0, -0.11, 0.92], [0.02, 0.11, 0.5], [0.02, -0.11, 0.5],
    [0.0, 0.11, 0.08], [0.0, -0.11, 0.08]])


def joints_at(x, y, heading, phase) -> np.ndarray:
    j = COCO17.copy()
    swing = 0.18 * math.sin(phase)
    j[[13, 15], 0] += swing
    j[[14, 16], 0] -= swing
    j[[7, 9], 0] -= swing * 0.6
    j[[8, 10], 0] += swing * 0.6
    c, s = math.cos(heading), math.sin(heading)
    return np.c_[x + c * j[:, 0] - s * j[:, 1], y + s * j[:, 0] + c * j[:, 1], j[:, 2]]


def r4(v) -> float:
    return round(float(v), 4)


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")


def sha256_file(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------- warehouse scene

WH_ID = "wh/sdg/fixture-01"
WH_KEY = scene_key_of(WH_ID)
WH_CAMS = [
    Cam("ceiling_00", (2.5, -9.0, 6.0), (0.0, 0.0, 0.0), (480.0, 482.0, 316.0, 184.5)),
    Cam("ceiling_04", (9.0, 4.0, 6.5), (1.0, 0.5, 0.0), (500.0, 500.0, 325.5, 176.0), offset_ms=120),
    Cam("eye_00", (-7.5, 1.5, 1.7), (2.0, -0.5, 1.0), (450.0, 452.0, 320.0, 180.0)),
]
CAM_ORDER = [c.id for c in WH_CAMS]
GROUND = {"origin": [-8.0, -5.0], "cell_m": 0.5, "nx": 32, "ny": 20, "cam_bit_order": CAM_ORDER,
          "texture": None, "texture_from_world": None}
RACK = {"cx": 1.0, "cy": 1.0, "l": 3.0, "w": 0.8, "h": 2.4, "heading": 0.0}
FK_DIMS = (2.4, 1.2, 2.1)
NO_VIDEO = {"eye_00": [(6000, 7000)]}
SINGLE_VIEW = {"P2": ((2000, 2300), "ceiling_00")}  # forced: P2 is hidden from the other cams in this window
TRACK_IDS = {("ceiling_00", "P1"): 1, ("ceiling_00", "P2"): 2, ("ceiling_00", "F1"): 3,
             ("ceiling_04", "P1"): 7, ("ceiling_04", "P2"): 5, ("ceiling_04", "F1"): 6,
             ("eye_00", "P1"): 11, ("eye_00", "P2"): 12, ("eye_00", "F1"): 13}
EYE_REID = {("eye_00", "P1"): 14}  # P1 gets a new local track after the eye_00 video gap


def p1(t):
    ts = t / 1000
    dodge = 0.0 if ts < 4.6 else 0.8 * min(ts - 4.6, 1.0)
    return -4.0 + 0.9 * ts, -1.5 + dodge


def p2(t):
    ts = t / 1000
    return 5.0 - 0.7 * ts, 3.0 + 0.05 * ts


def f1(t):
    return 5.0 - 0.8 * t / 1000, -2.5, math.pi, 0.8


def heading_of(fn, t):
    a, b = fn(max(t - 50, 0)), fn(t + 50)
    return math.atan2(b[1] - a[1], b[0] - a[0])


def has_video(cam_id, t) -> bool:
    cam = next(c for c in WH_CAMS if c.id == cam_id)
    if t < cam.offset_ms:
        return False
    return not any(a <= t < b for a, b in NO_VIDEO.get(cam_id, []))


def fk_box(t):
    x, y, hd, _ = f1(t)
    return {"cx": x, "cy": y, "l": FK_DIMS[0], "w": FK_DIMS[1], "h": FK_DIMS[2], "heading": hd}


def person_seen(cam: Cam, pid, xy, t) -> bool:
    if not has_video(cam.id, t):
        return False
    if pid in SINGLE_VIEW:
        (a, b), only = SINGLE_VIEW[pid]
        if a <= t < b and cam.id != only:
            return False
    target = np.array([xy[0], xy[1], 0.9])
    uv, front = cam.project(target)
    if not front[0] or not in_image(uv[0]):
        return False
    return not (segment_hits_box(cam.center, target, RACK) or segment_hits_box(cam.center, target, fk_box(t)))


def floor_state(cam: Cam, x, y, t) -> int:
    """1 visible, 2 occluded, 3 out_of_fov, 4 no_video (visibility.json states)."""
    if not has_video(cam.id, t):
        return 4
    p = np.array([x, y, 0.0])
    uv, front = cam.project(p)
    if not front[0] or not in_image(uv[0]):
        return 3
    if segment_hits_box(cam.center, p, RACK) or segment_hits_box(cam.center, p, fk_box(t)):
        return 2
    return 1


def floor_cell(x, y) -> tuple[int, int]:
    return int((x - GROUND["origin"][0]) // GROUND["cell_m"]), int((y - GROUND["origin"][1]) // GROUND["cell_m"])


def build_warehouse(root: Path) -> dict:
    """Writes the scene; returns facts the asks and index need."""
    times = list(range(0, T1, STEP))
    people = {"P1": p1, "P2": p2}
    rows = {c.id: [] for c in WH_CAMS}
    seen = {}  # (pid, t) -> [cam ids]
    for t in times:
        for pid, fn in people.items():
            x, y = fn(t)
            cams_seen = []
            for cam in WH_CAMS:
                if not person_seen(cam, pid, (x, y), t):
                    continue
                bb = bbox_of(cam, box_corners(x, y, 0.5, 0.5, 1.75))
                if bb is None:
                    continue
                tid = TRACK_IDS[(cam.id, pid)]
                if (cam.id, pid) in EYE_REID and t >= 7000:
                    tid = EYE_REID[(cam.id, pid)]
                rows[cam.id].append([t, tid, "person", *bb, 0.9])
                cams_seen.append(cam.id)
            seen[(pid, t)] = cams_seen
        x, y, hd, _ = f1(t)
        for cam in WH_CAMS:
            if not has_video(cam.id, t):
                continue
            uv, front = cam.project(np.array([x, y, 1.0]))
            if not front[0] or not in_image(uv[0]):
                continue
            bb = bbox_of(cam, box_corners(x, y, *FK_DIMS, hd))
            if bb is not None:
                rows[cam.id].append([t, TRACK_IDS[(cam.id, "F1")], "forklift", *bb, 0.85])
                seen.setdefault(("F1", t), []).append(cam.id)

    for cam in WH_CAMS:
        r = sorted(rows[cam.id], key=lambda v: (v[0], v[1]))
        write_json(root / f"boxes/{cam.id}/00000.json", {**HEADER, "cam_id": cam.id, "chunk_t0_ms": 0, "rows": r})

    # entities: one observation per (cam, local track), from box rows
    obs = {}
    for cam in WH_CAMS:
        for t, tid, cls, *_ in rows[cam.id]:
            o = obs.setdefault((cam.id, tid), [t, t])
            o[0], o[1] = min(o[0], t), max(o[1], t + STEP)
    owner = {v: k[1] for k, v in TRACK_IDS.items()} | {v: k[1] for k, v in EYE_REID.items()}
    entities = []
    for eid_, kind in (("P1", "person"), ("P2", "person"), ("F1", "forklift")):
        ob = [{"cam_id": c, "local_track_id": tid, "t0_ms": a, "t1_ms": min(b, T1), "method": "bytetrack", "conf": 0.9}
              for (c, tid), (a, b) in sorted(obs.items()) if owner[tid] == eid_]
        entities.append({"entity_id": eid_, "kind": kind, "observations": ob,
                         "association": {"method": "ground_dist", "conf": 0.95, "valid": [0, T1]}})
    write_json(root / "entities.json", {**HEADER, "entities": entities})

    # skeleton (10 fps)
    persons = {}
    for pid, fn in people.items():
        rec = {k: [] for k in ("t_ms", "root_world", "root_method", "root_sigma_m", "xyz", "conf", "n_views")}
        for t in times:
            x, y = fn(t)
            nv = len(seen[(pid, t)])
            if nv == 0:
                continue
            rec["t_ms"].append(t)
            rec["root_world"].append([r4(x), r4(y)])
            if nv >= 2:
                j = joints_at(x, y, heading_of(fn, t), t / 1000 * 2 * math.pi)
                rec["root_method"].append("tri")
                rec["root_sigma_m"].append(0.08)
                xyz, conf = [[r4(v) for v in p] for p in j], [0.9] * 17
                if pid == "P1" and 1000 <= t < 1500:  # wrists not triangulated: joints may be null inside a skeleton
                    for k in (9, 10):
                        xyz[k], conf[k] = None, 0.0
                rec["xyz"].append(xyz)
                rec["conf"].append(conf)
            else:
                rec["root_method"].append("ankle")
                rec["root_sigma_m"].append(0.40)
                rec["xyz"].append(None)
                rec["conf"].append([0.0] * 17)
            rec["n_views"].append([nv] * 17)
        persons[pid] = rec
    write_json(root / "skeleton.json", {**HEADER, "fps": 10, "joints": "coco17", "persons": persons})

    # tracks
    tracks = []
    for pid, fn in people.items():
        pts, last_obs = [], None
        for t in times:
            x, y = fn(t)
            if seen[(pid, t)]:
                state, last_obs = "observed", t
            elif last_obs is not None and t - last_obs <= 2000:
                state = "interpolated"
            else:
                state = "unknown"
            pts.append([t, r4(x), r4(y), 0.08 if len(seen[(pid, t)]) >= 2 else 0.35, state])
        cams_ = sorted({c for t in times for c in seen[(pid, t)]})
        tracks.append({"entity_id": pid, "cls": "person", "t0_ms": 0, "t1_ms": T1, "cam_ids": cams_, "pts": pts, "source": "tri"})
    fk_track = [[t, r4(f1(t)[0]), r4(f1(t)[1]), 0.15, "observed" if seen.get(("F1", t)) else "unknown"] for t in times]
    tracks.append({"entity_id": "F1", "cls": "forklift", "t0_ms": 0, "t1_ms": T1, "cam_ids": CAM_ORDER, "pts": fk_track, "source": "gt"})
    write_json(root / "tracks.json", {**HEADER, "tracks": tracks})

    # forklift
    track = []
    for t in times:
        x, y, hd, sp = f1(t)
        track.append({"t_ms": t, "x": r4(x), "y": r4(y), "heading": r4(hd), "speed": sp,
                      "footprint": footprint(x, y, FK_DIMS[0], FK_DIMS[1], hd), "sigma_m": 0.15, "source": "gt"})
    gt = [{"t_ms": t, "x": r4(f1(t)[0]), "y": r4(f1(t)[1]), "heading": r4(math.pi)} for t in times]
    write_json(root / "forklift.json", {**HEADER, "forklifts": [{"entity_id": "F1", "source": "gt", "track": track, "gt": gt}]})

    # events: near_miss (rule, from distances) + dodge (gt)
    near = [t for t in times if math.dist(p1(t), f1(t)[:2]) < 2.0 and f1(t)[3] >= 0.3]
    min_t = min(near, key=lambda t: math.dist(p1(t), f1(t)[:2]))
    min_d = math.dist(p1(min_t), f1(min_t)[:2])
    events = [
        {"id": "E1", "type": "near_miss", "t0_ms": near[0], "t1_ms": near[-1] + STEP, "actor_ids": ["P1", "F1"], "place_id": None,
         "severity": 2, "source": "rule", "params": {"min_dist_m": r4(min_d), "t_min_ms": min_t, "threshold_m": 2.0},
         "clip_ids": [f"{WH_KEY}:{c}:00000" for c in CAM_ORDER], "eids": []},
        {"id": "E2", "type": "dodge", "t0_ms": 4600, "t1_ms": 5600, "actor_ids": ["P1"], "place_id": None, "severity": 1,
         "source": "gt", "params": {"label": "Dodge"}, "clip_ids": [f"{WH_KEY}:{c}:00000" for c in CAM_ORDER], "eids": []},
    ]
    write_json(root / "events.json", {**HEADER, "events": events})

    # timeline + lanes
    lanes = {}
    for cam in WH_CAMS:
        edges = sorted({0, T1, cam.offset_ms, *[v for iv in NO_VIDEO.get(cam.id, []) for v in iv]})
        segs = []
        for a, b in zip(edges, edges[1:]):
            st = "ok" if has_video(cam.id, a) else "no_video"
            if segs and segs[-1][2] == st:
                segs[-1][1] = b
            else:
                segs.append([a, b, st])
        lanes[cam.id] = segs
    sec = []
    for s in range(T1 // 1000):
        t = s * 1000 + 500
        bits = sum(1 << i for i, c in enumerate(CAM_ORDER) if has_video(c, t))
        npers = sum(1 for pid in people if seen[(pid, t)])
        nveh = 1 if seen.get(("F1", t)) else 0
        evs = [e["id"] for e in events if e["t0_ms"] < (s + 1) * 1000 and e["t1_ms"] > s * 1000]
        sec.append([s, npers, nveh, bits, evs])
    write_json(root / "timeline.json", {**HEADER, "sec": sec, "lanes": lanes})

    # visibility (floor cell vs camera, sampled at the middle of each second)
    per_sec = []
    for s in range(T1 // 1000):
        t = s * 1000 + 500
        buf = bytearray()
        for iy in range(GROUND["ny"]):
            for ix in range(GROUND["nx"]):
                x = GROUND["origin"][0] + (ix + 0.5) * GROUND["cell_m"]
                y = GROUND["origin"][1] + (iy + 0.5) * GROUND["cell_m"]
                buf += bytes(floor_state(c, x, y, t) for c in WH_CAMS)
        per_sec.append(base64.b64encode(bytes(buf)).decode())
    write_json(root / "visibility.json", {**HEADER, **{k: GROUND[k] for k in ("cell_m", "nx", "ny", "origin", "cam_bit_order")},
                                          "row_major": True, "encoding": "u8_cell_cam",
                                          "states": {"0": "unknown", "1": "visible", "2": "occluded", "3": "out_of_fov", "4": "no_video"},
                                          "t0_s": 0, "per_sec": per_sec, "visibility_revision": REV})

    # points.bin: floor grid + rack surfaces
    pts, cols = [], []
    for x in np.arange(-8.0, 8.0001, 0.2):
        for y in np.arange(-5.0, 5.0001, 0.2):
            line = abs(x - round(x)) < 1e-6 or abs(y - round(y)) < 1e-6
            pts.append([x, y, 0.0])
            cols.append([110, 112, 120] if line else [72, 74, 80])
    rx0, rx1, ry0, ry1 = RACK["cx"] - 1.5, RACK["cx"] + 1.5, RACK["cy"] - 0.4, RACK["cy"] + 0.4
    for z in np.arange(0.0, 2.4001, 0.1):
        for x in np.arange(rx0, rx1 + 1e-6, 0.1):
            for y in (ry0, ry1):
                pts.append([x, y, z]); cols.append([168, 112, 48])
        for y in np.arange(ry0, ry1 + 1e-6, 0.1):
            for x in (rx0, rx1):
                pts.append([x, y, z]); cols.append([150, 98, 40])
    pts_a = np.round(np.array(pts, np.float32), 4).astype("<f4")
    bbox = [*pts_a.min(0).tolist(), *pts_a.max(0).tolist()]
    raw = b"SVAP" + struct.pack("<I", len(pts_a)) + struct.pack("<6f", *bbox)
    raw += pts_a.tobytes() + np.array(cols, np.uint8).tobytes()
    (root / "points.bin").write_bytes(raw)

    # cosmos C1-C5
    def frames(cam, a, b):
        return [{"cam_id": cam, "t_ms": t} for t in range(a, b, 250)]
    ref = lambda e, k: {"scene_key": WH_KEY, "entity_id": e, "kind": k}  # noqa: E731
    rack_uv, _ = WH_CAMS[0].project(box_corners(RACK["cx"], RACK["cy"], RACK["l"], RACK["w"], RACK["h"]))
    rack_box = [round(float(v), 1) for v in (rack_uv[:, 0].min() / W * 1000, rack_uv[:, 1].min() / H * 1000,
                                               rack_uv[:, 0].max() / W * 1000, rack_uv[:, 1].max() / H * 1000)]
    ghost = [[t, r4(f1(4500)[0] - 0.8 * (t - 4500) / 1000), -2.5] for t in range(4500, 6501, 500)]
    recs = [
        ("C1", 4600, "ceiling_04", [ref("P1", "person"), ref("F1", "forklift")], frames("ceiling_04", 1600, 7600), False,
         {"label": "yielding", "reason": "The worker steps sideways away from the forklift as it approaches.", "uncertainty": 0.2},
         {"status": "verified", "method": "situation_map rule wh_dodge", "ref": "labels/wh-sdg-fixture-01.json"}),
        ("C2", 4500, "ceiling_04", [ref("F1", "forklift")], frames("ceiling_04", 2500, 4500), True,
         {"actions": ["continue"], "reason": "The forklift keeps a steady heading with no braking cue.", "horizon_s": 2,
          "max_frame_ms": 4250, "actual": {"actions": ["continue"], "source": "forklift.json gt speed"}, "ghost": ghost},
         {"status": "verified", "method": "c2 rule vs forklift gt", "ref": "labels/wh-sdg-fixture-01.json"}),
        ("C3", 4500, "ceiling_00", [], frames("ceiling_00", 1500, 7500), False,
         {"objects": [{"label": "shelf", "box_0_1000": rack_box, "ground_poly": footprint(RACK["cx"], RACK["cy"], RACK["l"], RACK["w"], 0.0),
                       "depth_residual_m": 0.12, "geometric_support": True}]},
         {"status": "verified", "method": "depth ray test", "ref": None}),
        ("C4", 4500, "ceiling_04", [ref("P1", "person")], frames("ceiling_04", 1500, 7500), False,
         {"query": "when does the worker start yielding", "segments": [{"start_s": 4.75, "end_s": 5.6, "text": "worker steps back"}]},
         {"status": "verified", "method": "start error vs first Dodge frame = 0.15 s", "ref": "labels/wh-sdg-fixture-01.json"}),
        ("C5", 4500, "eye_00", [ref("F1", "forklift")], frames("eye_00", 1500, 6000), False,
         {"region_ref": "F1", "text": "A yellow counterbalance forklift with forks lowered, moving left.", "sign_text": None},
         {"status": "unverified", "method": "no labels", "ref": None}),
    ]
    cosmos = []
    for task, t, cam, actors, fr, prefix, payload, ver in recs:
        cosmos.append({"record_id": make_record_id(WH_KEY, task, t, cam, [a["entity_id"] for a in actors], "fx-prompt-1"),
                       "task": task, "t_ms": t, "cam_id": cam, "actor_refs": actors, "input_frame_refs": fr,
                       "prefix_only": prefix, "model_rev": "cosmos3-nano-reasoner@fixture", "prompt_rev": "fx-prompt-1",
                       "payload": payload, "raw": None, "verification": ver})
    write_json(root / f"cosmos/{WH_KEY}.json", {**HEADER, "scene_key": WH_KEY, "records": cosmos})

    manifest = {**HEADER, "scene_id": WH_ID, "scene_key": WH_KEY, "workspace": "warehouse", "title": "SDG fixture run 01",
                "t0_ms": 0, "t1_ms": T1, "fps": FPS, "world_frame_id": "sdg/fixture-01", "metric": True,
                "cams": [c.manifest() for c in WH_CAMS], "ground": GROUND,
                "points_meta": {"n": int(len(pts_a)), "voxel_m": 0.05, "depth_kind": "euclidean_range"},
                "assets": {"points": "points.bin", "skeleton": "skeleton.json", "forklift": "forklift.json",
                           "visibility": "visibility.json", "tracks": "tracks.json", "entities": "entities.json",
                           "boxes_dir": "boxes", "events": "events.json", "cosmos": f"cosmos/{WH_KEY}.json",
                           "timeline": "timeline.json"},
                "sources": {"generator": "fixtures/gen_fixture.py"},
                "pipeline": {"models": {"detector": "fixture-gt", "vlm": "cosmos3-nano-reasoner@fixture"}, "created": "2026-10-09T00:00:00Z"}}
    write_json(root / "manifest.json", manifest)
    return {"seen": seen, "rows": rows, "events": events, "min_t": min_t, "min_d": min_d, "cosmos": cosmos}


# ---------------------------------------------------------------- fleet scene

FL_ID, FL_KEY = "fl/pie/fixture-01", scene_key_of("fl/pie/fixture-01")
FL2_ID, FL2_KEY = "fl/pie/fixture-02", scene_key_of("fl/pie/fixture-02")
LAT0, LON0, HEAD = 43.6487, -79.3983, 72.0
DASH = Cam("dashcam", (1.5, 0.0, 1.3), (1.5 + 10 * math.cos(math.radians(5)), 0.0, 1.3 - 10 * math.sin(math.radians(5))),
           (300.0, 300.0, 320.0, 180.0), model="fisheye", dist=(-0.02, 0.005, 0.0, 0.0), sync="single_camera")
GPS_GAP = (4000, 7000)
CROSS_S = 55.0


def speed(t):
    ts = t / 1000
    if ts < 5:
        return 8.0
    if ts < 6.5:
        return 8.0 - 4.0 * (ts - 5)
    return 2.0 + 0.5 * (ts - 6.5)


S_TABLE = np.concatenate([[0.0], np.cumsum([speed(t) * 0.001 for t in range(0, 60_001)])])


def s_at(t):
    return float(S_TABLE[int(t)])


def latlon(s, lateral=0.0, lat0=LAT0, lon0=LON0, head=HEAD):
    h = math.radians(head)
    e = s * math.sin(h) - lateral * math.cos(h)
    n = s * math.cos(h) + lateral * math.sin(h)
    return lat0 + n / 111_320.0, lon0 + e / (111_320.0 * math.cos(math.radians(lat0)))


def ped_ego(t):
    lat = -6.0 + 2.0 * (t / 1000 - 3)
    return CROSS_S - s_at(t), lat


def veh_ego(t):
    return (80.0 - 10.0 * t / 1000) - s_at(t), 3.5


def ego_to_cam_pts(xyz):
    """ego-frame points through the dashcam (cam2world is camera -> ego)."""
    return DASH.project(xyz)


def gps_points(t1, lat0=LAT0, lon0=LON0, head=HEAD, gap=GPS_GAP, s_off=0.0, maxspeed=40.0):
    feats, last_fix = [], None
    for t in range(0, t1, 500):
        in_gap = gap is not None and gap[0] < t < gap[1]
        if not in_gap:
            last_fix = t
            lat, lon = latlon(s_at(t) + s_off, 0.0, lat0, lon0, head)
            state, gap_ms = "observed", 0
        else:
            lat, lon = latlon(s_at(last_fix) + s_off, 0.0, lat0, lon0, head)
            gap_ms = t - last_fix
            state = "interpolated" if gap[1] - gap[0] <= 2000 else "last_known"
        place = "pl_queen_spadina" if s_at(t) + s_off > 40 else "pl_queen_st_w"
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [round(lon, 7), round(lat, 7)]},
                      "properties": {"kind": "gps", "t_ms": t, "lat": round(lat, 7), "lon": round(lon, 7), "heading": HEAD,
                                     "speed": round(speed(t), 3), "way_id": 1000001, "node_id": 2000001 if place == "pl_queen_spadina" else None,
                                     "place_id": place, "state": state, "gap_ms": gap_ms, "maxspeed_kmh": maxspeed}})
    return feats


def trip_doc(trip_id, t1, replay_start, gap, s_off=0.0, maxspeed=40.0):
    pts = gps_points(t1, gap=gap, s_off=s_off, maxspeed=maxspeed)
    line = {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [f["geometry"]["coordinates"] for f in pts]},
            "properties": {"kind": "track", "trip_id": trip_id,
                           "replay": {"trip_id": trip_id, "replay_start_ms": replay_start, "source_start_ms": 0, "rate": 1.0,
                                      "coverage_intervals": [[0, t1]]}}}
    return {**HEADER, "type": "FeatureCollection", "trip_id": trip_id, "max_interpolation_gap_ms": 2000, "features": [line, *pts]}


BUILDINGS = {"w1001": {"side": 1, "s": (20.0, 45.0), "d": (8.0, 20.0), "name": "Fixture Block A", "h": 18.0, "floors": 5},
             "w1002": {"side": -1, "s": (50.0, 80.0), "d": (8.0, 22.0), "name": "Fixture Block B", "h": 30.0, "floors": 9}}


def build_fleet(root: Path) -> dict:
    times = list(range(0, T1, STEP))
    frames, rows = [], []
    seen = {}
    for t in times:
        s = s_at(t)
        in_gap = GPS_GAP[0] < t < GPS_GAP[1]
        if in_gap:
            lat, lon = latlon(s_at(GPS_GAP[0]))
            ego = {"lat": round(lat, 7), "lon": round(lon, 7), "heading": HEAD, "sigma_m": round(3 + 2 * (t - GPS_GAP[0]) / 1000, 2),
                   "state": "last_known"}
        else:
            lat, lon = latlon(s)
            ego = {"lat": round(lat, 7), "lon": round(lon, 7), "heading": HEAD, "sigma_m": 3.0,
                   "state": "observed" if t % 500 == 0 else "interpolated"}
        peds, vehs, blds = [], [], []
        px, py = ped_ego(t)
        if 3000 <= t < 8000 and px > 1.0:
            j = joints_at(px, py, math.pi / 2, t / 1000 * 2 * math.pi)
            uv, front = ego_to_cam_pts(j)
            kp = [[round(float(u), 1), round(float(v), 1), 0.8] for u, v in uv] if front.all() else None
            peds.append({"entity_id": "P1", "x": r4(px), "y": r4(py), "h": 1.7, "sigma_m": r4(0.3 + 0.04 * px), "kpts2d": kp,
                         "source": "fisheye_ground"})
            bb = bbox_of(DASH, box_corners(px, py, 0.5, 0.5, 1.75))
            if bb:
                rows.append([t, 1, "person", *bb, 0.88])
                seen[("P1", t)] = True
        vx, vy = veh_ego(t)
        if 2.0 < vx < 60.0:
            vehs.append({"entity_id": "V1", "x": r4(vx), "y": vy, "yaw": r4(math.pi), "cls": "car", "dims": [4.5, 1.8, 1.5],
                         "sigma_m": r4(1.0 + 0.05 * vx)})
            bb = bbox_of(DASH, box_corners(vx, vy, 4.5, 1.8, 1.5, math.pi))
            if bb:
                rows.append([t, 2, "car", *bb, 0.9])
                seen[("V1", t)] = True
        for bid, b in BUILDINGS.items():
            a, c = b["s"][0] - s, b["s"][1] - s
            if c < 2:
                continue
            a = max(a, 2.0)
            lat_ = b["side"] * b["d"][0]
            uv, front = ego_to_cam_pts(np.array([[a, lat_, 3.0], [c, lat_, 3.0]]))
            if front.all():
                u0, u1 = sorted(float(np.clip(u, 0, W - 1)) for u in uv[:, 0])
                if u1 - u0 > 2:
                    blds.append({"bid": bid, "u0": round(u0, 1), "u1": round(u1, 1)})
        frames.append({"t_ms": t, "ego": ego, "peds": peds, "vehs": vehs, "buildings": blds})
    write_json(root / "ego/00000.json", {**HEADER, "trip_id": FL_KEY, "chunk_t0_ms": 0, "frames": frames})
    write_json(root / "boxes/dashcam/00000.json", {**HEADER, "cam_id": "dashcam", "chunk_t0_ms": 0, "rows": sorted(rows, key=lambda r: (r[0], r[1]))})
    write_json(root / "trip.geojson", trip_doc(FL_KEY, T1, 0, GPS_GAP))

    def span(eid_):
        ts = [t for (e, t) in seen if e == eid_]
        return min(ts), min(max(ts) + STEP, T1)
    ents = [{"entity_id": "P1", "kind": "person",
             "observations": [{"cam_id": "dashcam", "local_track_id": 1, "t0_ms": span("P1")[0], "t1_ms": span("P1")[1], "method": "bytetrack", "conf": 0.88}],
             "association": {"method": "gt", "conf": 1.0, "valid": [0, T1]}},
            {"entity_id": "V1", "kind": "vehicle",
             "observations": [{"cam_id": "dashcam", "local_track_id": 2, "t0_ms": span("V1")[0], "t1_ms": span("V1")[1], "method": "bytetrack", "conf": 0.9}],
             "association": {"method": "gt", "conf": 1.0, "valid": [0, T1]}}]
    write_json(root / "entities.json", {**HEADER, "entities": ents})

    near = [t for t in times if 0 < ped_ego(t)[0] < 10 and abs(ped_ego(t)[1]) < 2 and 3000 <= t < 8000]
    events = [
        {"id": "E1", "type": "ped_crossing", "t0_ms": 3000, "t1_ms": 8000, "actor_ids": ["P1"], "place_id": "pl_queen_spadina",
         "severity": 2, "source": "rule", "params": {"min_dist_m": r4(min(ped_ego(t)[0] for t in near)), "near_t0_ms": near[0]},
         "clip_ids": [f"{FL_KEY}:dashcam:00000", f"{FL_KEY}:dashcam:00001"], "eids": []},
        {"id": "E2", "type": "hard_brake", "t0_ms": 5000, "t1_ms": 6500, "actor_ids": [], "place_id": "pl_queen_spadina",
         "severity": 1, "source": "rule", "params": {"decel_mps2": -4.0}, "clip_ids": [f"{FL_KEY}:dashcam:00001"], "eids": []},
    ]
    write_json(root / "events.json", {**HEADER, "events": events})
    sec = []
    for s_ in range(T1 // 1000):
        t = s_ * 1000 + 500
        sec.append([s_, int(("P1", t) in seen), int(("V1", t) in seen), 1,
                    [e["id"] for e in events if e["t0_ms"] < (s_ + 1) * 1000 and e["t1_ms"] > s_ * 1000]])
    write_json(root / "timeline.json", {**HEADER, "sec": sec, "lanes": {"dashcam": [[0, T1, "ok"]]}})

    bl = {}
    for bid, b in BUILDINGS.items():
        ring = [latlon(sv, b["side"] * dv) for sv, dv in ((b["s"][0], b["d"][0]), (b["s"][1], b["d"][0]), (b["s"][1], b["d"][1]),
                                                           (b["s"][0], b["d"][1]), (b["s"][0], b["d"][0]))]
        bl[bid] = {"name": b["name"], "addr": None, "height_m": b["h"], "floors": b["floors"], "pois": [{"name": "Fixture Cafe", "kind": "cafe"}],
                   "wikidata": None, "osm_id": f"way/{bid[1:]}",
                   "geom": {"type": "Polygon", "coordinates": [[[round(lo, 7), round(la, 7)] for la, lo in ring]]},
                   "source": "osm", "map_snapshot_date": "2026-10-01"}
    write_json(root / "buildings.json", {**HEADER, "map_snapshot_date": "2026-10-01", "buildings": bl})

    ref = {"scene_key": FL_KEY, "entity_id": "P1", "kind": "person"}
    cosmos = []
    for task, t, fr, prefix, payload, ver in (
        ("C1", 6000, [{"cam_id": "dashcam", "t_ms": x} for x in range(3000, 9000, 250)], False,
         {"label": "crossing", "reason": "The pedestrian walks across the lane in front of the car.", "uncertainty": 0.15},
         {"status": "verified", "method": "situation_map rule pie_crossing", "ref": None}),
        ("C2", 5000, [{"cam_id": "dashcam", "t_ms": x} for x in range(3000, 5000, 250)], True,
         {"actions": ["slow", "stop"], "reason": "A pedestrian is entering the lane ahead.", "horizon_s": 2, "max_frame_ms": 4750,
          "actual": {"actions": ["slow"], "source": "trip speed"}, "ghost": None},
         {"status": "verified", "method": "c2 rule vs OBD speed", "ref": None}),
        ("C4", 6000, [{"cam_id": "dashcam", "t_ms": x} for x in range(3000, 9000, 250)], False,
         {"query": "when does the pedestrian step into the lane", "segments": [{"start_s": 5.2, "end_s": 7.9, "text": "pedestrian in lane"}]},
         {"status": "unverified", "method": "no truth for this query", "ref": None}),
        ("C5", 2000, [{"cam_id": "dashcam", "t_ms": 2000}], False,
         {"region_ref": "w1001", "text": "Brick facade with a cafe sign at street level.", "sign_text": "FIXTURE CAFE"},
         {"status": "conflict", "method": "sign vs OSM name", "ref": None}),
    ):
        actors = [] if task == "C5" else [ref]
        cosmos.append({"record_id": make_record_id(FL_KEY, task, t, "dashcam", [a["entity_id"] for a in actors], "fx-prompt-1"),
                       "task": task, "t_ms": t, "cam_id": "dashcam", "actor_refs": actors, "input_frame_refs": fr, "prefix_only": prefix,
                       "model_rev": "cosmos3-nano-reasoner@fixture", "prompt_rev": "fx-prompt-1", "payload": payload, "raw": None,
                       "verification": ver})
    write_json(root / f"cosmos/{FL_KEY}.json", {**HEADER, "scene_key": FL_KEY, "records": cosmos})

    manifest = {**HEADER, "scene_id": FL_ID, "scene_key": FL_KEY, "workspace": "fleet", "title": "PIE fixture trip 01",
                "t0_ms": 0, "t1_ms": T1, "fps": FPS, "world_frame_id": "wgs84+ego_flu", "metric": True,
                "cams": [DASH.manifest()],
                "assets": {"trip": "trip.geojson", "ego_dir": "ego", "buildings": "buildings.json", "entities": "entities.json",
                           "events": "events.json", "timeline": "timeline.json", "boxes_dir": "boxes", "cosmos": f"cosmos/{FL_KEY}.json",
                           "places": "places.json"},
                "sources": {"generator": "fixtures/gen_fixture.py"},
                "pipeline": {"models": {"detector": "fixture-gt"}, "created": "2026-10-09T00:00:00Z"}}
    write_json(root / "places.json", {**HEADER, "trip_id": FL_KEY, "places": [
        {"place_id": pid, "kind": kind, "name": name, "aliases": aliases, "lat": la, "lon": lo, "geom": None}
        for pid, kind, name, aliases, la, lo in fixture_places()]})
    write_json(root / "manifest.json", manifest)
    return {"seen": seen, "events": events, "cosmos": cosmos}


def fixture_places() -> list[tuple]:
    lat, lon = latlon(CROSS_S)
    return [("pl_queen_spadina", "junction", "Queen St W & Spadina Ave", ["Queen and Spadina", "Queen & Spadina"], lat, lon),
            ("pl_queen_st_w", "road", "Queen St W", ["Queen Street West"], LAT0, LON0),
            ("pl_rom", "landmark", "Royal Ontario Museum", ["ROM"], 43.6677, -79.3948)]


def build_fleet_gps_only(root: Path) -> None:
    write_json(root / "trip.geojson", trip_doc(FL2_KEY, 60_000, 0, None, s_off=-200.0, maxspeed=None))
    write_json(root / "manifest.json", {**HEADER, "scene_id": FL2_ID, "scene_key": FL2_KEY, "workspace": "fleet",
                                        "title": "PIE fixture trip 02 (GPS only)", "t0_ms": 0, "t1_ms": 60_000, "fps": FPS,
                                        "world_frame_id": "wgs84", "metric": False, "cams": [], "assets": {"trip": "trip.geojson"},
                                        "sources": {"generator": "fixtures/gen_fixture.py"},
                                        "pipeline": {"models": {}, "created": "2026-10-09T00:00:00Z"}})


# ---------------------------------------------------------------- street scene (L1)

ST_ID = "st/fixture_cam_1/clip01"
ST_KEY = scene_key_of(ST_ID)
ST_CAM = SimpleNamespace(id="fixture_cam-1", offset_ms=0)
ST_CHUNK = f"chunks/{ST_CAM.id}/0000.mp4"
ST_DETS = f"dets/{ST_CAM.id}/0000.json"
CAR_T = (2000, 8000)


def st_person(t) -> list[float]:
    x = 60 + 50 * t / 1000
    return [round(x, 1), 245.0, round(x + 28, 1), 325.0]


def st_car(t) -> list[float] | None:
    if not CAR_T[0] <= t < CAR_T[1]:
        return None
    cx = 700 - 130 * (t - CAR_T[0]) / 1000
    x1, x2 = max(cx - 60, 0.0), min(cx + 60, W - 1.0)
    return [round(x1, 1), 200.0, round(x2, 1), 270.0] if x2 - x1 > 10 else None


def build_street(root: Path) -> dict:
    rows = []
    for t in range(0, T1, STEP):
        rows.append([t, "person", *st_person(t), 0.86])
        if st_car(t):
            rows.append([t, "car", *st_car(t), 0.91])
    write_json(root / ST_DETS, {**HEADER, "cam_id": ST_CAM.id, "chunk_index": 0, "chunk_t0_ms": 0, "source": "yolo11_coco",
                                "rows": rows})
    cam = {"kind": "cam2d", "id": ST_CAM.id, "w": W, "h": H, "video": f"video/{ST_CAM.id}.mp4",
           "chunks": [{"t0_ms": 0, "t1_ms": T1, "uri": ST_CHUNK}],
           "frame_ref": {"video_w": W, "video_h": H, "thumb_w": 320, "thumb_h": 180}, "offset_ms": 0,
           "thumbs": f"thumbs/{ST_CAM.id}", "sync_method": "single_camera", "sync_residual_ms": None}
    write_json(root / "manifest.json", {
        **HEADER, "scene_id": ST_ID, "scene_key": ST_KEY, "workspace": "street", "evidence_level": "L1",
        "title": "Street fixture camera 1 (clip01)", "t0_ms": 0, "t1_ms": T1, "fps": FPS, "world_frame_id": "image",
        "metric": False, "cams": [cam], "assets": {"dets_dir": "dets"},
        "sources": {"generator": "fixtures/gen_fixture.py", "vss": {"camera_id": ST_CAM.id, "location": "fixture", "stem": "clip01"}},
        "pipeline": {"models": {"detector": "fixture-gt"}, "created": "2026-10-09T00:00:00Z"}})
    return {"rows": rows}


# ---------------------------------------------------------------- asks, runs, labels

def ev(**kw) -> dict:
    d = Evidence.create(analysis_rev=REV, **kw).model_dump()
    if d["evidence_level"] is None:  # keeps the pre-L1 fixtures byte-identical
        del d["evidence_level"]
    return d


def obs(scene_key, cam, tid, t) -> ObservationRef:
    return ObservationRef(scene_key=scene_key, cam_id=cam, local_track_id=tid, t_ms=t)


class Seq:
    def __init__(self, run_id):
        self.run_id, self.frames, self.i, self.calls = run_id, [], 0, 0
        self.steps: list[dict] = []
        self.cur = None

    def step(self, state, summary, elapsed):
        sid = f"{self.run_id}.s{self.i}"
        self.cur = sid
        self.frames.append({"event": "step", "data": {"run_id": self.run_id, "step_id": sid, "i": self.i, "state": state,
                                                      "elapsed_ms": elapsed, "summary": summary, "exec_state": "waiting"}})
        name = {"parse": "parse", "plan": "policy.plan_next", "retrieve": "retrieve", "answer": "answer"}.get(state, "assess")
        self.steps.append({"step_id": sid, "parent_step_id": None, "i": self.i, "state": state, "name": name,
                           "started_ms": elapsed, "elapsed_ms": 5, "input": {"summary": summary}, "output": None,
                           "evidence_ids": [], "note": summary, "exec_state": "precomputed", "weave_call_id": None,
                           "trace_delivery_state": "local", "blob_ref": None})
        self.i += 1

    def tool(self, name, args, why, evidence, output, elapsed, pts_read=1, model_rev=None, scene_key=None, cam=None, rng=(0, T1)):
        cid = f"{self.run_id}.s{self.i}"
        state = self.steps[-1]["state"]
        env = {"input_range": {"scene_key": scene_key or args.get("scene_key", WH_KEY), "cam_id": cam, "t0_ms": rng[0], "t1_ms": rng[1]},
               "pts_read": pts_read, "model_rev": model_rev, "output": output, "coverage": 1.0, "gaps": [], "quality": "ok",
               "elapsed_ms": 12, "exec_state": "precomputed", "error": None}
        self.frames.append({"event": "tool_call", "data": {"run_id": self.run_id, "step_id": self.cur, "call_id": cid, "name": name,
                                                           "args": args, "why": why}})
        self.frames.append({"event": "tool_result", "data": {"run_id": self.run_id, "step_id": self.cur, "call_id": cid, "ok": True,
                                                             "envelope": env, "evidence": evidence}})
        self.steps.append({"step_id": cid, "parent_step_id": self.cur, "i": self.i, "state": state, "name": f"tool.{name}",
                           "started_ms": elapsed, "elapsed_ms": 12, "input": args, "output": env,
                           "evidence_ids": [e["eid"] for e in evidence], "note": why, "exec_state": "precomputed",
                           "weave_call_id": None, "trace_delivery_state": "local", "blob_ref": None})
        self.i += 1

    def emit(self, event, data):
        self.frames.append({"event": event, "data": {"run_id": self.run_id, **data}})

    def end(self, final, stop_reason):
        self.emit("status", {"final": final, "cached": False, "source_run_id": None, "weave_url": None, "stop_reason": stop_reason})
        self.emit("done", {})


def wh_main(wh) -> tuple[dict, dict]:
    run = "r_fixturewh0001"
    q = Seq(run)
    P = lambda e, k: {"scene_key": WH_KEY, "entity_id": e, "kind": k}  # noqa: E731
    t_hit = 4600
    q.step("parse", "Template find: worker, forklift; action dodge", 4)
    q.step("retrieve", "Hard-filter preselection → text and visual top-50 each → RRF", 30)
    e_cap = ev(scene_key=WH_KEY, cam_id="ceiling_04", t0_ms=0, t1_ms=5000, observation_ids=[], kind="model",
               method="caption_embed1_rrf", value={"rank_text": 1, "rank_visual": 2, "rrf": 0.0325})
    e_dodge = ev(scene_key=WH_KEY, cam_id=None, t0_ms=4600, t1_ms=5600, observation_ids=[obs(WH_KEY, "ceiling_04", 7, 4600)],
                 kind="measure", method="event_gt_dodge", value={"event_id": f"{WH_KEY}:E2", "t0_ms": 4600})
    q.tool("search", {"q": "The worker dodges the forklift", "workspace": "warehouse"}, "Semantic retrieval of candidate moments", [e_cap, e_dodge],
           {"n_cards": 1}, 32, pts_read=6)
    moment = {"moment_id": f"m_{WH_KEY}_4600", "scene_key": WH_KEY, "t0_ms": 4600, "t1_ms": 5600, "cams": CAM_ORDER,
              "primary_cam": "ceiling_00", "actor_refs": [P("P1", "person"), P("F1", "forklift")],
              "evidence_ids": [e_cap["eid"], e_dodge["eid"]], "score": 0.0325,
              "why": ["caption rank 1 · visual rank 2", "Dodge event starts at 4.6 s"], "thumb": f"/api/assets/{WH_KEY}/thumbs/ceiling_00/00004.jpg",
              "exec_state": "precomputed", "backend": "duckdb", "clip_id": f"{WH_KEY}:ceiling_04:00000", "vss_rank": None, "time_filter": None}
    q.emit("hits", {"query_id": "q1", "moments": [moment]})
    q.emit("model_result", {"scene_key": WH_KEY, "t_ms": 4500, "results": [r for r in wh["cosmos"] if r["task"] in ("C1", "C2", "C4")]})
    q.emit("ui", {"cmd": "seek", "args": {"t_ms": t_hit, "scene_key": WH_KEY}})
    q.emit("ui", {"cmd": "highlight", "args": {"ids": ["P1", "F1"], "mode": "outline"}})
    q.step("inspect", "Primary camera ceiling_00: the floor cell containing P1 is occluded", 60)
    cell = list(floor_cell(*p1(t_hit)))
    e_vis0 = ev(scene_key=WH_KEY, cam_id="ceiling_00", t0_ms=4000, t1_ms=5000, observation_ids=[], kind="measure",
                method="floor_cell_visibility", params={"cell": cell}, value={"cell": cell, "state": "occluded"})
    q.tool("visibility", {"scene_key": WH_KEY, "t_ms": t_hit, "target": "P1", "cam": "ceiling_00"}, "Can the primary camera see the floor cell containing P1?",
           [e_vis0], {"floor_cell": {"seen_by": ["ceiling_04", "eye_00"], "hidden_from": ["ceiling_00"]}}, 62, cam="ceiling_00", rng=(4000, 5000))
    pred = {"predicate_id": "p1", "subject_refs": [P("P1", "person"), P("F1", "forklift")], "operator": "lt", "value": 2.0, "unit": "m"}
    q.emit("claim", {"claim_id": "c1", "text": "The worker was less than 2 m from the forklift when dodging started", "predicates": [{**pred, "status": "unknown", "eids": [e_vis0["eid"]]}],
                     "aggregate": "UNRESOLVED", "before": None, "after": "UNRESOLVED", "examined_eids": [e_cap["eid"], e_vis0["eid"]],
                     "available_eids": [], "explain": None})
    q.step("plan", "Candidates: ceiling_04 gain 0.62 · eye_00 gain 0.41 → choose ceiling_04", 70)
    q.step("measure", "ceiling_04 + eye_00 triangulated distance measurement", 80)
    seen_cams = wh["seen"][("P1", t_hit)]
    d = math.dist(p1(t_hit), f1(t_hit)[:2])
    e_dist = ev(scene_key=WH_KEY, cam_id="ceiling_04", t0_ms=t_hit, t1_ms=t_hit + STEP,
                observation_ids=[obs(WH_KEY, c, TRACK_IDS[(c, "P1")], t_hit) for c in seen_cams] + [obs(WH_KEY, "ceiling_04", 6, t_hit)],
                kind="measure", method="ground_distance_tri", params={"a": "P1", "b": "F1"}, value=round(d, 2), unit="m", sigma=0.12)
    q.tool("measure", {"scene_key": WH_KEY, "t_ms": t_hit, "a": P("P1", "person"), "b": P("F1", "forklift"), "kind": "distance"},
           "Visible in the second camera; measure the P1–F1 ground distance", [e_dist], {"value": round(d, 2), "sigma": 0.12, "method": "ground_distance_tri"}, 84,
           cam="ceiling_04", rng=(t_hit, t_hit + STEP))
    c1 = next(r for r in wh["cosmos"] if r["task"] == "C1")["record_id"]
    c2 = next(r for r in wh["cosmos"] if r["task"] == "C2")["record_id"]
    q.emit("claim", {"claim_id": "c1", "text": "The worker was less than 2 m from the forklift when dodging started", "predicates": [{**pred, "status": "supported", "eids": [e_dist["eid"]]}],
                     "aggregate": "SUPPORTED", "before": "UNRESOLVED", "after": "SUPPORTED",
                     "examined_eids": [e_cap["eid"], e_vis0["eid"], e_dist["eid"]], "available_eids": [], "explain": {"c1": c1, "c2": c2}})
    q.step("answer", "Template sentence + measurement interpolation", 90)
    q.emit("answer", {"sentence_id": "a1", "text": f"The worker started to dodge at 4.6 s, {round(d, 2)} m (±0.12 m) from the forklift.", "claim_ids": ["c1"],
                      "cites": [e_dodge["eid"], e_dist["eid"]], "measure_refs": [{"eid": e_dodge["eid"], "field": "value.t0_ms"},
                                                                                {"eid": e_dist["eid"], "field": "value"}]})
    q.end("SUPPORTED", "enough_evidence")
    ask = {"ask_id": "wh-main", "workspace": "warehouse", "q": "When did the worker start to dodge the forklift, and how far was it?",
           "context": {"workspace": "warehouse", "scene_key": WH_KEY}, "budget": {"tools": 8, "seconds": 25},
           "expect": {"final": "SUPPORTED", "template": "find", "n_moments": 1}, "events": q.frames}
    evidence = [e for f in q.frames if f["event"] == "tool_result" for e in f["data"]["evidence"]]
    claims = [f["data"] for f in q.frames if f["event"] == "claim"][-1:]
    run_doc = {"run_id": run, "kind": "ask", "q": ask["q"], "context": ask["context"], "status": "done", "final": "SUPPORTED",
               "created": "2026-10-09T00:00:00Z", "elapsed_ms": 95, "source_run_id": None, "weave_url": None,
               "model_revs": {"vlm": "cosmos3-nano-reasoner@fixture"}, "steps": q.steps, "claims": claims, "evidence": evidence,
               "hits": [moment]}
    return ask, run_doc


def wh_refuse() -> dict:
    q = Seq("r_fixturewh0002")
    q.step("parse", "Out of scope: identity recognition", 3)
    q.end("UNSUPPORTED", "abstained")
    return {"ask_id": "wh-refuse", "workspace": "warehouse", "q": "Who is the person in eye_00?",
            "context": {"workspace": "warehouse", "scene_key": WH_KEY}, "budget": {"tools": 8, "seconds": 25},
            "expect": {"final": "UNSUPPORTED", "template": "unsupported", "n_moments": 0}, "events": q.frames}


def wh_nomatch() -> dict:
    q = Seq("r_fixturewh0003")
    q.step("parse", "Template find: bicycle", 3)
    q.step("retrieve", "Hard-filter preselection → retrieval through both channels", 20)
    q.tool("search", {"q": "A person riding a bicycle", "workspace": "warehouse"}, "Semantic retrieval of candidate moments", [], {"n_cards": 0}, 22, pts_read=0)
    q.emit("hits", {"query_id": "q1", "moments": []})
    q.end("NO_MATCH", "abstained")
    return {"ask_id": "wh-nomatch", "workspace": "warehouse", "q": "Is there anyone riding a bicycle in the warehouse?",
            "context": {"workspace": "warehouse", "scene_key": WH_KEY}, "budget": {"tools": 8, "seconds": 25},
            "expect": {"final": "NO_MATCH", "template": "find", "n_moments": 0}, "events": q.frames}


def fl_where(fl) -> dict:
    q = Seq("r_fixturefl0001")
    q.step("parse", "Template where_is: place Queen and Spadina; event pedestrian crossing", 4)
    q.step("retrieve", "geocode → find_passes → events", 20)
    lat, lon = latlon(CROSS_S)
    e_geo = ev(scene_key=FL_KEY, cam_id=None, t0_ms=0, t1_ms=T1, observation_ids=[], kind="map", method="geocode_rapidfuzz",
               params={"name": "Queen and Spadina"}, value={"place_id": "pl_queen_spadina", "lat": round(lat, 6), "lon": round(lon, 6), "score": 0.96})
    q.tool("geocode", {"name": "Queen and Spadina"}, "Resolve the place name", [e_geo], {"place_id": "pl_queen_spadina"}, 21, scene_key=FL_KEY)
    e_pass = ev(scene_key=FL_KEY, cam_id=None, t0_ms=3000, t1_ms=T1, observation_ids=[], kind="map", method="gps_pass",
                params={"place_id": "pl_queen_spadina", "r_m": 60}, value={"trip_id": FL_KEY, "t0_ms": 3000, "t1_ms": T1})
    q.tool("find_passes", {"place_id": "pl_queen_spadina", "r_m": 60, "fov_deg": 120}, "Which trips pass this junction?", [e_pass],
           {"passes": 1}, 23, scene_key=FL_KEY)
    e_evt = ev(scene_key=FL_KEY, cam_id="dashcam", t0_ms=3000, t1_ms=8000, observation_ids=[obs(FL_KEY, "dashcam", 1, 6000)],
               kind="measure", method="event_rule_ped_crossing", value={"event_id": f"{FL_KEY}:E1", "severity": 2, "t0_ms": 3000})
    q.tool("events", {"scene_key": FL_KEY, "t0_ms": 3000, "t1_ms": T1}, "Rule events in this interval", [e_evt], {"n": 1}, 25, scene_key=FL_KEY,
           cam="dashcam", rng=(3000, T1))
    moment = {"moment_id": f"m_{FL_KEY}_3000", "scene_key": FL_KEY, "t0_ms": 3000, "t1_ms": 8000, "cams": ["dashcam"],
              "primary_cam": "dashcam", "actor_refs": [{"scene_key": FL_KEY, "entity_id": "P1", "kind": "person"}],
              "evidence_ids": [e_pass["eid"], e_evt["eid"]], "score": 0.9, "why": ["Passing Queen and Spadina", "Rule event ped_crossing severity 2"],
              "thumb": f"/api/assets/{FL_KEY}/thumbs/dashcam/00006.jpg", "exec_state": "precomputed", "backend": "duckdb",
              "clip_id": f"{FL_KEY}:dashcam:00001", "vss_rank": None, "time_filter": None}
    q.emit("hits", {"query_id": "q1", "moments": [moment]})
    q.emit("model_result", {"scene_key": FL_KEY, "t_ms": 6000, "results": [r for r in fl["cosmos"] if r["task"] in ("C1", "C2", "C4")]})
    q.emit("ui", {"cmd": "focus", "args": {"latlon": [round(lat, 6), round(lon, 6)], "ms": 800}})
    q.emit("ui", {"cmd": "seek", "args": {"t_ms": 6000, "trip_id": FL_KEY}})
    q.step("assess", "The event predicate is supported by a rule event", 40)
    q.emit("claim", {"claim_id": "c1", "text": "A pedestrian crossed in front of the car near Queen and Spadina",
                     "predicates": [{"predicate_id": "p1", "subject_refs": [{"scene_key": FL_KEY, "entity_id": "P1", "kind": "person"}],
                                     "operator": "exists", "value": "ped_crossing", "unit": None, "status": "supported", "eids": [e_evt["eid"]]}],
                     "aggregate": "SUPPORTED", "before": None, "after": "SUPPORTED", "examined_eids": [e_geo["eid"], e_pass["eid"], e_evt["eid"]],
                     "available_eids": [], "explain": None})
    q.step("answer", "Template sentence", 45)
    q.emit("answer", {"sentence_id": "a1", "text": "A pedestrian crossed in front of the car in trip fixture-01 starting at 3.0 s (Queen and Spadina).", "claim_ids": ["c1"],
                      "cites": [e_evt["eid"], e_geo["eid"]], "measure_refs": [{"eid": e_evt["eid"], "field": "value.t0_ms"}]})
    q.end("SUPPORTED", "enough_evidence")
    return {"ask_id": "fl-where", "workspace": "fleet", "q": "Near the Queen and Spadina junction, did any pedestrian cross in front of the car?",
            "context": {"workspace": "fleet"}, "budget": {"tools": 8, "seconds": 25},
            "expect": {"final": "SUPPORTED", "template": "where_is", "n_moments": 1}, "events": q.frames}


def labels(wh) -> dict:
    P = lambda e, k: {"scene_key": WH_KEY, "entity_id": e, "kind": k}  # noqa: E731
    return {**HEADER, "labels": [
        {"scene_key": WH_KEY, "t_ms": 4600, "actor_refs": [P("P1", "person"), P("F1", "forklift")], "task": "C1", "label": "yielding",
         "source": "rule", "rule_id": "wh_dodge", "annotator": None, "note": None},
        {"scene_key": WH_KEY, "t_ms": 4500, "actor_refs": [P("F1", "forklift")], "task": "C2", "label": "continue",
         "source": "rule", "rule_id": "c2_continue", "annotator": None, "note": None},
        {"scene_key": WH_KEY, "t_ms": 4600, "actor_refs": [P("P1", "person")], "task": "C4", "label": "4.6",
         "source": "rule", "rule_id": "c4_dodge_start", "annotator": None, "note": "first Dodge frame (s)"},
    ]}


ANCHOR_Q = "Show me every clip, from any camera in any site, where a person is close to a moving vehicle"
CORPUS_ASKS = [
    ("corpus-anchor", "warehouse", ANCHOR_Q,
     {"workspace": "warehouse", "scene_key": WH_KEY, "scope": "corpus", "sources": ["warehouse", "fleet", "fixed"]}),
    ("corpus-street-distance", "street", "How far was the pedestrian from the car on the street?",
     {"workspace": "street", "scene_key": ST_KEY, "scope": "corpus", "sources": ["fixed"]}),
    ("corpus-nomatch", "street", "Find a bicycle on the street camera", {"workspace": "street", "scope": "corpus", "sources": ["fixed"]}),
]


def corpus_asks(out: Path) -> list[dict]:
    """The corpus asks run through the real agent on the generated scenes (sva-out plus the l1 street scene): recorded Cosmos
    (a scene without a recorded file has none), the unit tests' in-memory trace, fixed run ids and timings."""
    import tempfile

    from app.api import agent
    from app.core import embed, vlm
    from app.tests.unit.a5_fakes import FixtureStore, install_trace

    def recorded(task):
        def call(scene_key, *args, run=None, **kwargs):
            path = out / "sva-out" / scene_key / "cosmos" / f"{scene_key}.json"
            records = json.loads(path.read_text())["records"] if path.is_file() else []
            record = next((r for r in records if r["task"] == task), None)
            if record is None:
                raise vlm.VLMUnavailable("no recorded fixture")
            return CosmosRecord.model_validate(record)
        return call
    install_trace(SimpleNamespace(setattr=setattr))
    for name, task in (("situation", "C1"), ("predict_next", "C2"), ("ground_objects", "C3"), ("localize", "C4"), ("describe_region", "C5")):
        setattr(vlm, name, recorded(task))
    embed.embed_text = lambda q: [0.0] * 256
    asks = []
    with tempfile.TemporaryDirectory() as tmp:
        # Record data-only availability consistently, even when --out already
        # contains rendered media from a previous invocation. No frames are
        # invented: the private agent store genuinely has no media files.
        no_media = shutil.ignore_patterns('*.jpg','*.jpeg','*.png','*.mp4','*.mov','*.webm')
        assets = shutil.copytree(out / "sva-out", Path(tmp) / "sva-out",ignore=no_media)
        shutil.copytree(out / "l1" / ST_KEY, assets / ST_KEY,ignore=no_media)
        build_index(Path(tmp) / "index.duckdb", out, l1=True)
        store = FixtureStore(index_path=Path(tmp) / "index.duckdb", app_path=Path(tmp) / "app.duckdb", asset_dir=assets)
        for n, (ask_id, workspace, q, context) in enumerate(CORPUS_ASKS, 1):
            budget = {"tools": 8, "seconds": 25}
            frames = list(agent.ask_frames(store, agent.AskRequest(q=q, context=context, budget=budget)))
            frames = json.loads(json.dumps(frames).replace(frames[0]["data"]["run_id"], f"r_fixtureco{n:04d}"))
            for f in frames:
                if f["event"] == "step":
                    f["data"]["elapsed_ms"] = 5 * f["data"]["i"]
                elif f["event"] == "tool_result":
                    f["data"]["envelope"]["elapsed_ms"] = 12
            hits = next((f["data"]["moments"] for f in frames if f["event"] == "hits"), [])
            asks.append({"ask_id": ask_id, "workspace": workspace, "q": q, "context": context, "budget": budget,
                         "expect": {"final": frames[-2]["data"]["final"], "template": "find", "n_moments": len(hits)},
                         "events": frames})
        store.index.close()
    return asks


# ---------------------------------------------------------------- media (Pillow + ffmpeg)

def render_media(root: Path, workspace: str, facts: dict) -> None:
    from PIL import Image, ImageDraw
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg not found; rerun with --no-media")
    cams = {"warehouse": WH_CAMS, "fleet": [DASH], "street": [ST_CAM]}[workspace]
    draw = {"warehouse": draw_wh, "fleet": draw_fl, "street": draw_st}[workspace]
    for cam in cams:
        vid = root / f"video/{cam.id}.mp4"
        vid.parent.mkdir(parents=True, exist_ok=True)
        tdir = root / f"thumbs/{cam.id}"
        tdir.mkdir(parents=True, exist_ok=True)
        n = int((T1 - cam.offset_ms) * FPS / 1000)
        proc = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}",
                                 "-r", str(FPS), "-i", "-", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-g", str(FPS),
                                 "-crf", "30", "-preset", "veryfast", "-movflags", "+faststart", str(vid)], stdin=subprocess.PIPE)
        for k in range(n):
            media_ms = round(k * 1000 / FPS)
            t = cam.offset_ms + media_ms
            img = draw(cam, t, Image, ImageDraw)
            proc.stdin.write(img.tobytes())
            if t // 1000 != (t - round(1000 / FPS)) // 1000 or k == 0:
                s = t // 1000
                if workspace != "warehouse" or has_video(cam.id, t):
                    img.resize((320, 180)).save(tdir / f"{s:05d}.jpg", quality=70)
        proc.stdin.close()
        if proc.wait() != 0:
            raise SystemExit(f"ffmpeg failed for {vid}")


def _poly(d, cam, P, fill=None, outline=None, width=1):
    uv, front = cam.project(P)
    if front.all():
        d.line([tuple(p) for p in uv], fill=outline, width=width) if outline else d.polygon([tuple(p) for p in uv], fill=fill)


def draw_wh(cam: Cam, t, Image, ImageDraw):
    img = Image.new("RGB", (W, H), (24, 26, 30))
    d = ImageDraw.Draw(img)
    if not has_video(cam.id, t):
        d.rectangle([0, 0, W, H], fill=(0, 0, 0))
        d.text((W // 2 - 30, H // 2), "NO VIDEO", fill=(200, 200, 200))
        return img
    for x in np.arange(-8, 8.01, 1.0):
        P = np.c_[np.full(41, x), np.linspace(-5, 5, 41), np.zeros(41)]
        uv, front = cam.project(P)
        pts = [tuple(p) for p, f in zip(uv, front) if f]
        if len(pts) > 1:
            d.line(pts, fill=(60, 64, 72))
    for y in np.arange(-5, 5.01, 1.0):
        P = np.c_[np.linspace(-8, 8, 65), np.full(65, y), np.zeros(65)]
        uv, front = cam.project(P)
        pts = [tuple(p) for p, f in zip(uv, front) if f]
        if len(pts) > 1:
            d.line(pts, fill=(60, 64, 72))
    rc = box_corners(RACK["cx"], RACK["cy"], RACK["l"], RACK["w"], RACK["h"])
    for a, b in ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)):
        _poly(d, cam, rc[[a, b]], outline=(168, 112, 48), width=2)
    x, y, hd, _ = f1(t)
    fc = box_corners(x, y, *FK_DIMS, hd)
    for a, b in ((0, 1), (2, 3), (4, 5), (6, 7), (0, 2), (1, 3), (4, 6), (5, 7), (0, 4), (1, 5), (2, 6), (3, 7)):
        _poly(d, cam, fc[[a, b]], outline=(240, 200, 40), width=2)
    for pid, fn, col in (("P1", p1, (80, 180, 255)), ("P2", p2, (120, 230, 140))):
        px, py = fn(t)
        if person_seen(cam, pid, (px, py), t):
            bb = bbox_of(cam, box_corners(px, py, 0.5, 0.5, 1.75))
            if bb:
                d.rectangle(bb, outline=col, width=2)
                d.text((bb[0], max(bb[1] - 12, 0)), pid, fill=col)
    d.text((8, H - 16), f"{cam.id}  t={t / 1000:.2f}s", fill=(230, 230, 230))
    return img


def draw_fl(cam: Cam, t, Image, ImageDraw):
    img = Image.new("RGB", (W, H), (40, 44, 52))
    d = ImageDraw.Draw(img)
    road = np.array([[x, y, 0.0] for x in np.linspace(2, 80, 40) for y in (-5.0, 5.0)])
    uv, front = cam.project(road)
    left = [tuple(p) for p, f in zip(uv[0::2], front[0::2]) if f]
    right = [tuple(p) for p, f in zip(uv[1::2], front[1::2]) if f]
    if len(left) > 1 and len(right) > 1:
        d.polygon(left + right[::-1], fill=(58, 60, 64))
    s = s_at(t)
    for bid, b in BUILDINGS.items():
        a, c = b["s"][0] - s, b["s"][1] - s
        if c < 2:
            continue
        lat_ = b["side"] * b["d"][0]
        quad = np.array([[max(a, 2), lat_, 0], [c, lat_, 0], [c, lat_, b["h"]], [max(a, 2), lat_, b["h"]]])
        _poly(d, cam, quad, fill=(90, 80, 70) if bid == "w1001" else (70, 80, 95))
    px, py = ped_ego(t)
    if 3000 <= t < 8000 and px > 1:
        bb = bbox_of(cam, box_corners(px, py, 0.5, 0.5, 1.75))
        if bb:
            d.rectangle(bb, outline=(80, 180, 255), width=2)
    vx, vy = veh_ego(t)
    if 2 < vx < 60:
        bb = bbox_of(cam, box_corners(vx, vy, 4.5, 1.8, 1.5, math.pi))
        if bb:
            d.rectangle(bb, outline=(240, 120, 80), width=2)
    d.text((8, H - 16), f"dashcam  t={t / 1000:.2f}s  v={speed(t):.1f} m/s", fill=(230, 230, 230))
    return img


def draw_st(cam, t, Image, ImageDraw):
    img = Image.new("RGB", (W, H), (96, 120, 150))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 120, W, 180], fill=(110, 100, 92))
    d.rectangle([0, 180, W, 280], fill=(58, 60, 64))
    d.rectangle([0, 280, W, H], fill=(150, 148, 140))
    for x in range(0, W, 80):
        d.line([(x, 230), (x + 40, 230)], fill=(230, 220, 120), width=3)
    car = st_car(t)
    if car:
        d.rectangle(car, fill=(200, 60, 50))
    d.rectangle(st_person(t), fill=(40, 70, 160))
    d.text((8, H - 16), f"{cam.id}  t={t / 1000:.2f}s", fill=(20, 20, 20))
    return img


# ---------------------------------------------------------------- index.duckdb

def pseudo_vec(text: str) -> list[float]:
    rng = np.random.default_rng(int(hashlib.sha256(text.encode()).hexdigest()[:8], 16))
    v = rng.normal(size=256)
    return (v / np.linalg.norm(v)).astype(np.float32).tolist()


VEHICLES = {"bicycle", "car", "motorcycle", "bus", "truck"}


def street_clips(m: dict, sdir: Path, cam: dict) -> list[list]:
    """clips rows of a 2D camera: counts are the most boxes of a kind in one frame (no track ids); video_uri = chunk uri."""
    rows = [r for i in range(len(cam["chunks"]))
            for r in json.loads((sdir / m["assets"]["dets_dir"] / cam["id"] / f"{i:04d}.json").read_text())["rows"]]
    out = []
    for c0 in range(0, m["t1_ms"], 5000):
        pts = [r for r in rows if c0 <= r[0] < c0 + 5000]
        labels_ = sorted({r[1] for r in pts})
        cap = f"{m['title']} {cam['id']} {c0 // 1000}-{c0 // 1000 + 5}s: " + (" and ".join(labels_) + " in view" if labels_ else "empty street")
        per_frame = lambda keep: max([sum(1 for r in pts if r[0] == t and keep(r[1])) for t in {r[0] for r in pts}], default=0)  # noqa: E731
        chunk = next(ch for ch in cam["chunks"] if ch["t0_ms"] <= c0 < ch["t1_ms"])
        out.append([f"{m['scene_key']}:{cam['id']}:{c0 // 5000:05d}", m["scene_id"], m["scene_key"], m["workspace"], cam["id"], c0,
                    c0 + 5000, None, None, None, None, cap, pseudo_vec("txt:" + cap), pseudo_vec("vis:" + cap),
                    per_frame(lambda lab: lab == "person"), per_frame(lambda lab: lab in VEHICLES),
                    None, None, None, None, None, [], chunk["uri"]])
    return out


def build_index(path: Path, out: Path, l1: bool = False) -> None:
    """index.duckdb of the sva-out scenes; l1=True adds the scenes under <out>/l1 (street) for backend tests."""
    import duckdb
    if path.exists():
        path.unlink()
    con = duckdb.connect(str(path))
    con.execute((SVA / "schemas/index.sql").read_text())
    scenes = sorted(p.parent for p in (out / "sva-out").rglob("manifest.json"))
    if l1:
        scenes += sorted(p.parent for p in (out / "l1").rglob("manifest.json"))
    for sdir in scenes:
        m = json.loads((sdir / "manifest.json").read_text())
        key = m["scene_key"]
        con.execute("INSERT INTO scene_manifest VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [m["scene_id"], key, m["workspace"], m["title"], m["t0_ms"], m["t1_ms"], json.dumps(m)])
        evs = json.loads((sdir / m["assets"]["events"]).read_text())["events"] if "events" in m["assets"] else []
        for e in evs:
            con.execute("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [f"{key}:{e['id']}", m["scene_id"], key, e["type"], e["t0_ms"], e["t1_ms"], e["actor_ids"], e.get("place_id"),
                         e["severity"], e["source"], json.dumps(e["params"]), e["clip_ids"]])
        trip = json.loads((sdir / m["assets"]["trip"]).read_text()) if "trip" in m["assets"] else None
        if trip:
            for f in trip["features"][1:]:
                p = f["properties"]
                con.execute("INSERT INTO gps VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [key, p["t_ms"], p["lat"], p["lon"], p["heading"], p["speed"], p["way_id"], p["node_id"], p["place_id"],
                             p["state"], p["gap_ms"]])
        if "tracks" in m["assets"]:
            for tr in json.loads((sdir / m["assets"]["tracks"]).read_text())["tracks"]:
                con.execute("INSERT INTO tracks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [f"{key}:{tr['entity_id']}", m["scene_id"], key, tr["entity_id"], tr["cls"], tr["t0_ms"], tr["t1_ms"],
                             tr["cam_ids"], tr["source"]])
        ents = json.loads((sdir / m["assets"]["entities"]).read_text())["entities"] if "entities" in m["assets"] else []
        owner = {(o["cam_id"], o["local_track_id"]): e["entity_id"] for e in ents for o in e["observations"]}
        for cam in m["cams"]:
            if "chunks" in cam:
                con.executemany("INSERT INTO clips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                                street_clips(m, sdir, cam))
                continue
            bfile = sdir / m["assets"]["boxes_dir"] / cam["id"] / "00000.json"
            rows = json.loads(bfile.read_text())["rows"] if bfile.exists() else []
            con.executemany("INSERT INTO detections VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL)",
                            [[m["scene_id"], key, cam["id"], r[0], r[2], r[7], r[3], r[4], r[5], r[6], r[1], owner.get((cam["id"], r[1]))]
                             for r in rows])
            for c0 in range(0, m["t1_ms"], 5000):
                in_clip = [e for e in evs if e["t0_ms"] < c0 + 5000 and e["t1_ms"] > c0]
                cap = f"{m['title']} {cam['id']} {c0 // 1000}-{c0 // 1000 + 5}s: " + (
                    "; ".join(f"{e['type']} {' '.join(e['actor_ids'])}" for e in in_clip) or "routine activity")
                pts = [r for r in rows if c0 <= r[0] < c0 + 5000]
                gp = [f["properties"] for f in trip["features"][1:] if c0 <= f["properties"]["t_ms"] < c0 + 5000] if trip else []
                con.execute("INSERT INTO clips VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            [f"{key}:{cam['id']}:{c0 // 5000:05d}", m["scene_id"], key, m["workspace"], cam["id"], c0, c0 + 5000,
                             None, None, None, None, cap, pseudo_vec("txt:" + cap), pseudo_vec("vis:" + cap),
                             len({r[1] for r in pts if r[2] == "person"}), len({r[1] for r in pts if r[2] != "person"}),
                             gp[0]["lat"] if gp else None, gp[0]["lon"] if gp else None, gp[0]["heading"] if gp else None,
                             gp[0]["speed"] if gp else None, gp[0]["place_id"] if gp else None, [], cam["video"]])
    for pid, kind, name, aliases, la, lo in fixture_places():
        con.execute("INSERT INTO places VALUES (?, ?, ?, ?, ?, ?, ?)", [pid, kind, name, aliases, la, lo, None])
    con.close()


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(SVA / "fixtures"))
    ap.add_argument("--no-media", action="store_true")
    ap.add_argument("--index", default=None)
    a = ap.parse_args()
    out = Path(a.out)
    wh_root, fl_root, fl2_root = (out / "sva-out" / k for k in (WH_KEY, FL_KEY, FL2_KEY))
    wh = build_warehouse(wh_root)
    fl = build_fleet(fl_root)
    build_fleet_gps_only(fl2_root)
    st_root = out / "l1" / ST_KEY
    st = build_street(st_root)
    main_ask, run_doc = wh_main(wh)
    write_json(out / "asks.json", {"schema_version": "1", "asks": [main_ask, wh_refuse(), wh_nomatch(), fl_where(fl)]})
    write_json(out / "l1" / "asks_corpus.json", {
        "schema_version": "1",
        "note": "SSE sequences recorded from the real agent on the fixture scenes; run ids and timings are fixed.",
        "asks": corpus_asks(out)})
    write_json(out / "runs" / f"{run_doc['run_id']}.json", run_doc)
    write_json(out / "labels" / f"{WH_KEY}.json", labels(wh))
    if not a.no_media:
        render_media(wh_root, "warehouse", wh)
        render_media(fl_root, "fleet", fl)
        render_media(st_root, "street", st)
        (st_root / ST_CHUNK).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(st_root / f"video/{ST_CAM.id}.mp4", st_root / ST_CHUNK)
    if a.index:
        build_index(Path(a.index), out)
    print(f"fixture written to {out} (near_miss min {wh['min_d']:.2f} m at {wh['min_t']} ms; P1 seen at 4600 by {wh['seen'][('P1', 4600)]})")


if __name__ == "__main__":
    main()
