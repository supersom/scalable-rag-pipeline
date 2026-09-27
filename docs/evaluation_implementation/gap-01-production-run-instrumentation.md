# Gap 1 implementation: production run instrumentation

**Status:** In progress — the local development foundation is committed; PR 1 trace plumbing and run identity are implemented and verified locally. PRs 2–5 remain.

**Scope:** Close Gap 1 from the [evaluation-plane gap analysis](../evaluation-plane-gap-analysis.md#1-instrument-every-production-run-1)

**Outcome:** Every cached, successful, failed, cancelled, and timed-out agent request emits a durable, redacted, versioned record that can be joined to its OpenTelemetry trace and used for replay or audit.

Local development and verification use the [evaluation instrumentation environment](local-environment.md).

## Implementation progress

### Completed: PR 0 local development foundation

As of 2026-09-26, the repository can develop and verify Gap 1 without deploying to AWS or loading the production GPU models:

- [x] Added an isolated `scalable-rag-eval` Docker Compose project.
- [x] Added deterministic, OpenAI-compatible fake chat and embedding endpoints.
- [x] Added a local, versioned `eval-traces` bucket using Moto's S3-compatible service.
- [x] Added a local OpenTelemetry Collector with OTLP gRPC and HTTP receivers.
- [x] Added a containerized API using repository-pinned dependencies.
- [x] Added PostgreSQL, Redis, Qdrant, and Neo4j dependencies on alternate host ports.
- [x] Bound every published development port to `127.0.0.1`.
- [x] Added `make local-up`, `make local-down`, `make local-check`, and `make local-smoke` workflows.
- [x] Added contract tests plus an authenticated, end-to-end streaming smoke test.
- [x] Documented startup, configuration, fake-model behavior, and local-versus-staging boundaries in [local-environment.md](local-environment.md).

Verified on 2026-09-26:

| Check | Result |
|---|---|
| `make local-check` | 18 passed, 2 skipped |
| `pytest -q` | 158 passed, 2 skipped |
| `make local-smoke` | 4 passed |
| Python compilation | Clean |
| Git whitespace validation | Clean |
| Running services | 8 healthy/running, all host ports loopback-only |

The skipped tests in the first two commands are the opt-in live-stack and OTLP export tests, which `make local-smoke` runs explicitly. `pytest.ini` limits default collection to `tests/`, so a bare `pytest -q` does not mistake `scripts/load_test.py` for a correctness test. Locust remains an optional, separately pinned development dependency in `requirements-loadtest.txt`.

This foundation validates application contracts and failure paths locally. It deliberately does not emulate AWS IAM, KMS, IRSA, managed queue authentication, EKS networking, multi-replica shutdown, or production load. Those remain staging and production-readiness checks.

### Implemented locally: PR 1 trace plumbing and run identity

As of 2026-09-27:

- [x] Initialize the process tracer provider and FastAPI instrumentation once during application creation.
- [x] Export through a bounded background OTLP/gRPC batch processor when an endpoint is configured; keep console export opt-in.
- [x] Configure service name, version, environment, sampler, sampling ratio, and per-RPC export timeout.
- [x] Flush and shut down tracing during application lifespan cleanup, including failed startup.
- [x] Assign `run_id` before cache lookup, return it in `X-Run-ID`, and include it with `session_id` in every NDJSON event.
- [x] Create an `agent.run` span under the HTTP server span for each accepted chat attempt.
- [x] Finalize success, cache hit, propagated error, timeout, and cancellation paths exactly once.
- [x] Close spans and graph iterators promptly on disconnect, including disconnect during response headers before the first body chunk.
- [x] Keep raw prompts, answers, user IDs, and exception messages out of the domain span. Replace HTTP URL/query fields with route templates and redact captured header values.
- [x] Add the missing FastAPI instrumentation dependency and copy `libs/` into the API image. Pin setuptools because the pinned instrumentation still imports `pkg_resources`.
- [x] Extend the local smoke contract to verify stream/header identifier consistency.

Verification:

| Check | Result |
|---|---|
| `pytest tests/observability -q` | 38 passed |
| Focused tests inside the pinned API image | 38 passed |
| `pytest -q` | 196 passed, 2 skipped |
| `make local-check` | 18 passed, 2 skipped |
| `make local-smoke` | 4 passed |
| Local Collector inspection | API resource, HTTP parent span, and correlated `agent.run` received over OTLP |

The focused tests cover actual ASGI disconnects during headers and body sends, cancellation while awaiting a dependency, concurrent streams, safe HTTP/domain attributes, real OTLP/gRPC delivery, sampling, idempotent setup, and shutdown flush. The ordinary-suite skips remain the two opt-in live-stack checks. Runtime checks used fake models and local dependencies without cloud credentials or a GPU. Existing configuration/logging and pinned upstream packages emit deprecation warnings; these do not fail the checks.

PR 1 traces accepted, authenticated chat requests. Authentication and request-validation rejections receive HTTP instrumentation but do not start an agent run. Sampling can suppress trace export while response run identifiers remain present. Errors swallowed by existing dependency clients or graph nodes are not reclassified here; typed dependency outcomes belong to PR 2. Cache/history failures now return correlated NDJSON errors after the streaming response begins. Cache-hit persistence is awaited inside the run, so its failures and disconnects are covered too.

`OTEL_EXPORTER_OTLP_TIMEOUT` limits each RPC attempt, not total shutdown time. The pinned SDK retries transient exporter failures, so shutdown during a Collector outage may exceed the five-second flush window. Collector-outage, backpressure, and deployment grace-period guarantees remain PR 5 checks. No durable run evidence or payload publication is implemented by this slice.

### Remaining Gap 1 delivery

| Slice | Status | Next outcome |
|---|---|---|
| PR 1: Trace plumbing and run identity | Committed; verified locally | Run identity and root domain spans verified; domain child spans follow in PR 2. |
| PR 2: Domain spans and typed dependency results | Not started | Span trees and retrieval/model metadata become testable. |
| PR 3: Evidence contract and collector | Not started | Runs produce reconstructable, versioned evidence. |
| PR 4: Redacted payloads and durable publication | Not started | Evidence is safely stored and delivered with the selected guarantee. |
| PR 5: Production deployment and controls | Not started | Privacy, durability, load, and operational controls are validated. |

No cloud credentials or model-provider API keys are required for PRs 1–3 local development. The local profile uses dummy AWS credentials, a local JWT secret, fake models, Moto S3, and the local Collector. Cloud-specific credentials and controls become necessary only for staging validation and PRs 4–5.

## Why this gap comes first

The offline evaluation harness can score routing, retrieval, and answer quality, but production requests do not yet preserve enough evidence to reconstruct what happened. Without that evidence, production failures cannot reliably become regression cases, component replay cannot reuse the original context, and release decisions cannot compare a candidate against the behavior users actually saw.

The implementation should keep two concerns separate:

- OpenTelemetry spans explain where time was spent and where a request failed.
- A versioned `RunEvidence` event records the domain evidence needed for evaluation, replay, and audit.

Traces are not the system of record for large prompts, retrieved documents, or tool results. Those payloads belong in encrypted object storage; spans and run events carry identifiers, hashes, metadata, and references.

## Current scaffold

The repository already has useful starting points:

- `services/api/app/observability.py` now attaches FastAPI auto-instrumentation from `services/api/main.py`, sanitizes HTTP metadata, and exposes shutdown cleanup.
- `libs/observability/tracing.py` now provides idempotent provider initialization, OTLP/gRPC export, resource identity, sampling, optional console export, and shutdown flushing.
- `libs/utils/ids.py` defines `generate_trace_id()`, but production request handling does not use it.
- `services/api/app/routes/chat.py` now creates a distinct `run_id`, traces the streaming lifecycle, correlates every event, and persists messages inside the run. Domain child spans and durable evidence remain outstanding.
- `services/api/app/agents/nodes/retriever.py` receives Qdrant hits, but converts them to strings and discards identifiers, ranks, and scores.
- `services/api/app/clients/ray_llm.py` returns only generated text, so model identity, token usage, finish reason, and provider request identifiers are lost.
- `libs/observability/metrics.py` declares operational metrics, but they have no production call sites.

This is enough to avoid starting from scratch, but it is not yet reconstructable production evidence.

## Goals

1. Assign a unique `run_id` to every request attempt while retaining the existing conversation `session_id`.
2. Create one `agent.run` root domain span with child spans for cache, memory, planning, retrieval, model calls, tools, persistence, and streaming.
3. Preserve ranked retrieval evidence before it is flattened into prompt context.
4. Capture tool trajectory, configuration fingerprints, latency, token usage, status, and payload references.
5. Redact sensitive data before it leaves the application process.
6. Publish exactly one terminal run event for every outcome, including cache hits and failures.
7. Keep publication bounded and off the response path unless audit requirements call for a transactional outbox.

## Non-goals

This gap does not implement evaluation workers, scorers, human review, replay execution, dashboards, release gates, or an Opik deployment. It produces the evidence those later components consume.

## Target data flow

```text
client request
    |
    v
FastAPI server span
    |
    +-- agent.run domain span
    |     +-- cache.lookup
    |     +-- memory.load
    |     +-- planner
    |     |     `-- llm.chat
    |     +-- retrieval
    |     |     +-- embedding.create
    |     |     +-- qdrant.search
    |     |     `-- neo4j.search
    |     +-- tool.execute
    |     +-- responder
    |     |     `-- llm.chat
    |     +-- memory.persist
    |     `-- cache.write
    |
    +-- redacted payloads -> encrypted object storage
    +-- spans -> OTLP collector
    `-- run-completed event -> queue or transactional outbox
                                      |
                                      v
                              evaluation plane
```

## Identity model

The implementation must distinguish three identifiers:

| Identifier | Lifetime | Purpose |
|---|---|---|
| `session_id` | Conversation | Groups multiple turns in the same user thread. |
| `run_id` | One request attempt | Joins the response, run evidence, stored payloads, evaluation results, and audit records. |
| `trace_id` | One distributed trace | Joins application work to OpenTelemetry spans across services. |

Generate `run_id` at the start of `chat_stream()`, before cache lookup. Return it on every NDJSON event, including cached responses and errors. Obtain `trace_id` from the active OpenTelemetry span instead of generating a second trace identity.

If the client retries a request, the retry should receive a new `run_id` unless the API later introduces an explicit idempotency key. An idempotency key and a run identifier solve different problems and must not be conflated.

## Versioned evidence contract

Create a small evaluation-evidence package owned by the production API:

```text
services/api/app/evaluation/
├── __init__.py
├── context.py       # request-scoped collector and lifecycle
├── models.py        # versioned event models
├── payloads.py      # encrypted object-store interface
├── publisher.py     # event publication interface
└── redaction.py     # allowlist, hashing, and content redaction
```

The first schema should be deliberately explicit and versioned:

```python
class RunEvidence(BaseModel):
    schema_version: Literal["1.0"]
    run_id: str
    trace_id: str
    session_id: str

    started_at: datetime
    completed_at: datetime
    status: Literal["success", "error", "cancelled", "timeout"]
    error_type: str | None = None

    tenant_id: str | None
    user_id_hash: str
    user_role: str | None
    use_case: str | None

    input_ref: str | None
    output_ref: str | None
    retrieval: RetrievalEvidence | None
    trajectory: list[ToolStep]
    configuration: ConfigurationFingerprint
    operations: OperationalEvidence
```

Nested models should cover these fields:

### Retrieval evidence

- Original and rewritten query references or hashes.
- Ordered document and chunk identifiers.
- Immutable document version identifiers where available.
- Rank and raw score for each result.
- Retrieval source, such as Qdrant or Neo4j.
- Collection name and index or corpus snapshot.
- ACL filter and ACL policy version.
- Reranker identity and pre/post-rerank positions when reranking exists.
- Retrieval latency and result count.

### Tool trajectory

- Monotonic step number.
- Tool name and tool-schema version.
- Redacted argument and result references.
- Authorization or approval outcome.
- Start time, latency, status, retry count, and error type.
- Side-effect or idempotency key when the tool can mutate state.

### Configuration fingerprint

- Agent build or commit SHA.
- Prompt hash and prompt-template version.
- Generation model and embedding model identifiers.
- Chunking, index, reranker, tool-schema, policy, and ACL versions.
- Relevant feature flags and graph version.

### Operational evidence

- End-to-end latency.
- Time to first response token or first streamed answer event.
- Input, output, and cached token counts when available.
- Estimated cost and the price-table version used to compute it.
- Agent step count, retry count, and cache-hit status.
- Provider throttling, cancellation, and timeout indicators.

Do not make fields appear populated when the runtime cannot produce them. A missing value should be `null` with a documented reason or surfaced by a completeness check, not replaced with a guess.

## Request-scoped evidence collector

Introduce a mutable, request-scoped `RunEvidenceCollector` that accumulates typed evidence while the graph runs. It should expose narrow append/update methods rather than allowing arbitrary dictionary mutation:

```python
collector.record_retrieval(...)
collector.record_model_call(...)
collector.record_tool_step(...)
collector.mark_first_output(...)
collector.finish(status="success")
```

Pass the collector through LangGraph's `configurable` context alongside `llm` and `user_id`. This keeps evaluation evidence out of `AgentState`, whose fields affect graph behavior and checkpoints. Nodes may record observations, but they must not make routing decisions based on evaluation state.

The collector should be append-only for repeated operations and safe for concurrent retrieval branches. It should calculate terminal timings once and reject a second finalization attempt.

## Span boundaries and attributes

FastAPI auto-instrumentation should remain in place for HTTP server spans. Add an explicit `agent.run` span inside `chat_stream()` so domain work remains stable even if route names or web instrumentation change.

| Span | Primary implementation point | Safe attributes |
|---|---|---|
| `agent.run` | `services/api/app/routes/chat.py` | `run_id`, hashed tenant/user identifiers, use case, final status, cache hit |
| `cache.lookup` | `chat.py` or semantic-cache client | hit/miss, latency, cache namespace/version |
| `memory.load` | `chat.py` or memory client | message count, latency; no message text |
| `planner` | planner node | action, selected tool, latency |
| `llm.chat` | `RayLLMClient.chat_completion()` | model, temperature, token counts, finish reason, provider status |
| `retrieval` | retriever node | result count, index snapshot, total latency |
| `embedding.create` | embedding client | model, dimensions, latency |
| `qdrant.search` | Qdrant client | collection, limit, latency, result count |
| `neo4j.search` | Neo4j client or retriever node | query-template version, latency, result count |
| `tool.execute` | tool node | tool name, schema version, authorization outcome, latency, status |
| `responder` | responder node | response type and latency |
| `memory.persist` | route or memory client | message count, latency, status |
| `cache.write` | route or cache client | write/skip reason, latency, status |

Span attributes must stay small and low-cardinality where practical. Never attach raw prompts, user messages, document bodies, embeddings, tool results, access tokens, or full generated answers. Use object references, hashes, byte counts, and stable identifiers.

## Preserve retrieval evidence

`retrieve_node()` currently turns Qdrant hits into strings and then deduplicates them with a `set`. That loses rank, score, record identity, and deterministic ordering.

Refactor retrieval in two stages:

1. Normalize each backend result into a typed internal record containing content plus evidence metadata.
2. Produce prompt-ready strings from those records only after evidence has been recorded.

The normalized record should resemble:

```python
class RetrievedItem(BaseModel):
    retrieval_source: Literal["qdrant", "neo4j"]
    document_id: str | None
    document_version: str | None
    chunk_id: str | None
    content: str
    content_hash: str
    score: float | None
    rank: int
    metadata: dict[str, Any]
```

Deduplication must be deterministic and should use a stable key such as `(document_id, chunk_id, content_hash)`. Preserve the best score and original rank when duplicates appear. Store only the redacted content reference in `RunEvidence`; the graph may still receive content in memory for generation.

## Capture model-call metadata

Change the internal LLM client boundary so callers can access provider metadata without parsing it repeatedly. Prefer a typed result:

```python
class LLMResult(BaseModel):
    text: str
    model: str
    input_tokens: int | None
    output_tokens: int | None
    cached_tokens: int | None
    finish_reason: str | None
    provider_request_id: str | None
```

If changing every caller at once is too disruptive, add a temporary `chat_completion_result()` method and keep `chat_completion()` as a compatibility wrapper that returns `.text`. Migrate planner, responder, query rewriting, and HyDE callers before removing the wrapper.

The local vLLM-compatible service currently returns only message content. Extend that response to include the model, finish reason, and token usage, or record those fields as unavailable until the serving layer exposes them. Do not estimate tokens in the API process and label them as provider usage.

## Payload storage and redaction

Large or sensitive content should follow this path:

```text
raw request data
    -> classification and redaction
    -> encrypted object storage
    -> opaque reference in RunEvidence
```

Recommended object-key structure:

```text
eval-traces/{environment}/{yyyy}/{mm}/{dd}/{run_id}/input.json
eval-traces/{environment}/{yyyy}/{mm}/{dd}/{run_id}/output.json
eval-traces/{environment}/{yyyy}/{mm}/{dd}/{run_id}/retrieval.json
eval-traces/{environment}/{yyyy}/{mm}/{dd}/{run_id}/tools/{step}.json
```

Required controls:

- Server-side encryption with a dedicated KMS key.
- Separate IAM roles for production writers, evaluation readers, and sensitive-trace reviewers.
- Shorter retention for raw content than for redacted metadata.
- Tenant and environment partitioning.
- An allowlist of fields that may enter spans and run events.
- Hashing or tokenization for user identifiers.
- Redaction before both object storage and telemetry export.
- A deletion path that can remove tenant/user content while retaining non-identifying aggregate metrics.

The payload writer should return an opaque URI or key. Downstream workers should resolve it through an authorized storage client, not assume a public S3 URL.

## Event publication

Define a small publisher protocol so durability can evolve without changing request instrumentation:

```python
class RunEvidencePublisher(Protocol):
    async def publish(self, event: RunEvidence) -> None: ...
```

Implementations can be introduced in stages:

1. `LoggingPublisher` for local development and contract tests.
2. `QueuePublisher` for best-effort or bounded-retry publication to Kafka or SQS.
3. `OutboxPublisher` when the run record is an audit record that must not be lost.

Use best-effort publication only when losing some evaluation samples is acceptable. Bound retries and time spent publishing so an unavailable evaluation system does not hold open the user stream.

Use a transactional outbox when evidence is part of an audit obligation:

1. Commit the terminal run record and outbox row in PostgreSQL.
2. A separate publisher reads pending rows and sends them to the queue.
3. Mark a row delivered only after broker acknowledgement.
4. Retry safely using `run_id` as the event idempotency key.

Regardless of transport, consumers must deduplicate by `(schema_version, run_id)`.

## Finalization semantics

Exactly one terminal event should be attempted for every path:

| Request path | Terminal status | Required evidence |
|---|---|---|
| Cache hit | `success` | Cache hit, latency, input/output refs, configuration, zero graph steps |
| Normal graph completion | `success` | Retrieval, trajectory, model calls, output, persistence status |
| Unhandled exception | `error` | Error type, completed steps, partial evidence, no sensitive error message |
| Client disconnect | `cancelled` | Cancellation point, completed steps, elapsed time |
| Provider or application timeout | `timeout` | Timed-out dependency/span, retry count, elapsed time |

Place finalization in the streaming generator's `try`/`except`/`finally` lifecycle. A response is not finished when `chat_stream()` returns a `StreamingResponse`; it is finished when the generator completes or is cancelled.

If payload storage or event publication fails, annotate the run span and increment an operational metric. Do not silently convert an evidence failure into a successful evidence record.

## OpenTelemetry setup

Replace the console-only production path with an OTLP exporter aimed at an OpenTelemetry Collector. Keep the console exporter available behind a local-development setting.

Initialization should be:

- Called exactly once when `services/api/main.py` creates the FastAPI application.
- Idempotent in tests and local reload mode.
- Configured with resource attributes such as service name, environment, version, and deployment identity.
- Controlled through environment-based sampling and exporter settings.
- Flushed during FastAPI lifespan shutdown.

The production flow should be:

```text
FastAPI pod -> OTLP -> OpenTelemetry Collector -> trace backend and/or Opik
```

The application should not need separate tracing code for every downstream backend. The Collector owns batching, retries, routing, and backend-specific exporters.

## Configuration

Add typed settings for the instrumentation feature. Final names can follow the repository's environment-variable conventions, but the configuration needs to cover:

| Setting | Purpose | Safe default |
|---|---|---|
| `OTEL_SERVICE_NAME` | Trace service identity | `rag-api-service` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Collector endpoint | unset locally |
| `OTEL_TRACES_SAMPLER` | Trace sampling policy | environment-specific |
| `EVAL_EVIDENCE_ENABLED` | Master switch for run events | `false` until storage is configured |
| `EVAL_EVIDENCE_BUCKET` | Large-payload destination | no default |
| `EVAL_EVENT_PUBLISH_MODE` | `log`, `queue`, or `outbox` | `log` locally |
| `EVAL_EVENT_QUEUE_URL` | Queue destination | no default |
| `EVAL_CAPTURE_CONTENT` | Permit redacted content storage | `false` |
| `EVAL_RAW_RETENTION_DAYS` | Raw-payload lifecycle | policy-defined |
| `EVAL_METADATA_RETENTION_DAYS` | Metadata lifecycle | policy-defined |

Fail startup when evidence is enabled but required destinations or encryption settings are missing. A misconfigured production pod should not pretend it is producing durable evidence.

## File-by-file implementation map

| File or area | Change |
|---|---|
| `services/api/app/evaluation/models.py` | Add versioned evidence, retrieval, trajectory, configuration, and operations models. |
| `services/api/app/evaluation/context.py` | Add the request-scoped collector, timers, concurrency safety, and one-time finalization. |
| `services/api/app/evaluation/redaction.py` | Add allowlisted serialization, identifier hashing, and content redaction. |
| `services/api/app/evaluation/payloads.py` | Add local/no-op and encrypted S3 payload writers. |
| `services/api/app/evaluation/publisher.py` | Add publisher protocol and logging/queue/outbox implementations. |
| `services/api/app/config.py` | Add typed OpenTelemetry and evidence settings with validation. |
| `libs/observability/tracing.py` | Add OTLP export, resource attributes, sampling, idempotent initialization, and shutdown. |
| `services/api/app/observability.py` | Wire FastAPI instrumentation and expose tracer shutdown. |
| `services/api/main.py` | Initialize instrumentation once and flush it during lifespan shutdown. |
| `services/api/app/routes/chat.py` | Generate `run_id`, create `agent.run`, pass the collector, include `run_id` in events, and finalize every path. |
| `services/api/app/clients/ray_llm.py` | Return typed model output and usage metadata; add `llm.chat` spans. |
| `services/api/app/clients/qdrant.py` | Add search spans and preserve collection/result metadata. |
| `services/api/app/agents/nodes/retriever.py` | Normalize ranked results, record evidence, and keep deterministic ordering. |
| `services/api/app/agents/nodes/tool.py` | Record authorized tool calls, arguments/results by reference, latency, retries, and status. |
| planner/responder/enhancer nodes | Record prompt version, model-call purpose, and output metadata without raw content in spans. |
| Helm/deployment configuration | Add Collector endpoint, evidence destinations, IAM, KMS, retention, and sampling settings. |

## Delivery sequence

Split the work into reviewable changes. Each pull request should leave the service runnable and add focused tests.

### PR 0: Local development foundation — complete

- [x] Run the API and its data dependencies in an isolated Compose project.
- [x] Provide deterministic fake chat and embedding services that match the application clients.
- [x] Provide local versioned object storage and an OpenTelemetry Collector.
- [x] Add configuration, contract tests, a live smoke test, and operator documentation.

**Exit condition met:** The complete local stack starts without cloud credentials or GPU models, and an authenticated streaming request succeeds through the containerized API.

### PR 1: Trace plumbing and run identity

- [x] Initialize OpenTelemetry from `services/api/main.py`.
- [x] Add OTLP export, resource attributes, sampling, and shutdown flush.
- [x] Generate `run_id` and return it in every stream event.
- [x] Create the `agent.run` span and cover cache-hit, success, error, cancellation, and timeout paths.

**Exit condition met locally:** Each accepted chat attempt has a `run_id` and a root domain span in integration tests, including requests disconnected before the first body chunk. Sampling controls whether the trace is exported. Implementation and tests are committed together in reviewable tracing-provider, API-lifecycle, and local-integration slices.

### PR 2: Domain spans and typed dependency results

- Add cache, memory, planner, retrieval, embedding, vector, graph, model, tool, persistence, and cache-write spans.
- Introduce typed LLM results without breaking callers.
- Normalize retrieval records and preserve rank, score, and stable identifiers.

**Exit condition:** One integration test can assert the expected span tree and metadata without inspecting logs.

### PR 3: Evidence contract and collector

- Add `RunEvidence` schema version `1.0`.
- Add the request-scoped collector and pass it through LangGraph configuration.
- Record configuration fingerprints, retrieval evidence, trajectory, operations, and partial failures.
- Add fixture-based serialization and backward-compatibility tests.

**Exit condition:** A test can reconstruct the important decisions and dependencies of a run from the event and referenced fixtures.

### PR 4: Redacted payloads and durable publication

- Add redaction and encrypted object storage.
- Add queue publication with bounded retries and a dead-letter queue, or implement the transactional outbox if evidence is an audit record.
- Add idempotency, duplicate-consumer tests, and publisher failure metrics.

**Exit condition:** Restart and failure-injection tests prove the chosen delivery guarantee.

### PR 5: Production deployment and operating controls

- Deploy or configure the OpenTelemetry Collector.
- Add IAM, KMS, lifecycle, network policy, secrets, dashboards, and alerts.
- Run load tests for latency overhead, payload size, event loss, queue lag, and exporter backpressure.
- Document on-call procedures for collector, queue, object-store, and outbox failures.

**Exit condition:** The production readiness review signs off on privacy, durability, performance, and failure behavior.

## Test plan

### Unit tests

- Schema validation and stable JSON serialization.
- Redaction of secrets, credentials, personal data, and document content.
- Identifier hashing stability and tenant separation.
- Collector concurrency and one-time finalization.
- Deterministic retrieval ordering and deduplication.
- Cost calculation against a versioned price table.
- Publisher retry and idempotency behavior.

### Integration tests

- Cache hit emits a complete terminal event without graph spans.
- Normal retrieval run emits the expected trace hierarchy and ranked evidence.
- Tool run records the selected tool, arguments/result references, and status.
- Provider error preserves partial evidence and emits `error`.
- Timeout emits `timeout`; client disconnect emits `cancelled`.
- Payload-store failure and queue failure produce metrics and span errors without leaking content.
- A consumer can join the run event, payloads, and trace by `run_id` and `trace_id`.

### Security tests

- Cross-tenant readers cannot resolve object references.
- Span and event exports contain no raw authorization headers, JWTs, prompts, embeddings, or document bodies.
- Raw content expires according to lifecycle policy.
- User/tenant deletion removes applicable payloads and identifying metadata.

### Load and resilience tests

- Measure p50, p95, and p99 request overhead with instrumentation enabled.
- Verify bounded memory during Collector or broker outages.
- Verify exporter batching and backpressure behavior.
- Measure event loss for best-effort mode or prove outbox recovery for durable mode.
- Confirm evidence generation does not delay streaming beyond the agreed service-level objective.

## Acceptance criteria

Gap 1 is closed when all of the following are true:

- [x] Every accepted chat request attempt receives a stable, unique `run_id` before cache lookup.
- [x] Every NDJSON response event includes `run_id` and `session_id`.
- [ ] Every run has one `agent.run` span and the expected child spans for work that actually occurred.
- [ ] Every terminal path attempts exactly one versioned `RunEvidence` event.
- [ ] Cache hits, errors, cancellations, and timeouts are represented explicitly.
- [ ] Ranked retrieval evidence retains document/chunk IDs, scores, ranks, source, and index/ACL versions.
- [ ] Model calls retain model identity and provider-reported token metadata when available.
- [ ] Tool calls retain ordered trajectory metadata and redacted payload references.
- [ ] Configuration fingerprints identify the agent, prompts, models, corpus/index, tools, and policies used.
- [ ] Large payloads are encrypted, access-controlled, retention-bound, and referenced rather than embedded.
- [ ] Redaction runs before telemetry or payload export, with regression tests for known sensitive fields.
- [ ] A run event, its payloads, and its trace can be joined using `run_id` and `trace_id`.
- [ ] Publication failure is observable and does not grow request latency or memory without bounds.
- [ ] Load tests show instrumentation overhead and event-loss behavior remain within explicit production SLOs.

## Opik boundary

Opik can be added as a trace and experiment inspection layer after this evidence path exists. The OpenTelemetry Collector can route spans to it, and evaluation workers can publish grader results to it.

Opik should not replace `RunEvidence`, own the only copy of raw production evidence, or become a synchronous dependency of the request path. Git and CI should remain authoritative for dataset definitions, scorer code, baselines, and release policy; durable storage should remain authoritative for replayable production evidence.

## Design decisions to settle before PR 4

Three choices require explicit product, security, and operations agreement:

1. **Delivery guarantee:** best-effort queue publication or transactional outbox.
2. **Content policy:** metadata-only capture, redacted content capture, or a restricted raw tier plus redacted tier.
3. **Retention:** separate durations for raw payloads, redacted metadata, traces, and evaluation results.

These decisions do not block PRs 1 through 3. The interfaces above keep them replaceable while the team establishes the evidence contract and trace coverage.

## References

- [Evaluation-plane gap analysis](../evaluation-plane-gap-analysis.md)
- [Evaluation harness design](../eval-harness-design.md)
- [Evaluation harness implementation plan](../eval-harness-plan.md)
- [OpenTelemetry Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [OpenTelemetry Collector deployment patterns](https://opentelemetry.io/docs/collector/deploy/)
- [OpenTelemetry generative-AI semantic attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
