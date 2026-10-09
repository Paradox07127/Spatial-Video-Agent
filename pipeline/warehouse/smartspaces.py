"""P8: SmartSpaces Warehouse_017 (3 cameras, 300 s) with the SDG method: pose2d -> triangulate -> events.

  python pipeline/warehouse/smartspaces.py

Writes video/, thumbs/, ground/map.png, skeleton.json, events.json into _tmp/P-W2/wh-ss-warehouse_017/ and
_work/wh-ss-warehouse_017/{pose2d/, persons.json, manifest.base.json} for run_all.py, which assembles and publishes.
No point cloud and no visibility (the depth HDF5 files are not downloaded); metric false (spec §6.2). The dataset documents
no camera synchronisation, so each other camera's offset to `camera` is estimated: offsets in pose-frame steps over
+-SYNC_SCAN_MS, each scored by the median ground distance of the roots of associated (camera, other) track pairs. A clear
minimum = not at the scan edge, >= SYNC_MIN_N frames, and every other offset's median at least SYNC_MARGIN higher; then
the camera gets offset_ms, sync_method "manual", sync_residual_ms = half a step and joins the cross-camera association.
Otherwise it stays "unknown" and its people get single-view roots only. The scene has no forklift (Nova Carter AMRs and
humanoid robots only), so the near_miss rule yields no events.
"""
from __future__ import annotations

import json
import math
import shutil
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, paths, video  # noqa: E402
from pipeline.warehouse import events, pose2d, triangulate  # noqa: E402

SS_DIR = paths.DATA / "raw" / "smartspace" / "MTMC_Tracking_2025" / "test" / "Warehouse_017"
SCENE_ID = "wh/ss/warehouse_017"
KEY = "wh-ss-warehouse_017"
SENSORS = ["Camera", "Camera_01", "Camera_02"]  # calibration.json ids; cam ids are their lower case
T1_MS = 300_000
POSE_FPS = 5  # 15 fps over 300 s would make skeleton.json ~20 MB for the ~10 people in view
VIDEO_W = 960
CELL_M = 0.5
SYNC_SCAN_MS = 1000
SYNC_MIN_N = 30
SYNC_MARGIN = 0.10


def cam_entries(calib: dict) -> dict[str, dict]:
    """extrinsicMatrix is world -> OpenCV camera; the contract wants cam2world in the OpenGL basis, column-major."""
    out = {}
    for s in calib["sensors"]:
        if s["id"] not in SENSORS:
            continue
        cid = s["id"].lower()
        at = {a["name"]: a["value"] for a in s["attributes"]}
        w, h = int(at["frameWidth"]), int(at["frameHeight"])
        Kc = s["intrinsicMatrix"]
        fx, fy, cx, cy = (round(v, 6) for v in (Kc[0][0], Kc[1][1], Kc[0][2], Kc[1][2]))
        E = np.eye(4)
        E[:3] = s["extrinsicMatrix"]
        M = np.linalg.inv(E) @ np.diag([1.0, -1, -1, 1])
        out[cid] = {"id": cid, "w": w, "h": h, "K": [fx, fy, cx, cy], "model": "pinhole", "dist": [], "camera_basis": "opengl",
                    "cam2world": [round(float(v), 9) for v in M.reshape(-1, order="F")],
                    "fov_deg": round(math.degrees(2 * math.atan(h / (2 * fy))), 6), "video": f"video/{cid}.mp4",
                    "frame_ref": {"video_w": VIDEO_W, "video_h": round(h * VIDEO_W / w), "thumb_w": 320, "thumb_h": 180},
                    "offset_ms": 0, "thumbs": f"thumbs/{cid}", "sync_method": "unknown", "sync_residual_ms": None}
    return out


def offset_scan(geos: dict, tracks: dict, ref: str, cam: str, step: int) -> dict[int, tuple[float, int]]:
    """offset_ms -> (median ground distance, n frames) over the (ref, cam) track pairs that associate (>= 3 common frames,
    median < triangulate.ASSOC_M) when cam's scene time = media time + offset_ms."""
    ground = {k: {t: g for t, d in v.items() if (g := triangulate.root_view(geos[k[0]], d)[0]) is not None}
              for k, v in tracks.items() if k[0] in (ref, cam)}
    out = {}
    for off in range(-SYNC_SCAN_MS, SYNC_SCAN_MS + 1, step):
        pooled = []
        for a in (k for k in ground if k[0] == ref):
            for b in (k for k in ground if k[0] == cam):
                d = [float(np.linalg.norm(ground[a][t + off] - p)) for t, p in ground[b].items() if t + off in ground[a]]
                if len(d) >= 3 and np.median(d) < triangulate.ASSOC_M:
                    pooled += d
        out[off] = (float(np.median(pooled)), len(pooled)) if pooled else (math.inf, 0)
    return out


def clear_minimum(scan: dict[int, tuple[float, int]]) -> int | None:
    best = min(scan, key=lambda o: scan[o][0])
    m, n = scan[best]
    if n < SYNC_MIN_N or abs(best) == SYNC_SCAN_MS or any(v[0] < m * (1 + SYNC_MARGIN) for o, v in scan.items() if o != best):
        return None
    return best


def ground(calib: dict, map_w: int, map_h: int, cams: list[str]) -> dict:
    s0 = calib["sensors"][0]
    s, tx, ty = s0["scaleFactor"], s0["translationToGlobalCoordinates"]["x"], s0["translationToGlobalCoordinates"]["y"]
    # map column = (x + tx)·s, map row = map_h - (y + ty)·s. Checked on a right-wall pillar base seen by camera_01:
    # world (9.29, 2.94) -> (1218, 497); the pillar mark in map.png spans columns 1195-1215, rows 491-499.
    return {"origin": [round(-tx, 4), round(-ty, 4)], "cell_m": CELL_M, "nx": math.ceil(map_w / s / CELL_M),
            "ny": math.ceil(map_h / s / CELL_M), "cam_bit_order": cams, "texture": "ground/map.png",
            "texture_from_world": [s, 0.0, s * tx, 0.0, -s, map_h - s * ty, 0.0, 0.0, 1.0]}


def run() -> dict:
    """Produces everything for run_all.py; returns the base manifest (also saved as manifest.base.json)."""
    from PIL import Image
    out = pose2d.OUT_ROOT / "_tmp" / pose2d.OWNER / KEY
    wd = pose2d.work_dir(KEY)
    calib = json.loads((SS_DIR / "calibration.json").read_text())
    cams = cam_entries(calib)
    src = {s.lower(): SS_DIR / "videos" / f"{s}.mp4" for s in SENSORS}
    with lease.acquire(cpu=4, owner=pose2d.OWNER):
        for cid, p in src.items():
            if not (out / cams[cid]["video"]).is_file():
                video.transcode(p, out / cams[cid]["video"], max_w=VIDEO_W)
                video.thumbs(out / cams[cid]["video"], out / cams[cid]["thumbs"])
        (out / "ground").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SS_DIR / "map.png", out / "ground" / "map.png")
    if not all((wd / "pose2d" / f"{c}.json").is_file() for c in cams):
        with lease.acquire(cpu=4, gpu=1, owner=pose2d.OWNER):
            pose2d.run_pose(src, POSE_FPS, wd)
    with lease.acquire(cpu=2, owner=pose2d.OWNER):
        poses = pose2d.load(KEY, list(cams))
        inputs = [wd / "pose2d" / f"{c}.json" for c in cams] + [SS_DIR / "calibration.json"]
        ref = SENSORS[0].lower()
        step = round(1000 / poses[ref]["fps"])
        geos, tracks = {c: triangulate.Geo(cams[c]) for c in cams}, triangulate.person_tracks(poses)
        synced, sync = {ref}, {}
        for cid in cams:
            if cid == ref:
                continue
            scan = offset_scan(geos, tracks, ref, cid, step)
            off = clear_minimum(scan)
            sync[cid] = {"offset_ms": off, "scan": {str(o): [round(m, 3) if math.isfinite(m) else None, n] for o, (m, n) in scan.items()}}
            print(f"sync {cid}: offset_ms={off} scan(median_m, n)={sync[cid]['scan']}", file=sys.stderr)
            if off is not None:
                cams[cid].update(offset_ms=off, sync_method="manual", sync_residual_ms=step / 2)
                poses[cid] = {**poses[cid], "frames": [[t + off, d] for t, d in poses[cid]["frames"] if 0 <= t + off < T1_MS]}
                synced.add(cid)
        if len(synced) > 1:
            cams[ref].update(sync_method="manual", sync_residual_ms=0.0)
        persons = triangulate.write(KEY, poses, cams, T1_MS, inputs, linked=synced)
        ev = events.finish(events.near_misses(persons, []), KEY, list(cams))
        pose2d.write_json(out / "events.json", {**pose2d.header([out / "skeleton.json"]), "events": ev})
    mw, mh = Image.open(SS_DIR / "map.png").size
    base = {**pose2d.header([SS_DIR / "calibration.json", SS_DIR / "map.png", *src.values()]), "scene_id": SCENE_ID,
            "title": "SmartSpaces Warehouse_017", "t1_ms": T1_MS, "fps": 30, "world_frame_id": "smartspaces/warehouse_017",
            "metric": False, "cams": list(cams.values()), "ground": ground(calib, mw, mh, list(cams)),
            "sources": {"dataset": "nvidia/PhysicalAI-SmartSpaces MTMC_Tracking_2025/test/Warehouse_017", "license": "CC-BY-4.0",
                        "pose_fps": POSE_FPS, "forklift": "none in scene",
                        "sync": {"reference": ref, "rule": f"offset scan +-{SYNC_SCAN_MS} ms in {step} ms steps, clear minimum: "
                                 f"interior, >= {SYNC_MIN_N} frames, every other offset >= {SYNC_MARGIN:.0%} higher", "cams": sync}},
            "models": pose2d.MODELS}
    pose2d.write_json(wd / "manifest.base.json", base)
    print(f"smartspaces {KEY}: {len(persons)} persons, {len(ev)} events", file=sys.stderr)
    return base


if __name__ == "__main__":
    run()
