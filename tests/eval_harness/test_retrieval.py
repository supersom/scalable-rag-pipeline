import asyncio

import pytest

from eval.harness.datasets import QAItem
from eval.harness.retrieval import (
    RetrievalResult, arm_counts, calibrate_threshold, hit_at_k, reciprocal_rank, run_retrieval,
    source_recall, summarize_retrieval,
)


def qa(id, origin="hand", label="source", sources=("a.pdf",), contexts=(), calib=(), answerable=True):
    return QAItem(id=id, query="q", origin=origin, answerable=answerable,
                  label_type=label if answerable else None,
                  expected_sources=tuple(sources) if label == "source" and answerable else (),
                  reference_contexts=tuple(contexts), calibration_sources=tuple(calib),
                  ground_truth=None, tags=())


class FakeRecorder:
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def last(self):
        return self.value


def chunks(rows):
    return [{"filename": f, "page": 1, "score": s, "text": t} for f, s, t in rows]


def retriever(recorder, rows, documents):
    async def fn(state):
        recorder.value = chunks(rows)
        return {"documents": documents}
    return fn


def scored(item_id, rows):
    ranked = chunks(rows)
    return RetrievalResult(item_id=item_id, verdict="scored", ranked=ranked, documents_n=len(ranked),
                           vector_docs=len(ranked), top1_score=ranked[0]["score"] if ranked else None, latency_ms=5.0)


def test_arm_counts_separates_vector_and_graph_documents():
    docs = ["text [Source: a.pdf]", "Kubernetes RELATES_TO Pods", "multi\nline [Source: b.pdf]"]
    assert arm_counts(docs) == (2, 1)
    assert arm_counts([]) == (0, 0)


def test_rank_metrics():
    names = ["x.pdf", "a.pdf", "y.pdf", "b.pdf"]
    expected = {"a.pdf", "b.pdf"}
    assert [hit_at_k(names, expected, k) for k in (1, 3, 5)] == [False, True, True]
    assert reciprocal_rank(names, expected) == 0.5
    assert reciprocal_rank(["x.pdf"], expected) == 0.0
    assert source_recall(names, expected, k=3) == 0.5
    assert source_recall(names, expected, k=5) == 1.0


def test_run_retrieval_scores_item_and_counts_arms():
    rec = FakeRecorder()
    fn = retriever(rec, [("a.pdf", 0.9, "t"), ("x.pdf", 0.4, "t")], ["t [Source: a.pdf]", "A REL B"])
    [result] = asyncio.run(run_retrieval([qa("s1")], fn, rec, {"a.pdf"}))
    assert result.verdict == "scored" and result.top1_score == 0.9
    assert (result.documents_n, result.vector_docs, result.graph_docs) == (2, 1, 1)
    assert [c["filename"] for c in result.ranked] == ["a.pdf", "x.pdf"]


def test_run_retrieval_marks_missing_sources_unscorable_without_searching():
    async def must_not_run(state):
        raise AssertionError("retriever should not be called")

    [result] = asyncio.run(run_retrieval([qa("s1", sources=("a.pdf", "gone.pdf"))], must_not_run, FakeRecorder(), {"a.pdf"}))
    assert result.verdict == "unscorable" and "gone.pdf" in result.reason


def test_run_retrieval_records_errors():
    async def boom(state):
        raise RuntimeError("qdrant down")

    [result] = asyncio.run(run_retrieval([qa("s1")], boom, FakeRecorder(), {"a.pdf"}))
    assert result.verdict == "error" and "RuntimeError: qdrant down" in result.reason


REFERENCE = "Horizontal Pod Autoscaler adjusts replicas"


def dual_item():
    return qa("f1", "golden", "fuzzy", contexts=[REFERENCE], calib=["hpa.html"])


def test_calibrate_threshold_prefers_highest_threshold_on_ties():
    result = scored("f1", [("hpa.html", 0.9, "The horizontal pod autoscaler adjusts the number of replicas"),
                           ("x.pdf", 0.5, "assembly language registers")])
    assert calibrate_threshold([dual_item()], [result]) == (0.9, 1.0, 1)


def test_calibrate_threshold_finds_boundary():
    result = scored("f1", [("hpa.html", 0.9, "horizontal pod autoscaler"), ("x.pdf", 0.5, "assembly language registers")])
    threshold, agreement, n = calibrate_threshold([dual_item()], [result])
    assert threshold == 0.6 and agreement == 1.0 and n == 1


def test_calibrate_threshold_without_dual_items():
    plain = qa("f2", "golden", "fuzzy", contexts=[REFERENCE])
    assert calibrate_threshold([plain], [scored("f2", [("a.pdf", 0.9, "t")])]) == (0.5, None, 0)


def summary_inputs():
    items = [
        qa("s1"), qa("s2", sources=("b.pdf",)), qa("s3", "synthetic", sources=("c.pdf",)),
        qa("f1", "golden", "fuzzy", contexts=[REFERENCE]), qa("u1", answerable=False, label=None),
    ]
    results = [
        scored("s1", [("a.pdf", 0.9, "t"), ("x.pdf", 0.5, "t")]),
        scored("s2", [("x.pdf", 0.8, "t"), ("y.pdf", 0.7, "t"), ("b.pdf", 0.6, "t")]),
        RetrievalResult(item_id="s3", verdict="unscorable", reason="sources not ingested: ['c.pdf']"),
        scored("f1", [("k8s.html", 0.7, "horizontal pod autoscaler adjusts replicas")]),
        scored("u1", [("x.pdf", 0.4, "t")]),
    ]
    return items, results


def test_summarize_retrieval():
    items, results = summary_inputs()
    s = summarize_retrieval(items, results, 0.5, 0.9, 3)
    assert s["headline"] == {"source_hit@1": {"successes": 1, "n": 2}, "source_hit@3": {"successes": 2, "n": 2},
                             "source_hit@5": {"successes": 2, "n": 2}}
    hand = s["groups"]["source/hand"]
    assert hand["n"] == 2 and hand["hit@1"] == 0.5 and hand["hit@3"] == 1.0
    assert hand["mrr"] == pytest.approx((1 + 1 / 3) / 2) and hand["source_recall@5"] == 1.0
    assert "source/synthetic" not in s["groups"] and s["unscorable"] == ["s3"]
    assert s["groups"]["fuzzy/golden"]["hit@1"] == 1.0
    assert s["groups"]["unanswerable/hand"]["mean_top1_score"] == 0.4
    assert s["fuzzy"] == {"n": 1, "threshold": 0.5, "agreement": 0.9, "n_calibration": 3, "reliable": True}
    assert s["unanswerable"] == {"n_answerable": 3, "n_unanswerable": 1, "auroc": 1.0, "cutoff": 0.7,
                                 "descriptive_only": True}
    assert s["empty_context_rate"] == 0.0 and s["arms"] == {"vector_docs": 7, "graph_docs": 0}
    assert s["latency_ms"] == {"p50": 5.0, "p95": 5.0} and s["errors"] == 0 and s["invalid"] is False


def test_summarize_retrieval_flags_low_agreement_as_unreliable():
    items, results = summary_inputs()
    assert summarize_retrieval(items, results, 0.5, 0.5, 3)["fuzzy"]["reliable"] is False
    assert summarize_retrieval(items, results, 0.5, None, 0)["fuzzy"]["reliable"] is False
