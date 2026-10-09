"""P4: per-camera person / vehicle boxes (YOLO11m + ByteTrack) and COCO-17 keypoints (RTMPose-m) for one SDG run.

  python pipeline/warehouse/pose2d.py RUN [--cams ceiling_00,ceiling_04,eye_00] [--fps 15]

Writes _tmp/P-W2/_work/<scene_key>/pose2d/<cam>.json = {cam_id, fps, w, h, frames: [[t_ms, dets]]}, each det
[local_track_id, cls, x1, y1, x2, y2, conf, kpts]; kpts = 17 x [x, y, score] in source pixels for persons, null otherwise.
Car / bus / truck boxes ride along for forklift.py (COCO has no forklift class).
Also holds the product header helpers shared by the P-W2 scripts.
"""
from __future__ import annotations

import argparse
import functools
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, paths  # noqa: E402
from pipeline.warehouse import sdg  # noqa: E402

OWNER = "P-W2"
ANALYSIS_REV = "pw2-1"
DATA_ROOT = paths.ROOT
OUT_ROOT = DATA_ROOT / "sva-out"
WORK = OUT_ROOT / "_tmp" / OWNER / "_work"
YOLO_WEIGHTS = Path.home() / "sva" / "weights" / "yolo11m.pt"
RTMPOSE_M = ("https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/"
             "rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip")
COCO_CLS = {0: "person", 2: "car", 5: "bus", 7: "truck"}
MODELS = {"detector": "yolo11m+bytetrack", "pose": "rtmpose-m_simcc-body7_256x192"}


def run_dir(run: str) -> Path:
    p = Path(run)
    return p if p.is_dir() else sdg.SDG_ROOT / run


@functools.cache
def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return "sha256:" + h.hexdigest()


@functools.cache
def producer() -> str:
    """The ws checkout is an rsync copy without .git, so the producer is identified by the hash of the warehouse sources."""
    h = hashlib.sha256(b"".join(p.read_bytes() for p in sorted(Path(__file__).parent.glob("*.py"))))
    return "src-sha256:" + h.hexdigest()[:12]


def header(inputs: list[Path]) -> dict:
    """Common product header; input paths are keyed relative to paths.ROOT."""
    return {"schema_version": "1", "analysis_rev": ANALYSIS_REV, "producer_commit": producer(),
            "input_artifact_hashes": {str(p.relative_to(DATA_ROOT)): _sha256(str(p)) for p in inputs}}


def write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False))
    tmp.replace(path)


def scene_key(rd: Path) -> str:
    return sdg.scene_id(rd).replace("/", "-").lower()


def work_dir(scene_key: str) -> Path:
    d = WORK / scene_key
    d.mkdir(parents=True, exist_ok=True)
    return d


def detect_video(video: Path, fps: float, yolo, pose, *, offset_ms: int = 0) -> dict:
    """Tracks every round(src_fps/fps)-th frame; t_ms = offset_ms + media time of the frame."""
    import cv2
    cap = cv2.VideoCapture(str(video))
    src_fps = cap.get(cv2.CAP_PROP_FPS)
    step = max(1, round(src_fps / fps))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frames, i = [], 0
    while True:
        ok = cap.grab()
        if not ok:
            break
        if i % step == 0:
            _, img = cap.retrieve()
            r = yolo.track(img, persist=True, tracker="bytetrack.yaml", classes=list(COCO_CLS), verbose=False, device=0)[0]
            dets = []
            if r.boxes.id is not None:
                for tid, c, b, cf in zip(r.boxes.id.int().tolist(), r.boxes.cls.int().tolist(), r.boxes.xyxy.tolist(),
                                         r.boxes.conf.tolist()):
                    dets.append([tid, COCO_CLS[c], *(round(v, 1) for v in b), round(cf, 3), None])
            people = [d for d in dets if d[1] == "person"]
            if people:
                kp, sc = pose(img, bboxes=[d[2:6] for d in people])
                for d, k, s in zip(people, kp, sc):
                    d[7] = [[round(float(x), 1), round(float(y), 1), round(float(v), 3)] for (x, y), v in zip(k, s)]
            frames.append([offset_ms + round(i * 1000 / src_fps), dets])
        i += 1
    cap.release()
    return {"fps": src_fps / step, "w": w, "h": h, "frames": frames}


def run_pose(videos: dict[str, Path], fps: float, out_dir: Path, offsets: dict[str, int] | None = None) -> None:
    """One pose2d/<cam>.json per video; a fresh tracker per camera so local track ids are per camera."""
    import onnxruntime
    onnxruntime.preload_dlls()
    from rtmlib import RTMPose
    from ultralytics import YOLO
    pose = RTMPose(RTMPOSE_M, model_input_size=(192, 256), backend="onnxruntime", device="cuda")
    for cam, video in videos.items():
        res = detect_video(video, fps, YOLO(str(YOLO_WEIGHTS)), pose, offset_ms=(offsets or {}).get(cam, 0))
        write_json(out_dir / "pose2d" / f"{cam}.json", {"cam_id": cam, "video": str(video), "models": MODELS, **res})
        print(f"pose2d {out_dir.name}/{cam}: {len(res['frames'])} frames", file=sys.stderr)


def load(scene_key: str, cams: list[str]) -> dict[str, dict]:
    return {c: json.loads((WORK / scene_key / "pose2d" / f"{c}.json").read_text()) for c in cams}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("run")
    ap.add_argument("--cams", default=",".join(sdg.DISPLAY_CAMS))
    ap.add_argument("--fps", type=float, default=15)
    a = ap.parse_args()
    rd = run_dir(a.run)
    with lease.acquire(cpu=4, gpu=1, owner=OWNER):
        run_pose({c: rd / f"{c}.rgb.mp4" for c in a.cams.split(",")}, a.fps, work_dir(scene_key(rd)))


if __name__ == "__main__":
    main()
