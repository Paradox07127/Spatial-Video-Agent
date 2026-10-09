"""F4: RTMPose-m (rtmlib, ONNX) on the person boxes, cropped from the source frames -> _work/<scene_key>/pose2d.json.

  python pipeline/fleet/pose2d.py SCENE_KEY

rows [t_ms, local_track_id, 17 x [u, v, conf]] (COCO-17, source px). Boxes lower than SMALL_PX keep only the head and
torso joints; the limbs get conf 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease  # noqa: E402
from pipeline.fleet import pie  # noqa: E402

MODEL = ("https://download.openmmlab.com/mmpose/v1/projects/rtmposev1/onnx_sdk/"
         "rtmpose-m_simcc-body7_pt-body7_420e-256x192-e48f03d0_20230504.zip")
SMALL_PX = 60
LIMBS = [7, 8, 9, 10, 13, 14, 15, 16]  # elbows, wrists, knees, ankles


def run(key: str) -> Path:
    vid, out, work = pie.vid_of(key), pie.tmp_dir(key), pie.work_dir(key)
    by_t: dict[int, list[list]] = {}
    for r in pie.load_boxes(out):
        if r[2] == "person":
            by_t.setdefault(r[0], []).append(r)
    rows = []
    with lease.acquire(cpu=4, gpu=1, owner=pie.CARD):
        import onnxruntime
        from rtmlib import RTMPose
        onnxruntime.preload_dlls(directory="")  # the CUDA provider finds cuDNN only in the nvidia wheels torch installed
        pose =RTMPose(MODEL, model_input_size=(192, 256), backend="onnxruntime", device="cuda")
        cap = cv2.VideoCapture(str(pie.clip(vid)))
        f = 0
        while cap.grab():
            boxes = by_t.get(pie.t_ms(f))
            if boxes:
                _, img = cap.retrieve()
                kp, sc = pose(img, bboxes=[r[3:7] for r in boxes])
                for r, k, s in zip(boxes, kp, np.clip(sc, 0, 1)):
                    if r[6] - r[4] < SMALL_PX:
                        s[LIMBS] = 0
                    rows.append([r[0], r[1], [[round(float(u), 1), round(float(v), 1), round(float(c), 2)]
                                              for (u, v), c in zip(k, s)]])
            f += 1
    pie.write_json(work / "pose2d.json", {**pie.header([pie.clip(vid)]), "rows": rows})
    print(f"pose2d {key}: {len(rows)} person boxes", file=sys.stderr)
    return work / "pose2d.json"


if __name__ == "__main__":
    run(sys.argv[1])
