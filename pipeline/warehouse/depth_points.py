"""SDG run -> static point cloud (points.bin), floor grid, display videos and thumbnails, manifest.part.json for P-W2.

  python pipeline/warehouse/depth_points.py RUN_DIR_NAME

Depth: every 10th frame of the 10 depth.mp4, agent boxes and depth edges masked, per-pixel temporal median. The log depth
map gray = a - b*ln(d) is refit per run on floor pixels (ray hits z=0); a camera whose median floor error exceeds
MAX_RESIDUAL_M stays out of the cloud. Output goes to sva-out/_tmp/P-W1/<scene_key>/.
"""
from __future__ import annotations

import hashlib
import json
import os
import struct
import sys
import warnings
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, publish, video  # noqa: E402
from pipeline.warehouse import sdg  # noqa: E402

CARD = "P-W1"
ANALYSIS_REV = "pw1-1"
CPU = 4
STEP = 2  # pixel stride of the cloud (2 px at 1662 px focal = 1.2 cm at 10 m, finer than the voxel)
FRAME_EVERY = 10
BOX_MARGIN_PX = 12
MAX_RANGE_M = 30.0  # one gray level is 2.8 % of range: 0.8 m at 30 m
FIT_RANGE_M = 25.0
FIT_BAND_GRAY = 2.0  # floor inliers: within this many gray levels of the current fit
MAX_RESIDUAL_M = 0.25
VOXEL_M = 0.05
MAX_POINTS = 500_000
FLOOR_Z_M = 0.1
CELL_M = 0.5


def producer_commit() -> str:
    return os.environ.get("SVA_COMMIT", "uncommitted")


def sha256(p: Path) -> str:
    return "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()


def median_depth(run_dir: Path, cam_id: str) -> tuple[np.ndarray, np.ndarray]:
    """Temporal median gray (NaN where every sample was masked) and RGB, both at STEP stride."""
    n = video.probe(run_dir / f"{cam_id}.depth.mp4")["n_frames"]
    idx = list(range(0, n, FRAME_EVERY))
    gray = sdg.frames(run_dir / f"{cam_id}.depth.mp4", idx)
    rgb = sdg.frames(run_dir / f"{cam_id}.rgb.mp4", idx, "rgb24")
    boxes = sdg.agents(run_dir, cam_id)
    g = np.empty((len(idx), *gray[0, ::STEP, ::STEP].shape), np.float32)
    c = np.empty((len(idx), *rgb[0, ::STEP, ::STEP].shape), np.float32)
    for k, i in enumerate(idx):
        bad = sdg.jumps(gray[k])
        for a in boxes[i]:
            if a["box"]:
                x1, y1, x2, y2 = a["box"]
                bad[max(y1 - BOX_MARGIN_PX, 0):y2 + BOX_MARGIN_PX, max(x1 - BOX_MARGIN_PX, 0):x2 + BOX_MARGIN_PX] = True
        bad = bad[::STEP, ::STEP]
        g[k] = np.where(bad, np.nan, gray[k, ::STEP, ::STEP])
        c[k] = np.where(bad[..., None], np.nan, rgb[k, ::STEP, ::STEP])
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # pixels masked in every sample stay NaN
        return np.nanmedian(g, 0), np.nanmedian(c, 0)


def floor_range(cam: dict) -> np.ndarray:
    """Range from the camera centre to z=0 along each STEP ray (NaN where the ray misses the floor)."""
    r = sdg.pixel_rays(cam, STEP)
    t = -sdg.center(cam)[2] / r[..., 2]
    return np.where(r[..., 2] < 0, t, np.nan)


def fit_depth(gray: dict[str, np.ndarray], floor: dict[str, np.ndarray]) -> tuple[float, float, dict[str, float]]:
    """Run-wide a, b of gray = a - b*ln(d) on floor inliers of all cameras; per-camera median |d_obs - d_floor| in m
    (None: the camera has no floor inlier)."""
    a, b = sdg.DEPTH_A, sdg.DEPTH_B
    for _ in range(3):
        xs, ys = [], []
        for cam, g in gray.items():
            d = floor[cam]
            ok = (d < FIT_RANGE_M) & (np.abs(g - (a - b * np.log(d))) < FIT_BAND_GRAY)
            xs.append(np.log(d[ok]))
            ys.append(g[ok])
        slope, a = np.polyfit(np.concatenate(xs), np.concatenate(ys), 1)
        b = -slope
    res = {}
    for cam, g in gray.items():
        d = floor[cam]
        ok = (d < FIT_RANGE_M) & (np.abs(g - (a - b * np.log(d))) < FIT_BAND_GRAY)
        res[cam] = round(float(np.median(np.abs(sdg.depth_m(g[ok], a, b) - d[ok]))), 4) if ok.any() else None
    return float(a), float(b), res


def in_cloud(r: float | None) -> bool:
    return r is not None and r <= MAX_RESIDUAL_M


def voxelize(xyz: np.ndarray, rgb: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    key = np.floor(xyz / VOXEL_M).astype(np.int64)
    _, inv, cnt = np.unique(key, axis=0, return_inverse=True, return_counts=True)
    inv = inv.ravel()
    mean = lambda v: np.stack([np.bincount(inv, v[:, i]) for i in range(3)], 1) / cnt[:, None]  # noqa: E731
    pts, col = mean(xyz), mean(rgb)
    if len(pts) > MAX_POINTS:
        keep = np.sort(np.random.default_rng(0).choice(len(pts), MAX_POINTS, replace=False))
        pts, col = pts[keep], col[keep]
    return pts.astype(np.float32), np.clip(np.round(col), 0, 255).astype(np.uint8)


def write_points(path: Path, xyz: np.ndarray, rgb: np.ndarray) -> list[float]:
    """CONTRACT §4.2: "SVAP", u32 N, f32 bbox[6], then f32 xyz*N, u8 rgb*N (little endian)."""
    bbox = [*xyz.min(0).tolist(), *xyz.max(0).tolist()]
    with open(path, "wb") as f:
        f.write(struct.pack("<4sI6f", b"SVAP", len(xyz), *bbox))
        f.write(xyz.astype("<f4").tobytes())
        f.write(rgb.tobytes())
    return bbox


def ground_grid(xyz: np.ndarray) -> dict:
    """Grid over the 1st-99th percentile xy extent of the floor points."""
    fl = xyz[np.abs(xyz[:, 2]) < FLOOR_Z_M, :2]
    lo = np.floor(np.percentile(fl, 1, axis=0) / CELL_M) * CELL_M
    hi = np.ceil(np.percentile(fl, 99, axis=0) / CELL_M) * CELL_M
    nx, ny = (int(round(v)) for v in (hi - lo) / CELL_M)
    return {"origin": [float(lo[0]), float(lo[1])], "cell_m": CELL_M, "nx": nx, "ny": ny, "cam_bit_order": sdg.DISPLAY_CAMS,
            "texture": None, "texture_from_world": None}


def media(run_dir: Path, out: Path, cam_id: str) -> tuple[int, int]:
    dst = video.transcode(run_dir / f"{cam_id}.rgb.mp4", out / "video" / f"{cam_id}.mp4", max_w=960)
    video.thumbs(dst, out / "thumbs" / cam_id)
    info = video.probe(dst)
    return info["w"], info["h"]


def main() -> None:
    run = sys.argv[1]
    run_dir = sdg.SDG_ROOT / run
    sid = sdg.scene_id(run)
    out = publish.tmp_dir(CARD, sid.replace("/", "-").lower())
    with lease.acquire(cpu=CPU, owner=CARD):
        cv2.setNumThreads(CPU)
        cams = {c: sdg.cam_entry(sdg.camera_params(run_dir, c), c, video=None, thumbs=None, video_w=0, video_h=0)
                for c in sdg.ALL_CAMS}
        med = {c: median_depth(run_dir, c) for c in sdg.ALL_CAMS}
        floor = {c: floor_range(cams[c]) for c in sdg.ALL_CAMS}
        a, b, res = fit_depth({c: med[c][0] for c in sdg.ALL_CAMS}, floor)
        xyz, rgb = [], []
        for c in sdg.ALL_CAMS:
            if not in_cloud(res[c]):
                continue
            d = sdg.depth_m(med[c][0], a, b)
            ok = np.isfinite(d) & (d < MAX_RANGE_M)
            xyz.append(sdg.center(cams[c]) + sdg.pixel_rays(cams[c], STEP)[ok] * d[ok, None])
            rgb.append(med[c][1][ok])
        pts, col = voxelize(np.concatenate(xyz), np.concatenate(rgb))
        bbox = write_points(out / "points.bin", pts, col)
        ground = ground_grid(pts)
        disp = []
        for c in sdg.DISPLAY_CAMS:
            vw, vh = media(run_dir, out, c)
            disp.append(sdg.cam_entry(sdg.camera_params(run_dir, c), c, video=f"video/{c}.mp4", thumbs=f"thumbs/{c}",
                                      video_w=vw, video_h=vh))
    inputs = [run_dir / f"{c}.{k}" for c in sdg.ALL_CAMS for k in ("depth.mp4", "camera_params.jsonl", "object_detection.jsonl")]
    inputs += [run_dir / f"{c}.rgb.mp4" for c in sdg.ALL_CAMS]
    part = {"schema_version": "1", "analysis_rev": ANALYSIS_REV, "producer_commit": producer_commit(),
            "input_artifact_hashes": {f"{run}/{p.name}": sha256(p) for p in inputs},
            "scene_id": sid, "world_frame_id": sid.removeprefix("wh/"), "cams": disp, "ground": ground,
            "points_meta": {"n": len(pts), "voxel_m": VOXEL_M, "depth_kind": "euclidean_range"},
            "sources": {"depth_residual_m": res, "depth_log_fit": {"a": round(a, 4), "b": round(b, 4)},
                        "points_cams": [c for c in sdg.ALL_CAMS if in_cloud(res[c])], "points_bbox": bbox}}
    (out / "manifest.part.json").write_text(json.dumps(part, indent=1))
    print(f"{sid}: N={len(pts)} bbox={[round(v, 2) for v in bbox]} a={a:.2f} b={b:.2f} grid={ground['nx']}x{ground['ny']} "
          f"residual_m={res}", flush=True)


if __name__ == "__main__":
    main()
