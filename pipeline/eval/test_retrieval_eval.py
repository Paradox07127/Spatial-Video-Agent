"""Method-independent retrieval scoring on synthetic labels and ranked runs."""
import json
import math
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest


def moment(t0=0, t1=1000, grade=2, cam="a", scene="s"):
    return {"scene_key": scene, "cam_id": cam, "t0_ms": t0, "t1_ms": t1, "grade": grade}


def hit(t0=0, t1=1000, score=1, cam="a", scene="s"):
    return {"scene_key": scene, "cam_id": cam, "t0_ms": t0, "t1_ms": t1, "score": score}


def query(qid="q", relevant=None, domain="fleet", kind="known_item"):
    return {"qid": qid, "domain": domain, "text": "Find the event", "kind": kind,
            "relevant": [moment()] if relevant is None else relevant, "notes": ""}


def evaluate(queries, runs, **kw):
    from pipeline.eval.retrieval_eval import evaluate as score
    return score(queries, runs, **kw)


def test_uncapped_recall_with_seven_moments():
    labels = [moment(i * 1000, (i + 1) * 1000) for i in range(7)]
    ranked = [hit(i * 1000, (i + 1) * 1000) for i in range(5)]
    m = evaluate([query(relevant=labels, kind="multi_answer")], {"r": {"q": ranked}})["runs"]["r"]["overall"]
    assert [m[f"recall@{k}"] for k in (1, 5, 10, 20)] == pytest.approx([1 / 7, 5 / 7, 5 / 7, 5 / 7])


def test_iou_threshold_is_inclusive():
    q = query(relevant=[moment(0, 2000)])
    runs = {"edge": {"q": [hit()]}, "below": {"q": [hit(0, 999)]}}
    r = evaluate([q], runs)["runs"]
    assert r["edge"]["overall"]["recall@1"] == 1
    assert r["below"]["overall"]["recall@1"] == 0


def test_camera_labels_collapse_with_max_grade():
    q = query(relevant=[moment(grade=1), moment(cam="b")])
    runs = {"r": {"q": [hit(cam="c")]}}
    m = evaluate([q], runs)["runs"]["r"]["overall"]
    assert m["recall@1"] == m["ndcg@10"] == m["success@5"] == 1
    m = evaluate([q], {"r": {"q": [hit()]}}, match="camera")["runs"]["r"]["overall"]
    assert m["recall@1"] == 0.5
    assert m["success@5"] == 0
    assert evaluate([q], runs, match="camera")["runs"]["r"]["overall"]["recall@1"] == 0


def test_hit_uses_only_the_best_iou_moment():
    q = query(relevant=[moment(0, 2000, grade=1), moment(0, 1000)], kind="multi_answer")
    m = evaluate([q], {"r": {"q": [hit(), hit()]}})["runs"]["r"]["overall"]
    assert m["recall@10"] == 0.5
    assert m["success@5"] == 1


def test_duplicate_hits_count_only_at_first_rank():
    m = evaluate([query()], {"r": {"q": [hit(), hit(cam="b")]}})["runs"]["r"]["overall"]
    assert m["recall@10"] == m["ndcg@10"] == m["mrr"] == 1


def test_ndcg_uses_label_grades_and_original_ranks():
    q = query(relevant=[moment(grade=1), moment(2000, 3000)], kind="multi_answer")
    ranked = [hit(), hit(scene="other"), hit(2000, 3000)]
    m = evaluate([q], {"r": {"q": ranked}})["runs"]["r"]["overall"]
    assert m["ndcg@10"] == pytest.approx(2 / (2 + 1 / math.log2(3)))
    assert m["success@5"] == 1


def test_mrr_uses_first_partial_or_clear_match_beyond_cutoffs():
    ranked = [hit(scene="other")] * 20 + [hit()]
    m = evaluate([query(relevant=[moment(grade=1)])], {"r": {"q": ranked}})["runs"]["r"]["overall"]
    assert m["mrr"] == pytest.approx(1 / 21)
    assert m["recall@20"] == m["ndcg@10"] == m["success@5"] == 0


def test_auc_counts_ties_and_reports_no_answer_scores():
    qs = [query("a"), query("b"), query("c", [], kind="no_answer"), query("d", [], kind="no_answer")]
    runs = {"r": {qid: [hit(score=s)] for qid, s in zip("abcd", (0.9, 0.1, 0.5, 0.1))}}
    m = evaluate(qs, runs)["runs"]["r"]["overall"]
    assert m["auroc"] == 0.625
    assert m["no_answer_top1_mean"] == pytest.approx(0.3)
    assert m["n_answerable"] == m["n_no_answer"] == 2
    assert m["recall@1"] == 1


def test_missing_queries_are_empty_and_domain_means_are_macro():
    qs = [query("a"), query("b"), query("c", domain="street")]
    r = evaluate(qs, {"r": {"a": [hit()], "c": [hit()]}})["runs"]["r"]
    assert r["overall"]["recall@10"] == pytest.approx(2 / 3)
    assert r["per_domain"]["fleet"]["recall@10"] == 0.5
    assert r["per_domain"]["street"]["recall@10"] == 1
    assert r["per_query"]["b"]["mrr"] == 0


def test_empty_results_have_no_score_and_sort_below_negative_scores():
    qs = [query("a"), query("b", [], kind="no_answer")]
    m = evaluate(qs, {"r": {"a": [hit(score=-2)]}})["runs"]["r"]["overall"]
    assert m["auroc"] == 1
    assert m["no_answer_top1_mean"] is None
    assert m["n_no_answer_scored"] == 0
    assert m["n_no_answer"] == 1
    m = evaluate(qs, {"r": {}})["runs"]["r"]["overall"]
    assert m["auroc"] == 0.5


def test_metrics_are_null_when_population_is_absent():
    m = evaluate([query("a", [], kind="no_answer")], {"r": {}})["runs"]["r"]["overall"]
    assert m["recall@10"] is m["mrr"] is m["ndcg@10"] is m["auroc"] is None
    assert m["n_answerable"] == 0


def test_paired_bootstrap_constant_difference_and_fixed_seed():
    qs = [query(str(i)) for i in range(4)] + [query("empty", [], kind="no_answer")]
    runs = {"base": {}, "new": {str(i): [hit()] for i in range(4)}}
    r = evaluate(qs, runs, baseline="base")
    assert r["bootstrap"] == evaluate(qs, runs, baseline="base")["bootstrap"]
    assert r["bootstrap_samples"] == 1000 and r["seed"] == 20261011
    for metric in ("recall@10", "ndcg@10", "mrr"):
        assert r["bootstrap"]["new"]["overall"][metric] == {"mean_difference": 1.0, "ci95": [1.0, 1.0]}
        assert r["bootstrap"]["base"]["overall"][metric] == {"mean_difference": 0.0, "ci95": [0.0, 0.0]}
        assert r["bootstrap"]["new"]["per_domain"]["fleet"][metric]["ci95"] == [1.0, 1.0]


def test_paired_bootstrap_uses_same_query_indices():
    qs = [query(str(i)) for i in range(8)]
    ranked = {str(i): [hit()] if i % 2 else [] for i in range(8)}
    r = evaluate(qs, {"base": ranked, "new": ranked}, baseline="base")
    assert r["bootstrap"]["new"]["overall"]["recall@10"]["ci95"] == [0.0, 0.0]


def test_cli_multiple_runs_markdown_and_full_json():
    with TemporaryDirectory(dir=Path(__file__).parent) as d:
        p = Path(d)
        (p / "q.jsonl").write_text(json.dumps(query()) + "\n" + json.dumps(query("empty", [], domain="street", kind="no_answer")) + "\n")
        (p / "base.jsonl").write_text(json.dumps({"qid": "q", "ranked": []}) + "\n")
        (p / "new.jsonl").write_text(json.dumps({"qid": "q", "ranked": [hit()]}) + "\n")
        cmd = [sys.executable, "pipeline/eval/retrieval_eval.py", "--queries", str(p / "q.jsonl"),
               "--run", f"base={p / 'base.jsonl'}", "--run", f"new={p / 'new.jsonl'}", "--baseline", "base",
               "--match", "camera", "--tiou", "0.5", "--out", str(p / "result.json")]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        assert proc.returncode == 0, proc.stderr
        assert "| Run |" in proc.stdout and "| new | overall |" in proc.stdout
        assert "| new | street |" in proc.stdout and "mean" in proc.stdout.lower()
        r = json.loads((p / "result.json").read_text())
        assert r["match"] == "camera" and r["tiou"] == 0.5
        assert r["runs"]["new"]["overall"]["recall@10"] == 1
        assert r["bootstrap"]["new"]["overall"]["mrr"]["ci95"] == [1.0, 1.0]
        assert r["bootstrap"]["new"]["per_domain"]["street"]["mrr"]["ci95"] is None
