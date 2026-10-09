"""p50/p95 latency of POST /api/search (full response) and POST /api/agent/ask (first SSE event and `done`), cold and cached.

  python pipeline/eval/latency.py BASE_URL [--out JSON]

/api/search sends the 20 queries of manifests/queries.yaml; /api/agent/ask sends the asks of fixtures/asks.json. Pass 1 is
cold (first time each request is seen), pass 2 repeats the same requests (cache hits); ask_cached_on_second_pass counts the
pass-2 asks whose status frame says cached. Budgets (spec §7.4, §13.3): cached
ask < 3 s, cold search p95 < 600 ms. Writes {rows: [{metric, column, value, n, data_version}]} for weave_publish.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

import httpx
import yaml

SVA = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SVA))
from pipeline.common.index_build import OUT_ROOT  # noqa: E402

QUERIES = SVA / "pipeline" / "manifests" / "queries.yaml"
ASKS = SVA / "fixtures" / "asks.json"
WORKSPACE = {"wh": "warehouse", "fl": "fleet"}


def pct(xs: list[float], p: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, round(p / 100 * (len(s) - 1)))]


def timed_search(c: httpx.Client, base: str, q: dict) -> float:
    t0 = time.perf_counter()
    c.post(f"{base}/api/search", json={"q": q["q"], "workspace": WORKSPACE[q["scope"][:2]]}).raise_for_status()
    return (time.perf_counter() - t0) * 1000


def timed_ask(c: httpx.Client, base: str, ask: dict) -> tuple[float, float, bool]:
    """(ms to the first SSE event, ms to `done`, status.cached). The stream is read to its end: the server caches the run
    only after its last frame is taken."""
    t0 = time.perf_counter()
    first = total = cached = ev = None
    with c.stream("POST", f"{base}/api/agent/ask", json={"q": ask["q"], "context": ask["context"], "budget": ask["budget"]}) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if line.startswith("event:"):
                ev = line.removeprefix("event:").strip()
                first = (time.perf_counter() - t0) * 1000 if first is None else first
                total = (time.perf_counter() - t0) * 1000 if ev == "done" else total
            elif line.startswith("data:") and ev == "status":
                cached = json.loads(line.removeprefix("data:"))["cached"]
    if total is None:
        raise RuntimeError(f"ask {ask['ask_id']}: stream ended without a done event")
    return first, total, bool(cached)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url")
    ap.add_argument("--out", default=str(OUT_ROOT / "_logs" / "P-C" / "eval" / "latency.json"))
    a = ap.parse_args()
    base = a.base_url.rstrip("/")
    queries = yaml.safe_load(QUERIES.read_text())
    asks = json.loads(ASKS.read_text())["asks"]
    dv = f"{base} queries@{hashlib.sha256(QUERIES.read_bytes()).hexdigest()[:8]}"
    rows = []

    def add(name: str, col: str, ms: list[float]) -> None:
        for p in (50, 95):
            rows.append({"metric": f"{name}_p{p}_ms", "column": col, "value": round(pct(ms, p), 1), "n": len(ms), "data_version": dv})
    with httpx.Client(timeout=120) as c:
        for col in ("cold", "cached"):
            add("search", col, [timed_search(c, base, q) for q in queries])
        for col in ("cold", "cached"):
            res = [timed_ask(c, base, x) for x in asks]
            add("ask_first_event", col, [r[0] for r in res])
            add("ask_done", col, [r[1] for r in res])
        not_cached = [x["ask_id"] for x, r in zip(asks, res) if not r[2]]
        rows.append({"metric": "ask_cached_on_second_pass", "column": "cached", "value": len(asks) - len(not_cached),
                     "n": len(asks), "data_version": dv})
    Path(a.out).parent.mkdir(parents=True, exist_ok=True)
    Path(a.out).write_text(json.dumps({"rows": rows}, indent=1))
    for r in rows:
        print(f"{r['metric']:26s} {r['column']:7s} {r['value']:9.1f} {'' if r['metric'].endswith('pass') else 'ms'}  n={r['n']}")
    if not_cached:
        print(f"second pass not served from cache (status.cached false): {', '.join(not_cached)}")
    print(f"data_version: {dv} -> {a.out}")


if __name__ == "__main__":
    main()
