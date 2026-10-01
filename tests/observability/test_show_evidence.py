"""The evidence viewer summarizes an archived envelope without needing AWS."""

from scripts.show_evidence import one_line, summarize

ENVELOPE = {
    "run_id": "11111111-1111-1111-1111-111111111111", "completeness": "metadata_only",
    "safe_run": {
        "status": "error", "error_class": "ValueError", "trace_id": "abc",
        "operations": {"latency_ms": 1234.6, "input_tokens": 10, "output_tokens": 5, "cache_hit": False},
        "configuration": {"generation_model": "m", "embedding_model": "e"},
        "retrieval": [{"collection": "rag_collection", "vector_status": "success", "graph_status": "error", "item_count": 3}],
        "operation_events": [{"name": "planner"}, {"name": "responder"}], "unavailable": ["input"],
    },
}


def test_summary_extracts_the_fields_operators_look_for():
    summary = summarize(ENVELOPE)
    assert summary["latency_ms"] == 1235
    assert summary["retrieval"] == [("rag_collection", "success", "error", 3)]
    assert summary["operations"] == ["planner", "responder"]
    line = one_line(summary)
    assert "error=ValueError" in line and "cache=miss" in line


def test_summary_tolerates_a_sparse_envelope():
    sparse = {"run_id": "r", "safe_run": {}}
    assert summarize(sparse)["status"] is None
    assert "r " in one_line(summarize(sparse))
