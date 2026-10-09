"""C3 occluder boxes: geometric support rate from the SDG depth ray test written by cosmos_batch (§10.4). Semantic
correctness needs the 20 human-labelled examples; until they exist it is reported as unverified.

  python pipeline/eval/cosmos_grounding_check.py   ->  sva-out/_eval/cosmos_grounding_check.json
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common.cosmos_batch import load_records, write_eval  # noqa: E402


def main() -> None:
    recs = load_records("C3")
    objs = [o for r in recs for o in r["payload"]["objects"]]
    tested = [o for o in objs if o["depth_residual_m"] is not None]
    sup = [o for o in tested if o["geometric_support"]]
    out = {"metric": "C3 occluder geometric support rate", "truth": "SDG depth.mp4 ray test (box surface in front of floor)",
           "n_records": len(recs), "n_objects": len(objs), "n_depth_tested": len(tested), "n_supported": len(sup),
           "support_rate": round(len(sup) / len(tested), 4) if tested else None,
           "records_with_support": sum(any(o["geometric_support"] for o in r["payload"]["objects"]) for r in recs),
           "by_label": {k: {"n": n, "supported": sum(o["label"] == k for o in sup)}
                        for k, n in Counter(o["label"] for o in tested).items()},
           "semantic": "unverified (no human labels)"}
    write_eval("cosmos_grounding_check", out)
    print(f"C3 support_rate={out['support_rate']} N={out['n_depth_tested']} objects ({out['n_records']} records, "
          f"{out['records_with_support']} with a supported occluder)")


if __name__ == "__main__":
    main()
