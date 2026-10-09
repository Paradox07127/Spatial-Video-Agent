"""C2 next-action accuracy (§10.4): prefix rule asserted first, then per-action hit rates against SDG forklift GT / PIE
OBD; samples with < 2 s of future are excluded from the denominator and counted; ghost trajectory error at the horizon.

  python pipeline/eval/cosmos_prediction_acc.py   ->  sva-out/_eval/cosmos_prediction_acc.json
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common.cosmos_batch import ms_frame, load_labels, load_records, sdg_gt, write_eval  # noqa: E402
from pipeline.warehouse import sdg  # noqa: E402

ACTIONS = ["stop", "slow", "turn", "continue"]


def ghost_errors(recs: list[dict], gt_of) -> tuple[list[float], int]:
    """(ground distance ghost endpoint -> GT forklift at the horizon, count of endpoints whose horizon lies past the GT run);
    gt_of(scene_key) -> sdg_gt dict or None."""
    err, excluded = [], 0
    for r in recs:
        g, gt = r["payload"].get("ghost"), gt_of(r["scene_key"])
        if not g or gt is None:
            continue
        i = ms_frame(g[-1][0])
        if i >= len(gt["task"]):
            excluded += 1
            continue
        err.append(float(np.hypot(g[-1][1] - gt["xy"][i][0], g[-1][2] - gt["xy"][i][1])))
    return err, excluded


def main() -> None:
    recs = load_records("C2")
    for r in recs:
        ts = [f["t_ms"] for f in r["input_frame_refs"]]
        assert r["prefix_only"] and r["payload"]["max_frame_ms"] <= r["t_ms"] and max(ts) == r["payload"]["max_frame_ms"] \
            and all(r["t_ms"] - 2000 <= t < r["t_ms"] for t in ts), f"prefix rule broken: {r['scene_key']} {r['record_id']}"
    labels = load_labels("C2")
    runs = {sdg.scene_id(p).replace("/", "-").lower(): p for p in sdg.SDG_ROOT.iterdir() if sdg.RUN_RE.match(p.name)}
    out = {"metric": "C2 next 2 s action, prefix-only", "prefix_rule": f"all {len(recs)} records max_frame_ms <= t",
           "truth": {"warehouse": "SDG forklift bbox3d pose (stop < 0.3 m/s, slow dv <= -0.5 m/s, turn >= 15 deg over 2 s)",
                     "fleet": "PIE OBD speed/heading (stop < 3 km/h, slow <= -3 km/h, turn >= 15 deg)"}}
    for ws, name in (("wh", "warehouse"), ("fl", "fleet")):
        rs = [r for r in recs if r["scene_key"].startswith(ws + "-")]
        pairs = [(set(lab["label"].split(",")), set(r["payload"]["actions"])) for r in rs
                 for lab in labels.get((r["scene_key"], r["t_ms"]), [])[:1]]
        per = {}
        for a in ACTIONS:
            pos = [p for t, p in pairs if a in t]
            per[a] = {"n_truth": len(pos), "hit_rate": round(sum(a in p for p in pos) / len(pos), 4) if pos else None,
                      "n_predicted": sum(a in p for _, p in pairs)}
        gts = {}

        def gt_of(key):
            run = runs.get(key)
            if run is not None and run not in gts:
                gts[run] = sdg_gt(run)
            return gts.get(run)
        err, excluded = ghost_errors(rs, gt_of)
        out[name] = {"n": len(pairs), "excluded_short_future": len(rs) - len(pairs),
                     "exact_set_accuracy": round(sum(t == p for t, p in pairs) / len(pairs), 4) if pairs else None,
                     "per_action": per,
                     "ghost_endpoint_error_m": {"n": len(err), "excluded_past_run_end": excluded,
                                                "median": round(float(np.median(err)), 3) if err else None,
                                                "p90": round(float(np.quantile(err, 0.9)), 3) if err else None,
                                                "method": "geometric extrapolation of measured speed + predicted action"}}
    write_eval("cosmos_prediction_acc", out)
    print(out["prefix_rule"])
    for name in ("warehouse", "fleet"):
        s = out[name]
        print(f"C2 {name:9s} exact={s['exact_set_accuracy']} N={s['n']} excluded={s['excluded_short_future']} "
              + " ".join(f"{a}={v['hit_rate']}({v['n_truth']})" for a, v in s["per_action"].items())
              + f" ghost_err_m median={s['ghost_endpoint_error_m']['median']} N={s['ghost_endpoint_error_m']['n']}"
              + f" excluded_past_run_end={s['ghost_endpoint_error_m']['excluded_past_run_end']}")


if __name__ == "__main__":
    main()
