"""Ranked runs from a tiny index: BM25, cosine and reciprocal rank fusion."""
import json
import math

import duckdb
import pytest

from pipeline.eval import retrieval_runs as rr
from pipeline.eval.retrieval_eval import read_jsonl, validate


def test_bm25_ranks_rare_query_word_first():
    docs = {"a": "A person walks past the shelf", "b": "A person walks near the forklift",
            "c": "A person stands by the shelf", "d": None}
    ranked = rr.bm25_rank(docs, "person near the forklift")
    assert ranked[0][0] == "b"
    assert "d" not in [i for i, _ in ranked]
    assert all(s > 0 and math.isfinite(s) for _, s in ranked)


def test_cosine_rank_orders_by_cosine_and_skips_null_rows():
    ranked = rr.cosine_rank({"a": [2, 0], "b": [0, 1], "c": [1, 1], "d": None}, [3, 0])
    assert [i for i, _ in ranked] == ["a", "c", "b"]
    assert [round(s, 4) for _, s in ranked] == [1.0, round(1 / math.sqrt(2), 4), 0.0]


def test_rrf_sums_reciprocal_ranks():
    ranked = rr.rrf([[("a", 9.0), ("b", 1.0)], [("b", 0.5), ("c", 0.1)]])
    assert ranked == [("b", 1 / 62 + 1 / 61), ("a", 1 / 61), ("c", 1 / 62)]


def test_run_writes_scorable_jsonl(tmp_path, monkeypatch):
    index = tmp_path / "index.duckdb"
    con = duckdb.connect(str(index))
    con.execute("CREATE TABLE clips(clip_id TEXT, scene_key TEXT, workspace TEXT, cam_id TEXT, t0_ms BIGINT, "
                "t1_ms BIGINT, caption TEXT, v_txt FLOAT[256], v_vis FLOAT[256])")
    rows = [("c1", "wh-a", "warehouse", "cam1", 0, 5000, "a forklift turns left", [1.0] + [0.0] * 255, None),
            ("c2", "wh-a", "warehouse", "cam2", 5000, 10000, "a person waits", [0.0, 1.0] + [0.0] * 254, None),
            ("c3", "fl-b", "fleet", "front", 0, 5000, "a forklift on the road", [1.0] + [0.0] * 255, None)]
    con.executemany("INSERT INTO clips VALUES (?,?,?,?,?,?,?,?,?)", rows)
    con.close()
    queries = [{"qid": "q1", "domain": "warehouse", "text": "forklift", "kind": "no_answer", "relevant": []},
               {"qid": "q2", "domain": "fleet", "text": "person", "kind": "no_answer", "relevant": []}]
    calls = []
    monkeypatch.setattr(rr.embed, "embed_text", lambda q: calls.append(q) or [1.0] + [0.0] * 255)
    stats = rr.run(queries, str(index), ["bm25", "txt", "vis", "rrf:bm25+txt"], 50, tmp_path / "out")
    assert [s[0] for s in stats] == ["bm25", "txt", "vis", "rrf:bm25+txt"]
    assert calls == ["forklift", "person"]
    bm25 = {r["qid"]: r["ranked"] for r in read_jsonl(tmp_path / "out/bm25.jsonl")}
    assert bm25["q1"] == [{"scene_key": "wh-a", "cam_id": "cam1", "t0_ms": 0, "t1_ms": 5000, "score": bm25["q1"][0]["score"]}]
    assert bm25["q2"] == []
    txt = {r["qid"]: r["ranked"] for r in read_jsonl(tmp_path / "out/txt.jsonl")}
    assert [h["cam_id"] for h in txt["q1"]] == ["cam1", "cam2"] and [h["cam_id"] for h in txt["q2"]] == ["front"]
    assert all(r["ranked"] == [] for r in read_jsonl(tmp_path / "out/vis.jsonl"))
    fused = {r["qid"]: r["ranked"] for r in read_jsonl(tmp_path / "out/rrf_bm25+txt.jsonl")}
    assert fused["q1"][0]["score"] == pytest.approx(2 / 61)
    validate(queries, {"bm25": bm25, "txt": txt, "rrf": fused})
    json.dumps(fused, allow_nan=False)
