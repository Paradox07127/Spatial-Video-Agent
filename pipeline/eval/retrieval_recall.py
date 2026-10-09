"""Recall@5 and top-1 temporal IoU of the 20 scripted queries (manifests/queries.yaml) for three retrievers.

  python pipeline/eval/retrieval_recall.py [--before-after] [--index PATH] [--site-map PATH] [--out JSON]

Columns:
  search_default   VSS POST /search (top_k 100, the API maximum) over captions from the default ingest prompt
  search_reingest  the same after re-ingesting the evaluated chunks with reingest_prompt.md (--before-after only)
  search_struct    search_default fused (RRF, k = 60) with our index.duckdb clips ranked by Embed1 similarity of the
                   query to v_txt (structured captions) and to v_vis
Evaluated scenes = published scenes that the VSS corpus covers (site_map hits); every column keeps only results in those
scenes and in the query scope, and expected intervals outside them are dropped. --before-after (dev, ws :8100) re-ingests
every corpus chunk of the evaluated scenes inside a query scope with the structured prompt, measures, then re-ingests the same chunks without a
custom prompt so the shared index returns to the default prompt. Writes {rows: [{metric, column, value, n,
data_version}], per_query} for weave_publish.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from contextlib import ExitStack
from pathlib import Path

import duckdb
import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from pipeline.common import lease, site_map  # noqa: E402
from pipeline.common.caption import load_prompt, prompt_rev  # noqa: E402
from pipeline.common.embed import embed_texts  # noqa: E402
from pipeline.common.index_build import OUT_ROOT  # noqa: E402

QUERIES = Path(__file__).resolve().parents[1] / "manifests" / "queries.yaml"
VSS_URL = os.environ.get("VSS_URL", "http://127.0.0.1:8100")
MOMENT_MS = 5000
TOP_K = 5
RRF_K = 60
MOCK_POOL = 6  # :8100 runs re-ingest segments 6 at a time, each calling :8101/:8103


class VSS:
    def __init__(self):
        self.base = VSS_URL.rstrip("/") + "/api/v1"
        self.c = httpx.Client(timeout=300)
        tok = self.c.post(f"{self.base}/auth/login", json={"username": os.environ["VSS_USERNAME"],
                                                           "password": os.environ["VSS_PASSWORD"]}).raise_for_status().json()
        self.c.headers["Authorization"] = f"Bearer {tok['access_token']}"

    def search(self, q: str) -> list[dict]:
        return self.c.post(f"{self.base}/search", json={"query": q, "top_k": 100}).raise_for_status().json()["results"]

    def reingest(self, videos: list[str], prompt: str | None) -> list[str]:
        """Starts one job per chunk, waits for all; returns error strings."""
        jobs = [self.c.post(f"{self.base}/dashboard/reingest", json={"original_video": v, "chunk_count": 1,
                                                                      **({"custom_prompt": prompt} if prompt else {})}
                            ).raise_for_status().json()["job_id"] for v in videos]
        errs = []
        for j in jobs:
            while (st := self.c.get(f"{self.base}/dashboard/reingest/{j}").raise_for_status().json())["status"] in ("queued", "running"):
                time.sleep(4)
            errs += st["errors"]
        return errs


def moments(hits: list[tuple[str, int]]) -> list[tuple[str, int]]:
    """(scene_key, t_ms) in rank order -> distinct (scene_key, moment index); the same moment on several cams counts once."""
    out = []
    for key, t in hits:
        m = (key, t // MOMENT_MS)
        if m not in out:
            out.append(m)
    return out


def vss_moments(results: list[dict], maps: tuple[dict, dict], keep) -> list[tuple[str, int]]:
    hits = []
    for r in results:
        row = site_map.map_hit(maps, r)
        if row and keep(row["scene_key"]):
            hits.append((row["scene_key"], row["t_ms"]))
    return moments(hits)


def index_moments(con, qv: list[float], col: str, keep) -> list[tuple[str, int]]:
    rows = con.execute(f"SELECT scene_key, t0_ms FROM clips WHERE {col} IS NOT NULL "
                       f"ORDER BY array_cosine_similarity({col}, ?::FLOAT[256]) DESC", [qv]).fetchall()
    return moments([(k, t) for k, t in rows if keep(k)])[:50]


def rrf(*ranked: list) -> list:
    score: dict = {}
    for lst in ranked:
        for r, m in enumerate(lst, 1):
            score[m] = score.get(m, 0) + 1 / (RRF_K + r)
    return sorted(score, key=lambda m: -score[m])


def score(top: list[tuple[str, int]], expected: list[list]) -> tuple[float, float]:
    """(Recall@5, IoU of the top-1 moment with the expected interval it overlaps most)."""
    relevant = {(k, i) for k, a, b in expected for i in range(a // MOMENT_MS, (b - 1) // MOMENT_MS + 1)}
    recall = len(relevant & set(top[:TOP_K])) / min(TOP_K, len(relevant))
    iou = 0.0
    if top:
        k, i = top[0]
        a0, b0 = i * MOMENT_MS, (i + 1) * MOMENT_MS
        for kk, a, b in expected:
            if kk == k:
                iou = max(iou, max(0, min(b, b0) - max(a, a0)) / (max(b, b0) - min(a, a0)))
    return recall, iou


def sha8(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()[:8] if p.is_file() else "none"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before-after", action="store_true")
    ap.add_argument("--queries", default=str(QUERIES))
    ap.add_argument("--index", default=str(OUT_ROOT / "index.duckdb"))
    ap.add_argument("--site-map", default=str(OUT_ROOT / "site_map.json"))
    ap.add_argument("--out", default=str(OUT_ROOT / "_logs" / "P-C" / "eval" / "retrieval_recall.json"))
    a = ap.parse_args()
    queries = yaml.safe_load(Path(a.queries).read_text())
    smap = json.loads(Path(a.site_map).read_text())["map"]
    maps = site_map.load(Path(a.site_map))
    scenes = {r["scene_key"] for r in smap.values() if r["hit"]}
    scopes = tuple({q["scope"] for q in queries})
    chunks = sorted({r["original_video"] for r in smap.values() if r["hit"] and r["scene_key"].startswith(scopes)})
    evald = []
    for q in queries:
        exp = [e for e in q["expected"] if e[0] in scenes]
        if exp:
            evald.append({**q, "expected": exp, "keep": lambda k, s=q["scope"]: k in scenes and k.startswith(s)})
    print(f"retrieval_recall: {len(scenes)} evaluated scenes, {len(chunks)} corpus chunks, "
          f"{len(evald)}/{len(queries)} queries with expected moments in them", file=sys.stderr)
    if not evald:
        sys.exit("retrieval_recall: no query has expected moments in a published scene covered by the corpus")
    vss = VSS()
    ranked: dict[str, dict[str, list]] = {"search_default": {q["id"]: vss_moments(vss.search(q["q"]), maps, q["keep"])
                                                             for q in evald}}
    rev = None
    if a.before_after:
        prompt = load_prompt()
        rev = prompt_rev(prompt)
        with ExitStack() as hold:
            for _ in range(MOCK_POOL):
                hold.enter_context(lease.vlm_slot(owner="P-C"))
            print(f"re-ingesting {len(chunks)} chunks with prompt_rev {rev}", file=sys.stderr)
            errs = vss.reingest(chunks, prompt)
            ranked["search_reingest"] = {q["id"]: vss_moments(vss.search(q["q"]), maps, q["keep"]) for q in evald}
            print(f"restoring the default prompt on {len(chunks)} chunks", file=sys.stderr)
            errs += vss.reingest(chunks, None)
        for e in errs[:20]:
            print(f"  reingest error: {e}", file=sys.stderr)
    con = duckdb.connect(a.index, read_only=True) if Path(a.index).is_file() else None
    n_vec = con.execute("SELECT count(v_txt) + count(v_vis) FROM clips").fetchone()[0] if con else 0
    if n_vec:
        qvecs = embed_texts([q["q"] for q in evald])
        ranked["search_struct"] = {q["id"]: rrf(ranked["search_default"][q["id"]], index_moments(con, v, "v_txt", q["keep"]),
                                                index_moments(con, v, "v_vis", q["keep"]))
                                   for q, v in zip(evald, qvecs)}
    else:
        print(f"search_struct: skipped, {a.index} has no clip vectors", file=sys.stderr)
    dv = (f"queries@{sha8(Path(a.queries))} index@{sha8(Path(a.index))} site_map@{sha8(Path(a.site_map))} "
          f"scenes={len(scenes)}" + (f" prompt_rev={rev}" if rev else ""))
    per_query, rows = [], []
    for col, rk in ranked.items():
        sc = {q["id"]: score(rk[q["id"]], q["expected"]) for q in evald}
        for q in evald:
            per_query.append({"id": q["id"], "column": col, "source": q["source"], "recall@5": sc[q["id"]][0],
                              "tiou@1": sc[q["id"]][1], "top": rk[q["id"]][:TOP_K]})
        for i, metric in enumerate(("recall@5", "tiou@1")):
            rows.append({"metric": metric, "column": col, "value": round(sum(s[i] for s in sc.values()) / len(sc), 4),
                         "n": len(sc), "data_version": dv})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"rows": rows, "per_query": per_query}, indent=1))
    print(f"{'metric':10s} " + " ".join(f"{c:>16s}" for c in ranked) + "   n")
    for metric in ("recall@5", "tiou@1"):
        vals = {r["column"]: r["value"] for r in rows if r["metric"] == metric}
        print(f"{metric:10s} " + " ".join(f"{vals[c]:16.3f}" for c in ranked) + f"   {len(evald)}")
    print(f"data_version: {dv} -> {a.out}")


if __name__ == "__main__":
    main()
