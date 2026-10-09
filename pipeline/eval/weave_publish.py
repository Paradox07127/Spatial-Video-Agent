"""Push evaluation results to one Weave Evaluation (spec §8.4): one row per metric x column, each with N and data version.

  python pipeline/eval/weave_publish.py [--dry-run] RESULT.json [...]

Each RESULT.json holds {"rows": [{metric, column, value, n, data_version}]} (retrieval_recall.py, latency.py,
ui_audit.py; other eval scripts write the same shape). Project = WEAVE_PROJECT or WANDB_TEAM/WANDB_PROJECT; the key comes
from WANDB_API_KEY in the environment. --dry-run prints the table only.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    rows = [{**r, "file": Path(p).name} for p in a.results for r in json.loads(Path(p).read_text())["rows"]]
    print(f"{'metric':24s} {'column':18s} {'value':>10s} {'n':>5s}  data_version")
    for r in rows:
        print(f"{r['metric']:24s} {str(r['column'])[:18]:18s} {r['value']:10.4g} {r['n']:5d}  {r['data_version']}")
    if a.dry_run:
        print(f"weave_publish dry run: {len(rows)} rows from {len(a.results)} files")
        return
    import weave
    weave.init(os.environ.get("WEAVE_PROJECT") or f"{os.environ['WANDB_TEAM']}/{os.environ['WANDB_PROJECT']}")
    ev = weave.EvaluationLogger(name="sva-eval", model="sva", dataset="sva-eval-results")
    for r in rows:
        ev.log_example(inputs={"metric": r["metric"], "column": r["column"], "data_version": r["data_version"], "source": r["file"]},
                       output=r["value"], scores={"value": r["value"], "n": r["n"]})
    ev.log_summary({f"{r['metric']}/{r['column']}": r["value"] for r in rows})
    print(f"weave_publish: {len(rows)} rows -> {ev.ui_url}")


if __name__ == "__main__":
    main()
