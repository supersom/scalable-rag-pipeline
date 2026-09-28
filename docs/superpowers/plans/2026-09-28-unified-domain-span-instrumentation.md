# Unified Domain-Span Instrumentation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `domain_span` the one developer-facing operation interface for safe tracing and evaluation evidence, including new schema-registered operation kinds.

**Architecture:** Extend the existing `ObservedSpan` with a synchronous, schema-validated `record` method and an opaque content/provenance façade. Keep OTel and evidence as separate projections of accepted records; adapt the API's existing collector and capture session behind a request-local observer. Add versioned generic operation events to evidence while retaining the specialized fields and durable delivery path.

**Tech Stack:** Python 3, Pydantic, OpenTelemetry SDK, FastAPI, pytest, PostgreSQL, S3-compatible storage, SQS-compatible delivery.

**Spec:** [Unified domain-span instrumentation design](../specs/2026-09-28-unified-domain-span-instrumentation-design.md)

## Global Constraints

- Pre-refactor implementation baseline: `a2666a1e1d13a6b6a84f06a92ff24da05cdc82e5`; design-spec commit: `b97390e`.
- Preserve current `domain_span(name, *, attributes=None)` and `set_attribute` behavior for existing callers.
- No raw prompt, answer, document, tool result, exception message, or payload reference may enter OTel attributes/events.
- OTel sampling/export failure must not suppress run evidence. Evidence admission remains fail-open and nonblocking; PostgreSQL commit remains its durability boundary.
- Content capture remains restricted to approved local synthetic fixtures. `EVAL_CAPTURE_CONTENT` alone never authorizes a payload.
- Existing specialized evidence fields, model invocation ordinals, payload slots and object keys, and `None`-versus-zero semantics remain stable. New generic content uses one new slot kind.
- Existing 1.0 export envelopes remain readable. New operation events use an explicit schema version; do not rewrite prior records.
- No production deployment, new cloud permission, new scorer, or new content-approval policy is part of this plan.
- Commit tests and implementation together per task; do not land intentionally red commits. Preserve the four unrelated untracked documentation files.

## File structure

| Responsibility | Files |
| --- | --- |
| Shared schema, bounded records, opaque content handles | New `libs/observability/operation_contracts.py` |
| Existing public scope, operation IDs, safe OTel projection, request-local observer ports | `libs/observability/tracing.py` |
| API schema registration and specialized projection | New `services/api/app/evaluation/operation_schemas.py`, `instrumentation_adapter.py` |
| In-memory run evidence and synthetic-fixture provenance | `services/api/app/evaluation/context.py`, `capture.py`, `models.py` |
| Safe export, generic payload slot, deterministic key validation | `services/api/app/evaluation/delivery_models.py`, `metadata_export.py`, `payloads.py` |
| Adapter lifecycle and call-site migration | `services/api/main.py`, `services/api/app/routes/chat.py`, `services/api/app/agents/nodes/{planner,retriever,responder,tool}.py`, `services/api/app/clients/{ray_llm,ray_embed,qdrant,neo4j}.py`, plus cache/memory callers that use `domain_span` |
| Tests and developer recipe | `tests/observability/`, `tests/evaluation_delivery/`, `docs/evaluation_implementation/` |

## Review Focus

1. An unsampled span with a structured record must still produce evidence: Task 2's unsampled observer test and Task 4's adapter test.
2. A malformed schema field or raw content passed as a fact must not leak to OTel or durable evidence: Tasks 1, 2, and 4 test rejection and a safe error counter.
3. Two concurrent model calls completing in reverse order must keep request-time ordinals and payload references matched: Task 5's model parity test.
4. A client disconnect during a streamed answer must finalize once, free content buffers, and keep the run ID: Task 7's ASGI regression test.
5. A 1.0 envelope and an operation-content S3 reference must remain independently readable and tamper-checked: Task 3's contract and storage tests.

---

### Task 1: Define a bounded, versioned operation-record contract

**Files:**
- Create: `libs/observability/operation_contracts.py`
- Test: `tests/observability/test_operation_contracts.py`

**Interfaces:**
- Produces: `FieldRule(value_type, destinations, max_length=None)`, `PhaseRule(facts, content_names)`, `OperationSchema(name, version, phases)`, `register_operation_schema(schema)`, `get_operation_schema(name, version)`, `validate_record(schema, phase, facts, content_names)`.
- Produces: frozen `OperationRecord(operation_id, parent_operation_id, name, schema_version, phase, sequence, timestamp, facts)` and `ContentCandidate(value, provenance)`; neither serializes candidate bytes into metadata.

- [ ] **Step 1: Write failing contract tests.** Include registration conflict, unknown phase/field, wrong scalar type, overlong strings, nonfinite floats, and an approved `otel_safe` field alongside an `evidence_only` field:

```python
schema = OperationSchema("agent.review", 1, {
    "result": PhaseRule({
        "verdict": FieldRule(str, frozenset({"otel_safe", "evidence_only"}), max_length=32),
        "rubric_id": FieldRule(str, frozenset({"evidence_only"}), max_length=64),
    }, frozenset({"rationale"}))
})
register_operation_schema(schema)
assert validate_record(schema, "result", {"verdict": "pass"}, ()) == {"verdict": "pass"}
with pytest.raises(ValueError):
    validate_record(schema, "result", {"unreviewed_text": "secret"}, ())
```

- [ ] **Step 2: Run** `pytest tests/observability/test_operation_contracts.py -q`; expect missing-contract failures.
- [ ] **Step 3: Implement immutable rules, exact-name registration, and validation.** Reject bool-as-int, nonfinite numbers, mutable nested values, and duplicate registration with a different schema. Bound field count and string length before copying. Keep raw content only in `ContentCandidate`, not `OperationRecord`.
- [ ] **Step 4: Run** the focused test and `pytest tests/observability/test_domain_spans.py -q`; both pass.
- [ ] **Step 5: Commit** the two files with `feat: define structured operation contracts`.

### Task 2: Evolve `domain_span` without changing legacy behavior

**Files:**
- Modify: `libs/observability/tracing.py`
- Test: `tests/observability/test_domain_spans.py`, new `tests/observability/test_operation_records.py`

**Interfaces:**
- Consumes Task 1 contracts.
- Produces `domain_span(name, *, attributes=None, schema_version=None)` and `ObservedSpan.record(phase, *, facts=None, content=None) -> None`.
- Produces `record_observer: ContextVar[Callable[[OperationRecord, Mapping[str, ContentCandidate]], None] | None]` and `provenance_port: ContextVar[object | None]`. `ObservedSpan.content(value, *, provenance)` returns a `ContentCandidate`; `ObservedSpan.provenance` delegates to the bound port and is inert when absent.

- [ ] **Step 1: Write failing tests** for the same legacy attributes/status, parent-child operation IDs, two phase records in sequence, a suppressed OTel span that still calls the record observer, and a schema-invalid record that never calls either sink:

```python
seen = []
token = record_observer.set(lambda record, content: seen.append(record))
try:
    with domain_span("agent.review", schema_version=1) as op:
        op.record("result", facts={"verdict": "pass"})
finally:
    record_observer.reset(token)
assert [event.phase for event in seen] == ["result"]
assert seen[0].operation_id
```

- [ ] **Step 2: Run** `pytest tests/observability/test_operation_records.py -q`; expect missing-method failures.
- [ ] **Step 3: Implement record dispatch and safe OTel projection.** Allocate operation IDs independently of span sampling; resolve parent from `_active_operation`; validate before observer calls; add only fields classified `otel_safe` to the SDK span event; never attach `content` or a storage reference. Preserve the current final `operation_observer(name, attributes)` callback exactly once. Convert observer failures to a fixed-key `evidence.capture_failed` marker and safe warning.
- [ ] **Step 4: Run** `pytest tests/observability/test_domain_spans.py tests/observability/test_operation_records.py -q`; both pass.
- [ ] **Step 5: Commit** these changes with `feat: record structured events through domain spans`.

### Task 3: Add versioned generic evidence and one generic payload slot

**Files:**
- Modify: `services/api/app/evaluation/models.py`, `delivery_models.py`, `metadata_export.py`, `payloads.py`, `context.py`
- Test: `tests/evaluation_delivery/test_contracts.py`, `test_metadata_export.py`, `test_payloads.py`; `tests/observability/test_evidence_contract.py`

**Interfaces:**
- Produces `RunEvidence.operation_events: list[OperationEvidence]` and `SafeRun.operation_events: tuple[SafeOperationEvent, ...]` with operation/parent IDs, name/version/phase/sequence/status/timing, safe facts, and content-name-to-slot mapping.
- Produces `RunEvidenceCollector.record_event(event: OperationEvidence) -> None`; existing `record_retrieval`, `record_model_call`, and `record_tool_step` remain until migration is complete.
- Adds `PayloadKind.operation_content`, using `PayloadSlot(kind="operation_content", ordinal=<per-run operation ordinal>, item_ordinal=<candidate ordinal>)` and an event mapping from schema content name to slot. Existing kinds and paths remain unchanged.

- [ ] **Step 1: Write failing contract tests** for a reviewer event and its content slot, canonical hash stability, duplicate/invalid slot rejection, 1.0 envelope reading, and an S3 put/get round trip with a generic slot:

```python
slot = PayloadSlot(kind="operation_content", ordinal=1, item_ordinal=1)
assert slot.kind.value == "operation_content"
assert slot.model_dump(mode="json")["ordinal"] == 1
assert PayloadSlot(kind="model_request", ordinal=1).kind.value == "model_request"
```

- [ ] **Step 2: Run** `pytest tests/evaluation_delivery/test_contracts.py tests/evaluation_delivery/test_metadata_export.py tests/evaluation_delivery/test_payloads.py -q`; expect the new cases to fail.
- [ ] **Step 3: Implement the 1.1 run/export contract with a reader for stored 1.0 envelopes.** Ensure `project_metadata` pseudonymizes evidence-only identifiers and excludes unapproved fields; do not copy arbitrary facts into `SafeRun`. Keep v1.0 hash verification based on its original bytes. Extend only the deterministic `_slot_path`/reference checks required for `operation_content`.
- [ ] **Step 4: Run** the focused suites plus `pytest tests/evaluation_delivery/test_outbox.py tests/evaluation_delivery/test_delivery_worker.py -q`; all pass.
- [ ] **Step 5: Commit** with `feat: preserve versioned operation evidence and content slots`.

### Task 4: Bind the API evidence adapter and register current schemas

**Files:**
- Create: `services/api/app/evaluation/operation_schemas.py`, `instrumentation_adapter.py`
- Modify: `services/api/app/evaluation/capture.py`, `services/api/main.py`
- Test: new `tests/observability/test_instrumentation_adapter.py`, `tests/observability/test_lifespan.py`

**Interfaces:**
- Produces `bind_instrumentation(collector: RunEvidenceCollector, capture: CaptureSession | None)` context manager; it binds Task 2 observer/provenance ports and resets them in `finally`.
- Produces `register_api_operation_schemas()` for `retrieval`, `llm.chat`, `tool.execute`, and API-specific rich events. It is idempotent across test application creation.
- Adapter uses existing capture `source`, `proof`, `combine`, `remember`, `record`, and `omit` methods. It maps known schema phases to specialized collector methods and all accepted records to `record_event`.

- [ ] **Step 1: Write failing tests** showing an unsampled `llm.chat` event updates both generic and specialized evidence, invalid content proof yields an unavailable slot, no raw content enters OTel, and no adapter remains bound after an exception:

```python
with bind_instrumentation(collector, capture):
    with domain_span("agent.review", schema_version=1) as op:
        op.record("result", facts={"verdict": "pass"})
assert collector.event is None  # run is not finalized by a child operation
assert record_observer.get() is None
```

- [ ] **Step 2: Run** `pytest tests/observability/test_instrumentation_adapter.py -q`; expect missing-adapter failures.
- [ ] **Step 3: Implement the adapter and schemas.** Dispatch synchronously with no I/O; allocate generic content slots once per run; maintain model invocation ordinals; call capture policy before retaining bytes. Register schemas at API startup and leave runtime startup/shutdown order intact. Bind the adapter only while advancing the chat generator, matching current context-token handling.
- [ ] **Step 4: Run** focused adapter/lifespan/capture tests; all pass.
- [ ] **Step 5: Commit** with `feat: adapt domain records to API evidence`.

### Task 5: Migrate retrieval and model clients with exact parity

**Files:**
- Modify: `services/api/app/agents/nodes/retriever.py`, `services/api/app/clients/ray_llm.py`, `services/api/app/evaluation/operation_schemas.py`, `instrumentation_adapter.py`
- Test: `tests/observability/test_retrieval_evidence.py`, `test_model_metadata.py`; `tests/evaluation_delivery/test_capture_provenance.py`

**Interfaces:**
- Consumes `ObservedSpan.record` and `ObservedSpan.provenance` from Task 2, the API adapter from Task 4.
- Produces retrieval/model evidence through the adapter, with no direct `current_collector`, `current_capture`, `PayloadSlot`, or `ModelCall` import in these two call sites.

- [ ] **Step 1: Add parity tests** for vector success/graph partial failure, ranked duplicate hits and filename proof, request/response payloads, usage `None` versus zero, retries, and two model calls completed in reverse order:

```python
assert [call.request_hash for call in event.model_calls] == expected_request_hashes
assert [call.retry_count for call in event.model_calls] == expected_retry_counts
assert [item.rank for item in event.retrieval[0].items] == expected_ranks
```

- [ ] **Step 2: Run** the focused suites before migration; record the passing baseline.
- [ ] **Step 3: Replace `_record_evidence` and model-client direct capture with `op.record` phases.** Record retrieval before flattening. Record model request at invocation and result/error in `finally`; keep the existing typed `LLMResult` and string-returning compatibility wrapper. The adapter preserves old specialized fields and payload slots.
- [ ] **Step 4: Run** the focused suites plus `pytest tests/observability -q`; compare exact expected evidence/manifest values.
- [ ] **Step 5: Commit** with `refactor: use domain records for retrieval and model calls`.

### Task 6: Migrate tools, planner, responder, and simple dependencies

**Files:**
- Modify: `services/api/app/agents/nodes/planner.py`, `responder.py`, `tool.py`; `services/api/app/clients/ray_embed.py`, `qdrant.py`, `neo4j.py`; `services/api/app/evaluation/operation_schemas.py`, `instrumentation_adapter.py`
- Test: `tests/observability/test_agent_tool_spans.py`, `test_agent_evidence.py`, `test_evidence_observations.py`; `tests/evaluation_delivery/test_capture_provenance.py`

**Interfaces:**
- Consumes the same `domain_span`/`record` and provenance façade. Simple dependency spans keep `set_attribute` unless they carry rich evidence.
- Produces the current tool trajectory and source-approved planner/responder content without direct evaluation imports from agent nodes or clients.

- [ ] **Step 1: Add parity tests** for invalid/disabled tools, calculator argument/result approval, denied tool output, planner fallback, retrieved-context prompt proof, and cache-derived output omission:

```python
assert event.trajectory[0].tool_name == "calculator"
assert event.trajectory[0].arguments_hash == expected_hash
assert all("secret" not in str(span.attributes) for span in exported_spans)
```

- [ ] **Step 2: Run** the focused suites to establish the passing baseline.
- [ ] **Step 3: Move tool step and approved-content capture to `op.record`.** Use `op.provenance.source/template/combine/remember` in planner and responder; do not infer approval from a final prompt string. Keep existing dependency span names, outcome attributes, and swallowed-failure classification. Do not add `record` to simple backend spans that have no rich facts.
- [ ] **Step 4: Run** the focused suites and full `pytest tests/observability tests/evaluation_delivery -q`; all pass.
- [ ] **Step 5: Commit** with `refactor: instrument agent and tool operations through domain spans`.

### Task 7: Migrate streaming run binding and verify lifecycle

**Files:**
- Modify: `services/api/app/routes/chat.py`, `services/api/app/evaluation/instrumentation_adapter.py`
- Test: `tests/observability/test_chat_tracing.py`, `test_evidence_delivery_lifecycle.py`; `tests/evaluation_delivery/test_delivery_acceptance.py`

**Interfaces:**
- Consumes `bind_instrumentation(collector, capture)`; keeps `RunStreamingResponse.evidence` and current `run_id`/NDJSON contract.
- Produces exactly one terminal run snapshot and one evidence admission per accepted request.

- [ ] **Step 1: Add ASGI tests** for disconnect during headers, disconnect after first body chunk, cache hit, ordinary answer, propagated timeout, and repeated `on_close` invocation:

```python
assert response.headers["X-Run-ID"] == emitted_events[0]["run_id"]
assert terminal_collector.event.status == "cancelled"
assert admissions_for_run == 1
```

- [ ] **Step 2: Run** the focused suites to establish the passing baseline.
- [ ] **Step 3: Replace direct `bind_collector`/`bind_capture` calls with `bind_instrumentation` only around generator advancement and close.** Keep root `agent.run` span start/end, first-output marking, output hashing, and synchronous `evidence.admit` behavior. Do not hold context tokens across `yield`.
- [ ] **Step 4: Run** observability and delivery lifecycle suites, then `pytest -q`; all pass subject only to existing opt-in live-test skips.
- [ ] **Step 5: Commit** with `refactor: bind unified instrumentation for streamed runs`.

### Task 8: Acceptance, new-operation recipe, and final review

**Files:**
- Create: `tests/observability/test_reviewer_schema_example.py`
- Modify: `docs/evaluation_implementation/gap-01-production-run-instrumentation.md`
- Create: `docs/evaluation_implementation/domain-span-instrumentation.md`

**Interfaces:**
- Demonstrates a reviewer schema registered outside `libs/observability` and used through unchanged `domain_span`/`record` signatures.

- [ ] **Step 1: Add a reviewer example test** whose schema lives in the test feature module, records verdict and a denied rationale, then asserts a generic operation event, no new core method, and no content in OTel:

```python
with domain_span("agent.review", schema_version=1) as op:
    op.record("result", facts={"verdict": "pass"},
              content={"rationale": op.content("private", provenance=None)})
assert event.operation_events[-1].name == "agent.review"
assert "private" not in exported_trace_text
```

- [ ] **Step 2: Run** the example test; it must pass without changing `tracing.py` for the reviewer kind.
- [ ] **Step 3: Document the developer recipe**: define/register schema, open a `domain_span`, call `record`, classify facts, provide provenance, interpret unavailable payloads, and configure OTel/evidence with existing settings. Update the Gap 1 progress document with the refactor boundary, tests, and remaining production-enablement limits.
- [ ] **Step 4: Run** `pytest -q`, the focused live synthetic-fixture acceptance tests when the local stack is available, `python -m compileall libs services/api`, and `git diff --check`. Record exact counts and any opt-in skips; do not describe Moto as proving IAM/KMS enforcement.
- [ ] **Step 5: Commit** with `docs: explain unified domain-span instrumentation` after the tests and documentation are green.

## Execution and review

Implement in task order, with a green commit after each task. Before removing direct evaluation imports, compare old and new evidence snapshots and payload manifests on the same fixtures. Review the final diff for unapproved content reaching OTel, lost fixed fields, changed outbox bytes/hashes, and request-path I/O. The pre-refactor implementation remains identifiable at `a2666a1e1d13a6b6a84f06a92ff24da05cdc82e5`; switching to a different instrumentation design later is a separate change, not a destructive reset of subsequent work.
