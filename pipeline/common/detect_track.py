"""YOLO11m + ByteTrack on one camera video -> SCENE_DIR/boxes/<cam>/<chunk>.json (source pixels, local track ids).

  python pipeline/common/detect_track.py VIDEO SCENE_DIR --cam dashcam --cls person,car,truck,bus,bicycle [--fps 10] [--offset-ms 0]

Frames are taken every round(src_fps / fps) frames from frame 0; t_ms = offset_ms + frame * 1000 / src_fps. Tracks seen in
fewer than MIN_BOXES frames are dropped; every box of a track gets the track's majority class. The caller maps the
remaining local tracks to entities.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections import Counter
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402

WEIGHTS = Path.home() / "sva" / "weights" / "yolo11m.pt"
COCO = {"person": 0, "bicycle": 1, "car": 2, "motorcycle": 3, "bus": 5, "truck": 7}
IMGSZ = 1280  # pedestrians 40-150 px tall in 1080p; 640 input would shrink them to 13-50 px
MIN_BOXES = 10  # shorter tracks are mostly detector flicker
CHUNK_MS = 10000


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for block in iter(lambda: f.read(1 << 22), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def run(video: Path, scene_dir: Path, cam: str, classes: list[str], *, fps: float = 10, offset_ms: int = 0,
        analysis_rev: str = "detect-1", input_name: str | None = None, owner: str = "P-F") -> list[list]:
    """Writes the chunks and returns the rows [t_ms, local_track_id, cls, x1, y1, x2, y2, conf]."""
    with lease.acquire(cpu=4, gpu=1, owner=owner):
        from ultralytics import YOLO
        model = YOLO(str(WEIGHTS))
        names = {v: k for k, v in COCO.items()}
        cap = cv2.VideoCapture(str(video))
        src_fps = cap.get(cv2.CAP_PROP_FPS)
        stride = max(1, round(src_fps / fps))
        rows, f = [], 0
        while cap.grab():
            if f % stride == 0:
                _, img = cap.retrieve()
                r = model.track(img, persist=True, tracker="bytetrack.yaml", classes=[COCO[c] for c in classes],
                                imgsz=IMGSZ, verbose=False)[0]
                if r.boxes.id is not None:
                    t = offset_ms + round(f * 1000 / src_fps)
                    for (x1, y1, x2, y2), tid, c, s in zip(r.boxes.xyxy.tolist(), r.boxes.id.int().tolist(),
                                                           r.boxes.cls.int().tolist(), r.boxes.conf.tolist()):
                        rows.append([t, tid, names[c], round(x1, 1), round(y1, 1), round(x2, 1), round(y2, 1), round(s, 3)])
            f += 1
    votes: dict[int, Counter] = {}
    for r in rows:
        votes.setdefault(r[1], Counter())[r[2]] += 1
    # ByteTrack keeps a track across class flips (car/truck, person/bicycle); the track keeps its majority class.
    rows = [[r[0], r[1], votes[r[1]].most_common(1)[0][0], *r[3:]] for r in rows if votes[r[1]].total() >= MIN_BOXES]
    hdr = {"schema_version": "1", "analysis_rev": analysis_rev,
           "producer_commit": os.environ.get("SVA_PRODUCER_COMMIT", "unknown"),
           "input_artifact_hashes": {input_name or video.name: _sha256(video)}}
    out = scene_dir / "boxes" / cam
    out.mkdir(parents=True, exist_ok=True)
    chunks: dict[int, list[list]] = {}
    for r in rows:
        chunks.setdefault(r[0] // CHUNK_MS * CHUNK_MS, []).append(r)
    for c0, rs in sorted(chunks.items()):
        tmp = out / f"{c0 // CHUNK_MS:05d}.json.tmp"
        tmp.write_text(json.dumps({**hdr, "cam_id": cam, "chunk_t0_ms": c0, "rows": rs}, separators=(",", ":")))
        tmp.replace(out / f"{c0 // CHUNK_MS:05d}.json")
    print(f"detect_track {video}: {f} frames, {len(rows)} boxes, {len(votes)} tracks ({len({r[1] for r in rows})} kept)", file=sys.stderr)
    return rows


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("scene_dir")
    ap.add_argument("--cam", required=True)
    ap.add_argument("--cls", default="person,car,truck,bus,bicycle")
    ap.add_argument("--fps", type=float, default=10)
    ap.add_argument("--offset-ms", type=int, default=0)
    a = ap.parse_args()
    run(Path(a.video), Path(a.scene_dir), a.cam, a.cls.split(","), fps=a.fps, offset_ms=a.offset_ms)
