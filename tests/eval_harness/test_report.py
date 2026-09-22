import json

import pytest

from eval.harness.datasets import QAItem, RoutingItem
from eval.harness.report import (
    ComparabilityError, check_comparable, compare_headline, failure_rows, load_baseline, render_summary_md,
    save_baseline, write_run,
)
from eval.harness.retrieval import RetrievalResult, summarize_retrieval
from eval.harness.routing import RoutingResult, summarize_routing

MANIFEST = {"layer": "routing", "started_at": "2026-09-21T10:00:00+00:00", "model": "m", "endpoint_host": "h",
            "embed_model": "nomic", "embed_dim": 768, "git_sha": "abcdef1234567890", "git_dirty": False,
            "dataset_hashes": {"routing.jsonl": "0123456789abcdef"}, "corpus_hash": None, "qdrant_points": None,
            "neo4j_nodes": None, "planner_prompt_hash": "p"}


def test_compare_headline_flags_noise_and_skips_missing_metrics():
    current = {"pass_rate": {"successes": 9, "n": 10}, "new_metric": {"successes": 1, "n": 1}, "empty": {"successes": 0, "n": 0}}
    baseline = {"pass_rate": {"successes": 8, "n": 10}, "empty": {"successes": 0, "n": 0}}
    [row] = compare_headline(current, baseline)
    assert row["metric"] == "pass_rate" and row["delta"] == pytest.approx(0.1) and row["within_noise"] is True
    [big] = compare_headline({"pass_rate": {"successes": 95, "n": 100}}, {"pass_rate": {"successes": 60, "n": 100}})
    assert big["within_noise"] is False


def test_check_comparable():
    assert check_comparable(MANIFEST, dict(MANIFEST, git_sha="other")) == []
    other = dict(MANIFEST, model="different")
    with pytest.raises(ComparabilityError, match="model"):
        check_comparable(MANIFEST, other)
    assert check_comparable(MANIFEST, other, force=True) == ["model"]


def test_baseline_round_trip(tmp_path):
    path = tmp_path / "baselines" / "routing.json"
    save_baseline(path, MANIFEST, {"headline": {"pass_rate": {"successes": 1, "n": 2}}, "other": 1})
    assert load_baseline(path) == {"manifest": MANIFEST, "headline": {"pass_rate": {"successes": 1, "n": 2}}}


def routing_data():
    items = [RoutingItem("a", "hi", "direct_answer", None, ("greeting",), (), None),
             RoutingItem("b", "3*7", "tool_use", "calculator", ("arithmetic",), (), "21"),
             RoutingItem("c", "sqrt(4)", "tool_use", "calculator", ("arithmetic",), (), "2")]
    results = [RoutingResult("a", 0, "pass", "direct_answer", raw_verdict="pass", latency_ms=5.0),
               RoutingResult("b", 0, "wrong_tool", "tool_use", "web_search", raw_verdict="wrong_tool",
                             tool_check="skipped: routed elsewhere", latency_ms=7.0),
               RoutingResult("c", 0, "pass", "tool_use", "calculator", raw_verdict="pass", tool_check="fail", latency_ms=9.0)]
    return items, results


def test_failure_rows_for_routing():
    items, results = routing_data()
    rows = [vars(r) for r in results]
    failures = failure_rows("routing", items, rows)
    assert [f["id"] for f in failures] == ["b", "c"]
    assert failures[0]["expected"] == "tool_use/calculator" and failures[0]["actual"] == "tool_use/web_search"
    assert failures[0]["reason"] == "wrong_tool" and failures[1]["reason"] == "tool_check_failed"
    assert len(failure_rows("routing", items, rows, limit=1)) == 1


def qa_item(id, sources=("a.pdf",), label="source"):
    return QAItem(id=id, query="q", origin="hand", answerable=True, label_type=label,
                  expected_sources=tuple(sources) if label == "source" else (),
                  reference_contexts=("ctx",) if label == "fuzzy" else (), calibration_sources=(),
                  ground_truth=None, tags=())


def retrieval_data():
    items = [qa_item("s1"), qa_item("s2", ("b.pdf",)), qa_item("s3", ("c.pdf",)), qa_item("f1", label="fuzzy")]

    def scored(item_id, names):
        ranked = [{"filename": n, "page": 1, "score": 0.9 - i / 10, "text": "t"} for i, n in enumerate(names)]
        return RetrievalResult(item_id=item_id, verdict="scored", ranked=ranked, documents_n=len(ranked),
                               vector_docs=len(ranked), top1_score=ranked[0]["score"], latency_ms=5.0)

    results = [scored("s1", ["x.pdf", "y.pdf"]), scored("s2", ["b.pdf"]),
               RetrievalResult(item_id="s3", verdict="unscorable", reason="sources not ingested: ['c.pdf']"),
               scored("f1", ["k.html"])]
    return items, results


def test_failure_rows_for_retrieval():
    items, results = retrieval_data()
    failures = failure_rows("retrieval", items, [vars(r) for r in results])
    assert [(f["id"], f["reason"].split(":")[0]) for f in failures] == [("s1", "miss@5"), ("s3", "unscorable")]
    assert failures[0]["actual"] == "x.pdf, y.pdf"


def test_render_routing_summary():
    items, results = routing_data()
    summary = summarize_routing(items, results)
    failures = failure_rows("routing", items, [vars(r) for r in results])
    md = render_summary_md("routing", MANIFEST, summary, ["a warning"], failures)
    for expected in ["# Eval run: routing", "| pass_rate | 66.7% |", "## By tag", "## Safeguards", "## Confusion matrix",
                     "## Tool execution", "## Failures", "a warning", "abcdef1234"]:
        assert expected in md
    assert "INVALID RUN" not in md and "## Comparison with baseline" not in md


def test_render_marks_invalid_runs_and_includes_comparison():
    items, results = routing_data()
    summary = dict(summarize_routing(items, results), invalid=True)
    comparison = [{"metric": "pass_rate", "baseline": 0.5, "current": 0.75, "delta": 0.25, "within_noise": True}]
    md = render_summary_md("routing", MANIFEST, summary, [], [], comparison)
    assert "INVALID RUN" in md and "## Comparison with baseline" in md and "+25.0 pts" in md and "within noise" in md


def test_render_retrieval_summary():
    items, results = retrieval_data()
    summary = summarize_retrieval(items, results, 0.5, None, 0)
    md = render_summary_md("retrieval", dict(MANIFEST, layer="retrieval"), summary, [], [])
    for expected in ["# Eval run: retrieval", "| source_hit@5 |", "## Groups", "source/hand", "## Fuzzy",
                     "uncalibrated", "## Unanswerable separation", "## Arms"]:
        assert expected in md


def test_write_run_creates_all_files(tmp_path):
    out = tmp_path / "run"
    write_run(out, MANIFEST, [{"item_id": "a", "verdict": "pass"}, {"item_id": "b", "verdict": "wrong_tool"}],
              {"headline": {}}, "# md")
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "results.jsonl", "summary.json", "summary.md"]
    lines = (out / "results.jsonl").read_text().splitlines()
    assert [json.loads(line)["item_id"] for line in lines] == ["a", "b"]
    assert json.loads((out / "manifest.json").read_text())["model"] == "m"
