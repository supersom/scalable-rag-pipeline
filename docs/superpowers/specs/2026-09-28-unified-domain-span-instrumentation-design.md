# Unified domain-span instrumentation

**Status:** Interface direction approved in conversation; written spec pending user review. Implementation not started.

**Decision:** Evolve the existing `domain_span` interface rather than add named `observe.retrieval`, `observe.model_call`, and similar helpers. A new operation kind adds a versioned schema and registration, not a method in the shared library.

**Pre-refactor baseline:** `a2666a1e1d13a6b6a84f06a92ff24da05cdc82e5` on `dev`. This is the divergence point for comparing this design with the earlier named-helper option. Four unrelated untracked documentation files existed when the baseline was recorded and are outside this work.

## Intent and boundaries

New agents, tools, and clients should need one discoverable instrumentation interface for tracing and evaluation evidence. The interface must preserve the current run, retrieval, model-call, tool, dependency, operational, and payload information; the existing privacy and provenance rules; and the fail-open request path. It should work in other services without importing the FastAPI application's evaluation package.

This refactor does not implement production content approval, audit-grade pre-commit durability, new scorers, or a new OTel backend. It does not make the OTel Collector the evidence store. The existing S3, PostgreSQL outbox, and SQS delivery path remains authoritative after commit.

## Public interface

`libs.observability.tracing.domain_span` remains the entry point. Existing calls and `set_attribute` semantics remain valid. Its yielded `ObservedSpan` gains bounded structured recording and provenance facilities:

```python
with domain_span("agent.review", schema_version=1) as op:
    op.record("request", facts={"rubric_id": rubric_id})
    verdict = await reviewer.review(answer)
    op.record(
        "result",
        facts={"verdict": verdict.label, "confidence": verdict.confidence},
        content={"rationale": op.content(verdict.rationale, provenance=rationale_proof)},
    )
```

The same `domain_span(...); op.record(...)` shape applies to retrieval, model calls, tools, and future nodes. `record(phase, *, facts=None, content=None)` is the only structured event method. A `ContentCandidate` holds a value and opaque provenance handle in process; it is never an OTel attribute or serialized into metadata evidence. `set_attribute` remains for small, safe operational attributes. Existing spans that do not call `record` need no registered rich-event schema.

The operation scope allocates an application operation ID and parent ID independently of OTel sampling, records start/end times, and determines success, error, timeout, or cancellation on exit. It also exposes the current trace/span IDs when valid. Calls to `record` are synchronous, bounded, and contain no storage or network I/O. The operation observer receives lifecycle summaries once per scope and structured phase records separately; it must not double-count one operation as two dependency observations.

The streaming chat route remains responsible for creating the run ID, marking first output, hashing the actual answer, and finalizing the run exactly once on success, cache hit, error, timeout, or disconnect. Those are HTTP/stream lifecycle facts, not inferable from a child span. The route should ultimately import the shared instrumentation interface, while API startup owns evidence-adapter registration.

## Schema registry

A rich-event schema is identified by `(operation_name, schema_version)`. It declares allowed phases; fact names, types, bounds, and destination classification (`otel_safe`, `evidence_only`, or `derived_digest`); content slot names and maximum cardinality; and any mapping to an existing specialized evidence record. Registration occurs at service startup, not through request data or `.env`.

The shared library owns the registry protocol and validation, not a method per operation kind. Feature modules own their schemas. For example, a reviewer feature adds `agent.review/v1` and registers it; `tracing.py` and the public method signatures do not change. Existing retrieval, `llm.chat`, and `tool.execute` schemas have adapters that continue to populate the current specialized evidence fields. A new kind without a specialized mapper is retained in generic operation events.

Unknown names remain valid for ordinary operational spans. An attempted rich record with an unknown schema, phase, or field is rejected from both rich projections, counted as a bounded instrumentation error, and represented as unavailable detail in evidence when a run collector exists. It must not leak fields through an unchecked fallback. Instrumentation errors do not alter the user response.

## Data flow and evidence contract

One validated, in-process operation record feeds two independently configured projections:

```text
domain_span lifecycle + record(phase, facts, content)
  ├─ OTel projection: sampled safe attributes/events, no raw content or payload refs
  └─ API evidence adapter: unsampled metadata + policy-checked content candidates
       ├─ current specialized RunEvidence fields for existing consumers
       └─ versioned operation_events for all rich operation kinds
            → existing background runtime → S3 payloads + PostgreSQL envelope/outbox → SQS
```

The operation record has `operation_id`, `parent_operation_id`, name, schema version, phase, sequence, timestamp, terminal status and latency where applicable, and validated facts. The evidence contract gains `operation_events` with safe field projections and payload-slot outcomes. This requires an explicit run/export schema-version bump and readers that can still consume existing 1.0 records. Existing retrieval, model-call, trajectory, dependency, and operational fields remain present during migration. They are generated from the same accepted operation records, not from separate call-site writes. Parity tests must prove no current field, ordering rule, or `None`-versus-zero meaning changes.

The current `PayloadKind` enum has only fixed input, retrieval, model, and tool slots. Add one `operation_content` slot kind once, with a per-run ordinal and optional item ordinal; each generic operation event maps its schema-defined content name to that slot. Existing slot kinds and object keys remain unchanged. The S3 store's slot-path and reference validation must recognize the new kind, but future operation names must not require another enum or storage-path edit. A new content shape still requires an explicit policy/redaction review before it can be approved for storage; metadata-only operation kinds need only schema registration.

Model-call ordinals are allocated at invocation, not completion, so concurrent calls retain the existing request-to-payload mapping. Retrieval events retain source, document and chunk identity, rank, score, content hash, partial branch outcomes, and provenance before prompt formatting. Tool events retain argument/result hashes, registration and authorization facts, status, error class, and side-effect metadata when available. Unsupported facts remain explicitly unavailable; the refactor must not invent corpus, ACL, cost, or approval information that the application does not collect.

OTel sampling and exporter failures cannot suppress evidence collection. Evidence admission is still an in-memory, fail-open handoff; only the PostgreSQL transaction marks it committed. The existing upload-intent, redaction, limits, retention, outbox, retry, and quarantine behaviors are unchanged.

## Provenance and content safety

Callers identify how candidate content was made because a final string does not prove its source. The yielded operation exposes a small provenance façade backed by the API's existing capture session: `op.provenance.source(value)`, `template(value, template_id=...)`, `combine(*proofs)`, and `remember(derived_value, proof)`. The shared library treats proofs as opaque handles and does not decide whether they are approved. A tracing-only service gets inert handles and never exports content.

For a prompt assembled from a query, template, and retrieved documents, the call site combines the proofs for those exact components and passes the resulting handle to `op.content(prompt, provenance=proof)`. The API adapter verifies source bytes, tenant/case, manifest, and policy using the current capture implementation, then redacts and enforces payload slots and byte budgets. Missing, invalid, or unapproved proof yields an unavailable payload outcome while preserving safe metadata. A caller cannot assert `approved=True`. Cached answers and unknown tool results remain unapproved under the current policy. The current content policy remains restricted to trusted local synthetic fixtures; this design does not widen it.

## Configuration

`services/api/app/config.py` and `.env` retain the existing settings. `OTEL_*` controls span export and sampling. `EVAL_EVIDENCE_ENABLED` controls durable evidence delivery. `EVAL_CAPTURE_CONTENT`, `EVAL_CONTENT_POLICY`, the trusted fixture manifest, identity key, and existing storage, limit, and retention settings control content capture. The API registers the evidence adapter at startup; other services can use `domain_span` with tracing only or register their own adapter. No per-call evidence switch or environment-controlled schema whitelist is added. Disabled evidence allocates no delivery runtime, while tracing and the current in-process run snapshot behavior remain available.

## Implementation boundaries

- Extend `libs/observability/tracing.py` and add a small adjacent schema/record contract module. Preserve the current OTel provider, sampler, safe error handling, and `domain_span` compatibility.
- Add `services/api/app/evaluation/instrumentation_adapter.py`; reuse `context.py`, `capture.py`, and provenance/redaction code behind it.
- Extend `evaluation/models.py`, `delivery_models.py`, and `metadata_export.py` for safe versioned operation events and the one generic content slot. Update `payloads.py` to validate its deterministic slot path. Keep existing specialized fields, object-key shapes, and envelope integrity checks.
- Register schemas and the adapter in API startup. Migrate `routes/chat.py`, agent nodes, and backend clients in slices so new call sites import only the shared instrumentation surface. Leave the storage runtime, payload store, outbox, publisher, and worker behavior intact.
- Document the developer recipe for a new operation schema and a new reviewer node. Update `.env.evidence` only if configuration changes prove necessary; none are planned.

## Acceptance criteria

1. A new reviewer node can add a schema and use `domain_span`/`record` without editing the shared public interface or evaluation collector methods.
2. Existing callers of `domain_span` behave as before. Unsampled spans still contribute evidence. No raw prompt, answer, document, tool result, exception message, or payload reference reaches OTel.
3. Golden parity tests compare all existing run evidence fields and approved/unavailable payload manifests before and after migration, including request-time model ordinals, retrieval ordering, partial failures, retries, cache hits, stream cancellation, and disabled delivery.
4. The new operation-event contract round-trips and hashes canonically, and old 1.0 envelopes remain readable. A reviewer verdict and approved or unavailable rationale are reconstructable without a reviewer-specific core method.
5. Invalid schemas, facts, proofs, and adapter failures remain bounded and fail-open for the user request, with an observable instrumentation error and no unsafe export.
6. Local tests and the existing synthetic-fixture acceptance stack exercise the full capture-to-outbox path. No cloud deployment or GPU model is required for this refactor's local verification; production controls remain separate work.
