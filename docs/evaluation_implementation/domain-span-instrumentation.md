# Domain span instrumentation

`domain_span` is the single developer-facing interface for recording an operation to OpenTelemetry and to the evaluation evidence plane. A call site opens a span, records schema-checked facts and optional content candidates, and supplies provenance through the span. The API adapter projects accepted records into the existing run collector and payload capture. OpenTelemetry receives only fields explicitly marked safe for traces.

## Add a new operation

Define and register a schema in the feature that owns the operation. Shared tracing does not need a new method for each new node or tool:

```python
from libs.observability.operation_contracts import (
    FieldRule, OperationSchema, PhaseRule, register_operation_schema,
)

register_operation_schema(OperationSchema("agent.review", 1, {
    "result": PhaseRule({
        "verdict": FieldRule(str, frozenset({"otel_safe", "evidence_only"}), max_length=16),
    }, frozenset({"rationale"})),
}))
```

Then use the common span interface:

```python
from libs.observability.tracing import domain_span

with domain_span("agent.review", schema_version=1) as operation:
    operation.record(
        "result",
        facts={"verdict": "pass"},
        content={
            "rationale": operation.content(private_rationale, provenance=None),
        },
    )
```

Schemas are exact and versioned. `record` rejects undeclared phases, facts, content names, incorrect scalar types, and values beyond the configured bounds. Rejected records do not reach either sink; the span gets a fixed `evidence.capture_failed` marker. Records are dispatched even when OpenTelemetry sampling drops the span.

## Classify data and provenance

Facts declare where they may go:

- `otel_safe`: non-sensitive operational values that may be added to an OTel span event.
- `evidence_only`: values retained in the run record; string values are pseudonymized in safe exports.
- `derived_digest`: digests retained as pseudonyms rather than copied as raw identifiers.

Do not put prompts, answers, document text, tool arguments/results, exception messages, or payload references in facts or OTel attributes. Put candidate payloads in `content`; the adapter asks the existing capture policy whether their bytes may be retained. Content is not sent to OTel. With capture disabled, content is discarded after synchronous observers return.

Use `operation.provenance.source(value)` for input/history text, `.template(value, template_id=...)` for a known prompt template, `.proof(kind, value, **identity)` for a manifest-backed source, `.combine(...)` for derived content, and `.remember(value, proof)` when later operations need the derivation. Provenance is evidence, not approval by itself: capture still checks the local synthetic fixture policy. An absent or rejected proof results in an explicit unavailable payload slot rather than a guessed approval.

## API evidence and lifecycle

API schemas live in `services/api/app/evaluation/operation_schemas.py`; `bind_instrumentation` binds the run-local adapter while the chat generator advances and resets its context in `finally`. The adapter writes generic versioned operation events and preserves specialized retrieval, model-call, and tool-trajectory fields and their existing payload slots. Generic content uses the `operation_content` slot kind.

Keep tracing and evidence as separate projections of the same accepted record. OTel sampling and exporter outages do not gate evidence collection. The run collector remains the in-memory evidence boundary; durable database/outbox admission remains the existing terminal path. Do not add database, object-store, queue, or model-judge I/O to `record` or to a request span.

Tests: `tests/observability/test_reviewer_schema_example.py` is a minimal external-schema example. The contract, adapter, parity, and streaming lifecycle tests live beside it under `tests/observability/` and `tests/evaluation_delivery/`.
