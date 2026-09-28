"""API-owned rich operation schemas; the shared tracing API stays generic."""

from libs.observability.operation_contracts import (
    FieldRule, OperationSchema, PhaseRule, register_operation_schema,
)


def _text(*destinations, length=128):
    return FieldRule(str, frozenset(destinations), max_length=length)


def _number(kind, *destinations):
    return FieldRule(kind, frozenset(destinations))


def register_api_operation_schemas() -> None:
    """Idempotently install the API's known operation shapes at startup."""
    for schema in (
        OperationSchema("llm.chat", 1, {
            "request": PhaseRule({
                "purpose": _text("evidence_only", length=32),
                "request_hash": _text("derived_digest", length=71),
                "requested_model": _text("evidence_only"),
                "temperature": _number(float, "evidence_only"),
                "max_tokens": _number(int, "evidence_only"),
            }, frozenset({"request"})),
            "result": PhaseRule({
                "model": _text("evidence_only"),
                "input_tokens": _number(int, "evidence_only"),
                "output_tokens": _number(int, "evidence_only"),
                "cached_tokens": _number(int, "evidence_only"),
                "finish_reason": _text("evidence_only", length=64),
                "provider_request_id": _text("evidence_only"),
                "completion_id": _text("evidence_only"),
                "status": _text("otel_safe", "evidence_only", length=16),
                "error_type": _text("evidence_only", length=64),
                "retry_count": _number(int, "otel_safe", "evidence_only"),
                "latency_ms": _number(float, "evidence_only"),
            }, frozenset({"response"})),
        }),
        OperationSchema("retrieval", 1, {
            "request": PhaseRule({
                "query_hash": _text("derived_digest", length=71),
                "collection": _text("evidence_only"),
            }, frozenset({"query"})),
            "hit": PhaseRule({
                "retrieval_source": _text("evidence_only", length=16),
                "document_id": _text("evidence_only"),
                "document_version": _text("evidence_only"),
                "chunk_id": _text("evidence_only"),
                "rank": _number(int, "evidence_only"),
                "score": _number(float, "evidence_only"),
                "content_hash": _text("derived_digest", length=71),
            }, frozenset({"chunk"})),
            "result": PhaseRule({
                "collection": _text("evidence_only"),
                "vector_status": _text("otel_safe", "evidence_only", length=16),
                "graph_status": _text("otel_safe", "evidence_only", length=16),
                "graph_error_type": _text("evidence_only", length=64),
            }, frozenset()),
        }),
        OperationSchema("tool.execute", 1, {
            "request": PhaseRule({
                "tool_name": _text("evidence_only", length=64),
                "registered": _number(bool, "evidence_only"),
                "arguments_hash": _text("derived_digest", length=71),
            }, frozenset({"arguments"})),
            "result": PhaseRule({
                "result_hash": _text("derived_digest", length=71),
                "status": _text("otel_safe", "evidence_only", length=16),
                "error_type": _text("evidence_only", length=64),
                "retry_count": _number(int, "evidence_only"),
                "latency_ms": _number(float, "evidence_only"),
            }, frozenset({"result"})),
        }),
    ):
        register_operation_schema(schema)
