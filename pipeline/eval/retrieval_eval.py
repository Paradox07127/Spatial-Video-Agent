"""Score labelled JSONL queries against method-independent ranked JSONL runs.

  python pipeline/eval/retrieval_eval.py --queries Q.jsonl --run bm25=runs/bm25.jsonl --baseline bm25 --out result.json

Gains are the label grades (2 and 1). Hits choose the best-IoU label, with ties
following label order; repeated matches earn no gain. Empty rankings have no
top-1 score: AUROC orders them below finite scores, and score means omit them.
Undefined metrics are null in JSON and '-' in the table. Bootstrap intervals
are paired percentile intervals over answerable queries, overall and by domain.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import numpy as np

KS = (1, 5, 10, 20)
METRICS = tuple(f"recall@{k}" for k in KS) + ("mrr", "ndcg@10", "success@5")
PAIRED = ("recall@10", "ndcg@10", "mrr")
SEED = 20261011
SAMPLES = 1000


def read_jsonl(path: str | Path) -> list[dict]:
    rows, seen = [], set()
    with Path(path).open(encoding="utf-8") as f:
        for line, text in enumerate(f, 1):
            if not text.strip():
                continue
            row = json.loads(text)
            qid = row["qid"]
            if qid in seen:
                raise ValueError(f"{path}:{line}: duplicate qid {qid!r}")
            seen.add(qid)
            rows.append(row)
    return rows


def validate(queries: list[dict], runs: dict[str, dict[str, list[dict]]]) -> None:
    seen = set()
    for q in queries:
        qid = q["qid"]
        if qid in seen:
            raise ValueError(f"duplicate query qid {qid!r}")
        seen.add(qid)
        if q["kind"] not in ("known_item", "multi_answer", "no_answer"):
            raise ValueError(f"{qid}: invalid query kind")
        if (q["kind"] == "no_answer") != (not q["relevant"]):
            raise ValueError(f"{qid}: relevant labels disagree with query kind")
        for m in q["relevant"]:
            if m["grade"] not in (1, 2):
                raise ValueError(f"{qid}: grade must be 1 or 2")
            validate_interval(m)
    for name, run in runs.items():
        unknown = set(run) - seen
        if unknown:
            raise ValueError(f"{name}: unknown qids {sorted(unknown)}")
        for ranked in run.values():
            for h in ranked:
                validate_interval(h)
                if not math.isfinite(h["score"]):
                    raise ValueError(f"{name}: scores must be finite")


def validate_interval(m: dict) -> None:
    a, b = m["t0_ms"], m["t1_ms"]
    if not (math.isfinite(a) and math.isfinite(b) and 0 <= a < b):
        raise ValueError("intervals must have finite 0 <= t0_ms < t1_ms")


def moments(labels: list[dict], match: str) -> list[dict]:
    out = {}
    for m in labels:
        key = (m["scene_key"], m["t0_ms"], m["t1_ms"])
        if match == "camera":
            key += (m["cam_id"],)
        if key not in out:
            out[key] = dict(m)
        else:
            out[key]["grade"] = max(out[key]["grade"], m["grade"])
    return list(out.values())


def tiou(a: dict, b: dict) -> float:
    overlap = max(0, min(a["t1_ms"], b["t1_ms"]) - max(a["t0_ms"], b["t0_ms"]))
    union = a["t1_ms"] - a["t0_ms"] + b["t1_ms"] - b["t0_ms"] - overlap
    return overlap / union


def score_query(q: dict, ranked: list[dict], match: str, threshold: float) -> dict:
    row = {"domain": q["domain"], "kind": q["kind"], "top1_score": ranked[0]["score"] if ranked else None}
    if q["kind"] == "no_answer":
        return {**row, **dict.fromkeys(METRICS)}
    labels = moments(q["relevant"], match)
    used, matched = set(), []
    for rank, h in enumerate(ranked, 1):
        best, iou = None, -1.0
        for i, m in enumerate(labels):
            if h["scene_key"] != m["scene_key"] or (match == "camera" and h["cam_id"] != m["cam_id"]):
                continue
            v = tiou(h, m)
            if v >= threshold and v > iou:
                best, iou = i, v
        if best is not None and best not in used:
            used.add(best)
            matched.append((rank, labels[best]["grade"]))
    row.update({f"recall@{k}": sum(rank <= k for rank, _ in matched) / len(labels) for k in KS})
    row["mrr"] = 1 / matched[0][0] if matched else 0.0
    dcg = sum(grade / math.log2(rank + 1) for rank, grade in matched if rank <= 10)
    ideal = sum(grade / math.log2(rank + 1) for rank, grade in enumerate(sorted((m["grade"] for m in labels), reverse=True)[:10], 1))
    row["ndcg@10"] = dcg / ideal
    row["success@5"] = float(any(rank <= 5 and grade == 2 for rank, grade in matched))
    return row


def auroc(rows: list[dict]) -> float | None:
    n_pos = sum(r["kind"] != "no_answer" for r in rows)
    n_neg = len(rows) - n_pos
    if not n_pos or not n_neg:
        return None
    groups = {}
    for r in rows:
        score = r["top1_score"] if r["top1_score"] is not None else -math.inf
        pos, neg = groups.get(score, (0, 0))
        answerable = r["kind"] != "no_answer"
        groups[score] = (pos + answerable, neg + (not answerable))
    wins, below = 0.0, 0
    for score in sorted(groups):
        pos, neg = groups[score]
        wins += pos * (below + neg / 2)
        below += neg
    return wins / (n_pos * n_neg)


def summarize(rows: list[dict]) -> dict:
    answerable = [r for r in rows if r["kind"] != "no_answer"]
    scores = [r["top1_score"] for r in rows if r["kind"] == "no_answer" and r["top1_score"] is not None]
    return {**{m: float(np.mean([r[m] for r in answerable])) if answerable else None for m in METRICS},
            "n_answerable": len(answerable), "n_no_answer": len(rows) - len(answerable),
            "n_no_answer_scored": len(scores), "no_answer_top1_mean": float(np.mean(scores)) if scores else None,
            "auroc": auroc(rows)}


def bootstrap(rows: dict, base: dict, qids: list[str]) -> dict:
    if not qids:
        return {m: {"mean_difference": None, "ci95": None} for m in PAIRED}
    delta = np.array([[rows[qid][m] - base[qid][m] for m in PAIRED] for qid in qids])
    rng = np.random.default_rng(SEED)
    samples = np.array([delta[rng.integers(0, len(qids), size=len(qids))].mean(axis=0) for _ in range(SAMPLES)])
    lo, hi = np.quantile(samples, (0.025, 0.975), axis=0)
    means = delta.mean(axis=0)
    return {m: {"mean_difference": float(means[i]), "ci95": [float(lo[i]), float(hi[i])]} for i, m in enumerate(PAIRED)}


def evaluate(queries: list[dict], runs: dict[str, dict[str, list[dict]]], baseline: str | None = None,
             match: str = "moment", tiou: float = 0.5) -> dict:
    if match not in ("moment", "camera") or not 0 <= tiou <= 1:
        raise ValueError("match must be moment or camera and tiou must be between 0 and 1")
    if baseline is not None and baseline not in runs:
        raise ValueError(f"baseline {baseline!r} is not a run")
    validate(queries, runs)
    domains = sorted({q["domain"] for q in queries})
    result = {"match": match, "tiou": tiou, "baseline": baseline, "seed": SEED,
              "bootstrap_samples": SAMPLES, "runs": {}, "bootstrap": {}}
    for name, run in runs.items():
        rows = {q["qid"]: score_query(q, run.get(q["qid"], []), match, tiou) for q in queries}
        result["runs"][name] = {"overall": summarize(list(rows.values())),
                                "per_domain": {d: summarize([r for r in rows.values() if r["domain"] == d]) for d in domains},
                                "per_query": rows}
    if baseline is not None:
        base = result["runs"][baseline]["per_query"]
        qids = [q["qid"] for q in queries if q["kind"] != "no_answer"]
        for name, run in result["runs"].items():
            rows = run["per_query"]
            result["bootstrap"][name] = {"overall": bootstrap(rows, base, qids),
                                         "per_domain": {d: bootstrap(rows, base, [qid for qid in qids if rows[qid]["domain"] == d]) for d in domains}}
    return result


def markdown(result: dict) -> str:
    cols = ("n_answerable", "n_no_answer", "n_no_answer_scored") + METRICS + ("no_answer_top1_mean", "auroc")
    lines = ["| Run | Scope | " + " | ".join(cols) + " |", "| --- | --- | " + " | ".join("---:" for _ in cols) + " |"]
    def fmt(v):
        return "-" if v is None else str(v) if isinstance(v, int) else f"{v:.4f}"
    def cell(v):
        return str(v).replace("|", "\\|").replace("\n", " ")
    for name, run in result["runs"].items():
        for scope, row in [("overall", run["overall"]), *run["per_domain"].items()]:
            lines.append(f"| {cell(name)} | {cell(scope)} | " + " | ".join(fmt(row[c]) for c in cols) + " |")
    if result["bootstrap"]:
        lines += ["", f"Paired differences vs {cell(result['baseline'])} ({SAMPLES} resamples, seed {SEED}).", "",
                  "| Run | Scope | Metric | Mean difference | 95% CI |", "| --- | --- | --- | ---: | --- |"]
        for name, run in result["bootstrap"].items():
            for scope, row in [("overall", run["overall"]), *run["per_domain"].items()]:
                for m, v in row.items():
                    ci = v["ci95"]
                    interval = f"[{fmt(ci[0])}, {fmt(ci[1])}]" if ci is not None else "-"
                    lines.append(f"| {cell(name)} | {cell(scope)} | {m} | {fmt(v['mean_difference'])} | {interval} |")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--run", action="append", required=True, metavar="NAME=PATH")
    ap.add_argument("--baseline")
    ap.add_argument("--match", choices=("moment", "camera"), default="moment")
    ap.add_argument("--tiou", type=float, default=0.5)
    ap.add_argument("--out")
    a = ap.parse_args()
    try:
        queries, runs = read_jsonl(a.queries), {}
        for spec in a.run:
            name, sep, path = spec.partition("=")
            if not sep or not name or not path or name in runs:
                raise ValueError("--run requires a unique NAME=PATH")
            runs[name] = {r["qid"]: r["ranked"] for r in read_jsonl(path)}
        result = evaluate(queries, runs, a.baseline, a.match, a.tiou)
        if a.out:
            Path(a.out).write_text(json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    except (OSError, ValueError, KeyError, TypeError) as e:
        ap.error(str(e))
    print(markdown(result))


if __name__ == "__main__":
    main()
