"""C1 situation accuracy and confusion matrix, separately against rule labels and human labels (§10.4).

  python pipeline/eval/cosmos_situation_acc.py   ->  sva-out/_eval/cosmos_situation_acc.json
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common.cosmos_batch import load_labels, load_records, write_eval  # noqa: E402


def score(pairs: list[tuple[str, str]]) -> dict:
    """pairs of (truth, predicted) -> accuracy, N, confusion[truth][predicted]."""
    conf: dict[str, Counter] = {}
    for t, p in pairs:
        conf.setdefault(t, Counter())[p] += 1
    return {"n": len(pairs), "accuracy": round(sum(t == p for t, p in pairs) / len(pairs), 4) if pairs else None,
            "confusion": {t: dict(c) for t, c in sorted(conf.items())}}


def main() -> None:
    labels = load_labels("C1")
    recs = load_records("C1")
    out = {"metric": "C1 situation label accuracy", "n_records": len(recs),
           "truth": {"rule": "situation_map x-sva-rules: SDG worker Dodge + forklift speed/distance; PIE cross/action",
                     "human": "fixtures/labels source=human"}}
    for src in ("rule", "human"):
        res = {}
        for ws in ("wh", "fl"):
            pairs = [(lab["label"], r["payload"]["label"]) for r in recs if r["scene_key"].startswith(ws + "-")
                     for lab in labels.get((r["scene_key"], r["t_ms"]), []) if lab["source"] == src]
            res[{"wh": "warehouse", "fl": "fleet"}[ws]] = score(pairs)
        out[src] = res
    write_eval("cosmos_situation_acc", out)
    for src in ("rule", "human"):
        for ws, s in out[src].items():
            print(f"C1 {src:5s} {ws:9s} accuracy={s['accuracy']} N={s['n']}")


if __name__ == "__main__":
    main()
