"""SDG warehouse run helpers shared by the warehouse pipeline cards (scene ids, camera calibration).

Omniverse camera_params: cameraViewTransform is world->camera for row vectors (translation in the last row), and the USD
camera already uses the OpenGL basis (+Y up, looks -Z). So the column-major cam2world of the contract is inv(V) read
row-major, and fx, fy come from cameraProjection[0] and [5] (NDC scale) times half the render size.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

import cv2
import numpy as np

from pipeline.common import paths, video

SDG_ROOT = paths.DATA / "raw" / "sdg_warehouse" / "runs"
DISPLAY_CAMS = ["ceiling_00", "ceiling_04", "eye_00"]
ALL_CAMS = [f"ceiling_{i:02d}" for i in range(5)] + [f"eye_{i:02d}" for i in range(5)]
RUN_RE = re.compile(r"^([0-9a-f]{20})_(run_\d+)_seed_\d+$")
FPS = 30
# depth.mp4 gray = A - B*ln(d), d = euclidean range to the camera centre in m (R12 fit on one frame; runs refit a, b)
DEPTH_A, DEPTH_B = 168.05, 36.27
JUMP_GRAY = 3  # 3x3 gray range above this (about 8 % in range) marks a depth edge: H.264 mixes the two sides there


def scene_id(run_dir: str | Path) -> str:
    """`001e53453441935632ae_run_1_seed_1288693302` -> `wh/sdg/001e5345_run_1` (run_<n> alone is not unique)."""
    m = RUN_RE.match(Path(run_dir).name)
    if not m:
        raise ValueError(f"not an SDG run dir: {run_dir}")
    return f"wh/sdg/{m.group(1)[:8]}_{m.group(2)}"


def camera_params(run_dir: Path, cam_id: str, frame: int = 0) -> dict:
    with open(run_dir / f"{cam_id}.camera_params.jsonl") as f:
        for i, line in enumerate(f):
            if i == frame:
                return json.loads(line)
    raise IndexError(f"{run_dir.name}/{cam_id}: no frame {frame}")


def cam_entry(p: dict, cam_id: str, *, video: str | None, thumbs: str | None, video_w: int, video_h: int,
              thumb_w: int = 320, thumb_h: int = 180) -> dict:
    """Manifest cams[] entry (schemas/manifest.json) from one camera_params line."""
    w, h = p["renderProductResolution"]
    P = p["cameraProjection"]
    fx, fy = P[0] * w / 2, P[5] * h / 2
    ox, oy = p["cameraApertureOffset"]
    cx, cy = w / 2 + ox, h / 2 + oy
    V = np.array(p["cameraViewTransform"], float).reshape(4, 4)
    cam2world = np.linalg.inv(V).reshape(-1)
    return {"id": cam_id, "w": w, "h": h, "K": [round(v, 6) for v in (fx, fy, cx, cy)], "model": "pinhole", "dist": [],
            "camera_basis": "opengl", "cam2world": [round(float(v), 9) for v in cam2world],
            "fov_deg": round(math.degrees(2 * math.atan(h / (2 * round(fy, 6)))), 6), "video": video,
            "frame_ref": {"video_w": video_w, "video_h": video_h, "thumb_w": thumb_w, "thumb_h": thumb_h},
            "offset_ms": 0, "thumbs": thumbs, "sync_method": "sdg_frame_index", "sync_residual_ms": 0.0}


def project(cam: dict, xyz) -> np.ndarray:
    """World points -> source pixels with the contract's cam2world + K (pinhole)."""
    M = np.array(cam["cam2world"], float).reshape(4, 4, order="F")
    X = np.atleast_2d(np.asarray(xyz, float))
    pc = (np.linalg.inv(M) @ np.c_[X, np.ones(len(X))].T).T
    fx, fy, cx, cy = cam["K"]
    return np.c_[fx * pc[:, 0] / -pc[:, 2] + cx, cy - fy * pc[:, 1] / -pc[:, 2]]


def in_front(cam: dict, xyz) -> np.ndarray:
    M = np.array(cam["cam2world"], float).reshape(4, 4, order="F")
    X = np.atleast_2d(np.asarray(xyz, float))
    return (np.linalg.inv(M) @ np.c_[X, np.ones(len(X))].T)[2] < 0


def center(cam: dict) -> np.ndarray:
    return np.array(cam["cam2world"], float)[12:15]


def pixel_rays(cam: dict, step: int) -> np.ndarray:
    """(h/step, w/step, 3) world unit rays through the centres of every step-th source pixel."""
    M = np.array(cam["cam2world"], float).reshape(4, 4, order="F")
    fx, fy, cx, cy = cam["K"]
    u, v = np.meshgrid(np.arange(0, cam["w"], step) + 0.5, np.arange(0, cam["h"], step) + 0.5)
    d = np.stack([(u - cx) / fx, (cy - v) / fy, -np.ones_like(u)], -1) @ M[:3, :3].T
    return d / np.linalg.norm(d, axis=-1, keepdims=True)


def frames(path: Path, idx: list[int], pix_fmt: str = "gray") -> np.ndarray:
    """Decoded frames idx (ascending) as uint8 (n, h, w) for gray or (n, h, w, 3) for rgb24; frames past the end are absent."""
    info = video.probe(path)
    sel = "+".join(f"eq(n\\,{i})" for i in idx)
    raw = subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-threads", "4", "-i", str(path), "-vf", f"select={sel}",
                          "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", pix_fmt, "pipe:1"],
                         capture_output=True, check=True).stdout
    shape = (info["h"], info["w"]) if pix_fmt == "gray" else (info["h"], info["w"], 3)
    return np.frombuffer(raw, np.uint8).reshape(-1, *shape)


def depth_m(gray, a: float, b: float) -> np.ndarray:
    return np.exp((a - np.asarray(gray, np.float32)) / b)


def jumps(gray: np.ndarray) -> np.ndarray:
    k = np.ones((3, 3), np.uint8)
    return (cv2.dilate(gray, k).astype(np.int16) - cv2.erode(gray, k)) > JUMP_GRAY


def agents(run_dir: Path, cam_id: str) -> list[dict]:
    """Per frame: [{cls, box, world}] from object_detection; cls character|robot, box (x1, y1, x2, y2) or None when the
    camera sees no pixel of the agent, world = world_position (feet / chassis origin)."""
    out = []
    with open(run_dir / f"{cam_id}.object_detection.jsonl") as f:
        for line in f:
            fr = []
            for a in json.loads(line)["agents"].values():
                b = a["annotators"].get("bounding_box_2d_tight_fast")
                fr.append({"cls": a["label"]["class"], "box": b and (b["x_min"], b["y_min"], b["x_max"], b["y_max"]),
                           "world": a["metro_agent_data"]["world_position"]})
            out.append(fr)
    return out
