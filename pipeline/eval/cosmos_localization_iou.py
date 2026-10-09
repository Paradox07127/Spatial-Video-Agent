"""C4 temporal localisation for the fixed query "when does the worker start yielding" (§10.4): start error (s) against
the first SDG Dodge frame and temporal IoU with the first Dodge run; runs without Dodge must abstain; runs whose first
frame is already Dodge are boundary_censored and not scored on start.

  python pipeline/eval/cosmos_localization_iou.py   ->  sva-out/_eval/cosmos_localization_iou.json
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common.cosmos_batch import load_labels, load_records, write_eval  # noqa: E402


def main() -> None:
    labels = load_labels("C4")
    errs, ious, abst, missed, censored = [], [], [], 0, 0
    for r in load_records("C4"):
        lab = next(iter(labels.get((r["scene_key"], r["t_ms"]), [])), None)
        if lab is None:
            continue
        segs = r["payload"]["segments"]
        if lab["label"] == "abstain":
            abst.append(not segs)
        elif lab["label"] == "boundary_censored":
            censored += 1
        elif not segs:
            missed += 1
        else:
            a, b = (int(x) / 1000 for x in re.search(r"\[(\d+), (\d+)\)", lab["note"]).groups())
            s, e = segs[0]["start_s"], segs[0]["end_s"]
            errs.append(abs(s - a))
            inter = max(0.0, min(b, e) - max(a, s))
            ious.append(inter / (max(b, e) - min(a, s)) if max(b, e) > min(a, s) else 0.0)
    q = lambda v, p: round(float(np.quantile(v, p)), 3) if v else None  # noqa: E731
    out = {"metric": "C4 start error vs first Dodge frame", "truth": "SDG worker current_task_name == Dodge",
           "n_scored": len(errs), "start_error_s": {"median": q(errs, 0.5), "p90": q(errs, 0.9),
                                                     "within_1s": round(float(np.mean([e <= 1 for e in errs])), 4) if errs else None},
           "temporal_iou": {"mean": round(float(np.mean(ious)), 4) if ious else None, "median": q(ious, 0.5)},
           "missed_dodge_runs": missed, "boundary_censored": censored,
           "abstain": {"n": len(abst), "correct_rate": round(float(np.mean(abst)), 4) if abst else None}}
    write_eval("cosmos_localization_iou", out)
    print(f"C4 start_error_s median={out['start_error_s']['median']} N={len(errs)} IoU mean={out['temporal_iou']['mean']} "
          f"missed={missed} censored={censored} abstain_correct={out['abstain']['correct_rate']} N={len(abst)}")


if __name__ == "__main__":
    main()
