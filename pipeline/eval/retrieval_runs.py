"""Ranked JSONL runs for retrieval_eval.py from labelled queries and a read-only index.

  python pipeline/eval/retrieval_runs.py --queries Q.jsonl --index INDEX.duckdb --methods app,bm25,txt,vis,rrf:bm25+txt+vis --k 50 --out-dir DIR

Each query ranks only the clips of its domain workspace. bm25 is Okapi BM25 (k1 1.2,
b 0.75) over captions; txt and vis are the cosine of the query embedding against
v_txt and v_vis; rrf:a+b scores sum(1/(60+rank)) with 1-based ranks. app is the
product search (scene scope over the whole workspace) on a dev-mode Store whose
assets come from Settings (ASSET_DIR). Writes DIR/<method>.jsonl with ':' as '_'.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import tempfile
import time
from collections import Counter
from functools import lru_cache
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from app.core import embed  # noqa: E402
from pipeline.eval.retrieval_eval import read_jsonl  # noqa: E402

STOP = frozenset("a an and are as at be by did do does for from has have in is it its of on or show that the "
                 "their there this to was were what when where which who with".split())
K1, B, RRF_K = 1.2, 0.75, 60


@lru_cache(maxsize=None)
def tokens(text: str) -> Counter:
    return Counter(w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP)


def ranked(scores: dict[str, float]) -> list[tuple[str, float]]:
    return sorted(scores.items(), key=lambda x: (-x[1], x[0]))


def bm25_rank(docs: dict[str, str | None], query: str) -> list[tuple[str, float]]:
    """Clips that share no query word are left out."""
    tf = {i: tokens(t) for i, t in docs.items() if t is not None}
    if not tf:
        return []
    avg = sum(sum(c.values()) for c in tf.values()) / len(tf)
    scores = {}
    for w in tokens(query):
        hits = [(i, c[w], sum(c.values())) for i, c in tf.items() if w in c]
        idf = math.log((len(tf) - len(hits) + 0.5) / (len(hits) + 0.5) + 1)
        for i, f, n in hits:
            scores[i] = scores.get(i, 0.0) + idf * f * (K1 + 1) / (f + K1 * (1 - B + B * n / avg))
    return ranked(scores)


def cosine_rank(mat: dict[str, list[float] | None], qvec: list[float]) -> list[tuple[str, float]]:
    ids = [i for i, v in mat.items() if v is not None]
    if not ids:
        return []
    m = np.asarray([mat[i] for i in ids], dtype=np.float64)
    q = np.asarray(qvec, dtype=np.float64)
    norms = np.linalg.norm(m, axis=1) * np.linalg.norm(q)
    # a zero vector has no direction, so it has no finite cosine
    return ranked({i: float(s / n) for i, s, n in zip(ids, m @ q, norms) if n > 0})


def rrf(rankings: list[list[tuple[str, float]]]) -> list[tuple[str, float]]:
    scores = {}
    for r in rankings:
        for rank, (i, _) in enumerate(r, 1):
            scores[i] = scores.get(i, 0.0) + 1 / (RRF_K + rank)
    return ranked(scores)


def app_rank(queries: list[dict], index: str, k: int) -> tuple[dict, dict]:
    from app.api.search import SearchRequest, retrieve
    from app.core.store import Store
    from app.settings import Settings
    out, hits = {}, {}
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["RUNS_DIR"] = str(Path(tmp) / "runs")
        store = Store(index_path=Path(index), app_path=Path(tmp) / "app.duckdb", asset_dir=Settings().asset_dir)
        try:
            for q in queries:
                _, res = retrieve(store, SearchRequest(q=q["text"], workspace=q["domain"], k=min(k, 50), scope="scene"))
                cards = sorted(res.envelope.output["moments"], key=lambda m: (-m["score"], m["moment_id"]))
                out[q["qid"]] = [(m["moment_id"], m["score"]) for m in cards]
                hits.update((m["moment_id"], (m["scene_key"], m["primary_cam"], m["t0_ms"], m["t1_ms"])) for m in cards)
        finally:
            store.close()
    return out, hits


def run(queries: list[dict], index: str, methods: list[str], k: int, out_dir: str | Path) -> list[tuple]:
    con = duckdb.connect(index, read_only=True)
    try:
        rows = con.execute("SELECT clip_id, workspace, scene_key, cam_id, t0_ms, t1_ms, caption, v_txt, v_vis "
                           "FROM clips").fetchall()
    finally:
        con.close()
    hits = {r[0]: r[2:6] for r in rows}
    col = {name: {d: {r[0]: r[j] for r in rows if r[1] == d} for d in {r[1] for r in rows}}
           for name, j in (("bm25", 6), ("txt", 7), ("vis", 8))}
    qvecs, cache = {}, {}

    def rank(method: str) -> dict[str, list[tuple[str, float]]]:
        if method not in cache:
            if method == "app":
                cache[method], app_hits = app_rank(queries, index, k)
                hits.update(app_hits)
            elif method.startswith("rrf:"):
                parts = [rank(m) for m in method[4:].split("+")]
                cache[method] = {q["qid"]: rrf([p[q["qid"]] for p in parts]) for q in queries}
            elif method == "bm25":
                cache[method] = {q["qid"]: bm25_rank(col["bm25"].get(q["domain"], {}), q["text"]) for q in queries}
            elif method in ("txt", "vis"):
                for q in queries:
                    if q["qid"] not in qvecs:
                        qvecs[q["qid"]] = embed.embed_text(q["text"])
                cache[method] = {q["qid"]: cosine_rank(col[method].get(q["domain"], {}), qvecs[q["qid"]])
                                 for q in queries}
            else:
                raise ValueError(f"unknown method {method!r}")
        return cache[method]

    Path(out_dir).mkdir(parents=True, exist_ok=True)
    stats = []
    for method in methods:
        started = time.monotonic()
        runs = rank(method)
        lines = []
        for q in queries:
            top = [dict(zip(("scene_key", "cam_id", "t0_ms", "t1_ms"), hits[i]), score=s) for i, s in runs[q["qid"]][:k]]
            lines.append(json.dumps({"qid": q["qid"], "ranked": top}, allow_nan=False))
        (Path(out_dir) / (method.replace(":", "_") + ".jsonl")).write_text("".join(x + "\n" for x in lines), encoding="utf-8")
        stats.append((method, len(queries), sum(min(len(runs[q["qid"]]), k) for q in queries) / max(len(queries), 1),
                      time.monotonic() - started))
    return stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--queries", required=True)
    ap.add_argument("--index", required=True)
    ap.add_argument("--methods", default="app,bm25,txt,vis,rrf:bm25+txt+vis")
    ap.add_argument("--k", type=int, default=50)
    ap.add_argument("--out-dir", required=True)
    a = ap.parse_args()
    for method, n, mean, secs in run(read_jsonl(a.queries), a.index, a.methods.split(","), a.k, a.out_dir):
        print(f"{method}\tqueries={n}\tmean_ranked={mean:.1f}\tseconds={secs:.1f}")


if __name__ == "__main__":
    main()
