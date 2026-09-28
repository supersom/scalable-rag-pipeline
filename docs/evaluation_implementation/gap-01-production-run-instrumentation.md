# Gap 1 implementation: production run instrumentation

**Status:** In progress — the local foundation and PRs 1–4 are committed and verified locally. PR 5 (production enablement) remains and is not started.

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

### Implemented locally: PR 2 domain spans and typed dependency results

This slice adds `cache.lookup`, `memory.load`, `planner`, `retrieval`, `embedding.create`, `qdrant.search`, `neo4j.search`, `llm.chat`, `tool.execute`, `responder`, `memory.persist`, and `cache.write` spans. Child spans record elapsed time and explicit operation outcomes; handled graph failures mark retrieval `partial`, disabled tools are `disabled`, and deliberate cache-write omissions are `skipped`. A successful response can therefore coexist with failed dependency spans when a best-effort fallback recovered the request.

`RayLLMClient.chat_completion_result()` returns `LLMResult` with provider model, token usage, cached tokens, finish reason, request header ID, and completion ID. `chat_completion()` remains a string-returning compatibility wrapper. Planner, responder, and graph-search calls use the typed boundary. Missing provider metadata stays `None`, and malformed responses fail explicitly. One `llm.chat` span covers bounded retries and records retry count; it does not export prompts, answers, or exception messages.

`retrieve_context()` normalizes backend hits into `RetrievedItem` records inside a `RetrievalResult`. It retains source, stable chunk/relationship ID, document/version identity where available, raw score, original source rank, content hash, and metadata. Source paths are hashed when used as fallback document identity; a filename alone is not assumed to be a unique document ID. Deduplication preserves deterministic vector-then-graph ordering, the first original rank, and the best duplicate score. Neo4j relationship IDs are valid within the current database, not an immutable cross-snapshot document identity.

PR 2 made structured records available at the dependency boundary; PR 3 now retains them in the run collector before flattening. `AgentState.documents` remains prompt-ready strings, without evaluation data in graph checkpoints. Records are not yet published. Corpus/index snapshots, ACL decisions, token cost, tool approvals/schema versions, and durable payload references are still unavailable and are not invented.

Local tests exercise real graph nodes, real model HTTP clients against the deterministic fake provider, and the real embedded Qdrant backend. Neo4j query execution and PostgreSQL persistence use controlled boundary doubles; live stack smoke checks cover service connectivity and request completion. The wrapper supports both the pinned Qdrant `search` API and newer host clients' `query_points`. Failed retrieval branches cancel and await sibling work before the parent span ends. The graph tool's JSON prompt formatting bug was also fixed under a regression test.

PR 2 verification on 2026-09-27:

| Check | Result |
|---|---|
| Full `pytest -q --tb=short` | 229 passed, 2 existing opt-in skips |
| `pytest tests/observability -q --tb=short` | 71 passed |
| Observability tests in the pinned API image | 71 passed |
| `make local-check` | 18 passed, 2 existing opt-in skips |
| Rebuilt local API stack | All eight services healthy |
| Final `make local-smoke` | 4 passed |
| Local Collector inspection | Child spans, embedding dimensions, and model token usage received over OTLP |

A fresh read-only review found two issues, both fixed with failing-then-passing regression tests: planner tool names must be registry-allowlisted before export, and query/document embedding timeouts remain 5/60 seconds respectively for both pooled and temporary HTTP clients. Existing configuration, logging, and upstream-library deprecation warnings remain non-failing. No cloud deployment, credentials, GPU, scoring workers, or durable evidence publication were needed for these checks.

### Implemented locally: PR 3 evidence contract and collector

The API now builds a schema-version `1.0` metadata record for each accepted run. A request-scoped, lock-protected `RunEvidenceCollector` travels through LangGraph's `configurable` context, not `AgentState`. It appends repeated retrievals, model calls, tool steps, and dependency observations; records terminal timing once; rejects double finalization; and returns detached snapshots. Collection remains independent of OpenTelemetry sampling.

The record joins run/session/trace identity with configuration fingerprints, ranked retrieval identities and scores, query/content hashes, model/provider metadata, tool argument/result hashes, safe dependency outcomes, and operational summaries. Completed retrieval branches survive sibling failure; graph fallback remains a partial outcome. Success, error, timeout, cache hit, and header/body disconnect paths finalize the same contract. A successful final response does not erase failed dependency observations.

No raw prompts, answers, document text, tool arguments/results, user IDs, or exception messages are placed in this record. Optional `EVAL_IDENTITY_HASH_KEY` enables tenant-scoped HMAC user pseudonyms; without it the pseudonym is `null` with an explicit reason. This is a separate local/deployment secret, not a model-provider API key or the JWT signing secret. Hashes are identifiers, not encryption or a substitute for PR 4 privacy controls.

Set `AGENT_BUILD_SHA` from the build/deployment to identify the complete code revision. Known model identifiers, embedding dimensions, graph source, prompt-template source, and tool registry metadata also enter the fingerprint. Without a build revision, it is only a partial configuration fingerprint: changes to tool implementations or external services need not change it. Unknown corpus/index/ACL versions and cost remain `null`; partial token totals carry `usage_complete=false`. Time to first output measures the first answer event, not provider token streaming or client receipt.

`RunStreamingResponse.evidence` exposes the finalized record to server-side code and tests only. It is **not** added to NDJSON responses, logged, persisted, or sent to a queue. Payload references remain `null`. The fixture demonstrates metadata reconstruction and wire compatibility, not complete replay of production context. PR 4 (below) adds redacted synthetic payloads and durable publication; the in-memory property itself is unchanged.

Files responsible:

| Area | Files |
|---|---|
| Versioned wire contract | `services/api/app/evaluation/models.py` |
| Collector, keyed identities, context binding | `services/api/app/evaluation/context.py` |
| Known configuration fingerprints | `services/api/app/evaluation/configuration.py`, `services/api/app/config.py` |
| Request lifecycle and terminal record | `services/api/app/routes/chat.py` |
| Sampling-independent operation observations | `libs/observability/tracing.py` |
| Ranked evidence and failed-branch retention | `services/api/app/agents/nodes/retriever.py` |
| Model metadata and call purpose | `services/api/app/clients/ray_llm.py`, planner/responder nodes |
| Trajectory and handled tool outcomes | tool node, `services/api/app/tools/web_search.py`, `services/api/app/tools/sandbox.py` |
| Contract, lifecycle, and real-graph coverage | `tests/observability/test_evidence_contract.py`, `test_evidence_lifecycle.py`, `test_domain_integration.py`, `test_model_metadata.py`, `fixtures/run-evidence-v1.json` |
| Sampling and configured build identity | `tests/observability/test_evidence_observations.py`, `test_evidence_configuration.py` |
| Failed-branch retention and node outcomes | `tests/observability/test_retrieval_evidence.py`, `test_agent_evidence.py` |

PR 3 verification on 2026-09-27:

| Check | Result |
|---|---|
| Full `pytest -q --tb=short` | 245 passed, 2 existing opt-in skips |
| Observability tests in the pinned API image | 87 passed |
| `make local-check` | 18 passed, 2 existing opt-in skips |
| Rebuilt local stack | All eight services healthy |
| `make local-smoke` | 4 passed |
| Git whitespace validation | Clean |

Tests cover fixture round trips, unknown schema/extra payload rejection, detached snapshots, concurrent appends and requests, unsampled failures, provider retries, build fingerprints, partial retrieval, and terminal disconnects. The fresh read-only review found no remaining Critical issues; metadata mirroring and build identity findings were reproduced with failing tests and fixed. Existing upstream deprecation warnings remain non-failing. No cloud credentials, model API keys, GPU, or deployment were required.

PR 3 is packaged into six functionality commits plus documentation, with each staged snapshot independently tested. See the [completed PR 3 commit plan](pr-03-commit-plan.md) for hashes, boundaries, and verification counts. Tests and implementations land together; there are no intentionally red commits.

### Implemented locally: PR 4 redacted payloads and durable publication

With evidence delivery enabled, a terminal run is redacted, stored, and delivered without delaying or changing the streamed answer. The API admits the run into a bounded in-memory queue (`admitted`, not durable), a runtime worker uploads approved redacted payloads as versioned objects requesting `aws:kms`, and one PostgreSQL transaction commits the immutable run envelope, an outbox row, and payload references (`committed`). A standalone worker sends committed envelopes to SQS with leased retries, bounded backoff, and explicit quarantine. Delivery is at-least-once after commit; consumers deduplicate on `(export_version, run_schema_version, run_id)` and `export_hash`. Runs can still be lost before commit, so this is not audit-grade capture.

It is off by default and opt-in through `make local-evidence-up`. Content capture is restricted to the checked-in synthetic fixture manifest, locally signed tenants, and `ENV=local|test`; production content mode is rejected at startup. Operating details, commands, limits, and gaps are in [evidence delivery operations](evidence-delivery-operations.md).

Files responsible:

| Area | Files |
|---|---|
| Export, payload, receipt, and configuration contracts | `services/api/app/evaluation/delivery_models.py`, `delivery_config.py` |
| Synthetic redaction, safe metadata projection, provenance | `redaction.py`, `metadata_export.py`, `provenance.py`, `deploy/evaluation/restricted-synthetic-v1.json` |
| Versioned object storage and bounded SDK IO | `payloads.py`, `bounded_io.py` |
| Migrations, upload intents, transactional outbox | `migrate.py`, `migrations/001_evidence_outbox.sql`, `outbox.py`, `storage_models.py` |
| Bounded capture, terminal admission, lifecycle | `capture.py`, `runtime.py`, `services/api/app/routes/chat.py`, `services/api/app/auth/jwt.py` (optional `tenant_id`) |
| Queue publisher and delivery worker | `publisher.py`, `services/evaluation_delivery/worker.py` |
| Retention, cleanup, explicit retry | `retention.py`, `services/evaluation_delivery/maintenance.py` |
| Metrics with closed label sets | `delivery_metrics.py` |
| Local orchestration | `docker-compose.evaluation.yml`, `services/aws_emulator/`, `Makefile` targets |
| Contract, real-PostgreSQL, and live acceptance coverage | `tests/evaluation_delivery/`, `tests/local_env/` |

Known limits, all documented in the operations guide: redaction is a finite pattern set with measured gaps (JSON-quoted, flag-style, `passwd`, Basic-auth, URL-embedded, and `sk-`/`ghp_` secrets pass through) and only input and retrieval-chunk payloads are fixture-hash checked, so content capture must stay on fake models and synthetic data; orphan cleanup is per run namespace rather than per version; the delivery metrics have no scrape endpoint, only the API runtime records them, and the quarantine gauges are never set; the acceptance graph exercises the approved direct-answer path only; cached runs and unapproved sources have no replay provenance; only the calculator has an approved tool payload shape; and local Moto proves version and request-parameter contracts, not IAM, KMS, or private-network enforcement.

#### PR 4 verification

Run on 2026-09-28 from the `feat/evaluation-pr4` worktree at `0a3f1af` plus the export-tag fix committed just before this documentation (re-run after the fix with identical counts), against the local stack's PostgreSQL 15, Moto S3/SQS, and fake models. The pinned API image is `scalable-rag-eval-api-local:latest` (`sha256:813687a27349595cfd2d642eab056ebca3a5fc01229c071eeb4f4f01648581d0`).

| Check | Result |
|---|---|
| Full `pytest -q` (default, no live services) | 736 passed, 73 skipped |
| Real PostgreSQL: `test_outbox_postgres.py`, `test_migrations.py` with `RUN_EVAL_POSTGRES_TESTS=1` | 49 passed |
| Delivery and observability suites with all live opt-ins | 628 passed, 0 skipped |
| Live delivery, restart, and retention acceptance | 14 passed |
| Delivery and observability suites in the pinned API image, read-only checkout, pinned test requirements | 558 passed, 70 skipped |
| `make local-check` | 38 passed, 3 skipped |
| `make local-evidence-smoke`, `make local-smoke`, `make local-evidence-check` against a disposable evidence stack | 10 passed, 4 passed, 31 passed (1 skipped) |
| Maintenance CLI in that disposable stack: dry-run, apply, unknown-run retry, invalid arguments | Exit 0, 0, 3, 2 with fixed-key output |
| `git diff --check` | Clean |

The 73 default skips are 59 live-PostgreSQL tests (`RUN_EVAL_POSTGRES_TESTS`), 11 live delivery acceptance tests (`RUN_EVAL_DELIVERY_TESTS`), 2 tests needing the default local stack (`RUN_LOCAL_STACK_TESTS`), and 1 needing the evidence stack (`RUN_LOCAL_EVIDENCE_TESTS`). The pinned-image skips are the PostgreSQL and delivery opt-ins. The live acceptance tests use per-test schemas and random bucket and queue names, refuse to reuse existing names, and clean up after themselves. The smoke targets ran against a separate Compose project with different host ports so the developer's running `scalable-rag-eval` stack was not recreated; that project and its volumes were removed afterwards.

Evidence is local. It covers no AWS IAM, KMS enforcement, private networking, real queue authentication, multi-replica behavior, or load. Those are PR 5 gates.

### Remaining Gap 1 delivery

| Slice | Status | Next outcome |
|---|---|---|
| PR 1: Trace plumbing and run identity | Committed; verified locally | Run identity and root domain spans verified; domain child spans follow in PR 2. |
| PR 2: Domain spans and typed dependency results | Committed; verified locally | Retain evidence in PR 3. |
| PR 3: Evidence contract and collector | Committed; verified locally | Metadata decisions are reconstructable; payload replay/delivery follows in PR 4. |
| PR 4: Redacted payloads and durable publication | Committed locally; verified locally (see [PR 4 verification](#pr-4-verification)) | Synthetic-fixture evidence is stored, linked, and delivered at least once after commit. Production enablement follows in PR 5. |
| PR 5: Production deployment and controls | Not started | Privacy, durability, load, and operational controls are validated. |

No cloud credentials or model-provider API keys are required for PRs 1–4 local development. The local profile uses dummy AWS credentials, a local JWT secret, fake models, Moto S3, and the local Collector. Cloud-specific credentials and controls become necessary only for staging validation and PRs 4–5.

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
- `services/api/app/routes/chat.py` now creates a distinct `run_id`, traces the streaming lifecycle and child operations, correlates every event, and persists messages inside the run. It finalizes metadata evidence; durable publication remains outstanding.
- `services/api/app/agents/nodes/retriever.py` normalizes ranked, typed records before prompt formatting; PR 3 retains them in the run collector.
- `services/api/app/clients/ray_llm.py` exposes typed provider metadata alongside a compatible text wrapper; PR 3 aggregates repeated calls into run evidence.
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

- [x] Add cache, memory, planner, retrieval, embedding, vector, graph, model, tool, persistence, and cache-write spans.
- [x] Introduce typed LLM results without breaking callers.
- [x] Normalize retrieval records and preserve rank, score, and stable identifiers.

**Exit condition met locally:** Real-graph integration tests assert direct-answer, retrieval, cache-hit, and tool span trees and provider metadata without inspecting logs. Implementation and owning tests are committed together; see the [completed commit plan](pr-02-commit-plan.md).

### PR 3: Evidence contract and collector

- [x] Add `RunEvidence` schema version `1.0`.
- [x] Add the request-scoped collector and pass it through LangGraph configuration.
- [x] Record configuration fingerprints, retrieval evidence, trajectory, operations, and partial failures.
- [x] Add fixture-based serialization and backward-compatibility tests.

**Exit condition met locally for metadata:** Tests reconstruct important decisions and dependencies from the record and schema fixture. `retrieval` is an append-only list to support repeated calls rather than the singular illustrative model above. Payload references, full-context replay, storage, and delivery remain PR 4 work; this is not yet a durable audit record.

### PR 4: Redacted payloads and durable publication

- [x] Add restricted synthetic redaction and versioned KMS-requested object storage.
- [x] Add the transactional PostgreSQL outbox, SQS publication with bounded retries, and the standalone delivery worker with explicit quarantine.
- [x] Add idempotency and duplicate-consumer tests against real PostgreSQL.
- [ ] Publisher failure metrics: counters are defined and recorded by the API runtime only; the worker and maintenance entrypoints do not record them, the quarantine gauges are never set, and no route exposes the registry. Tracked as a known gap in [evidence delivery operations](evidence-delivery-operations.md#metrics-and-known-observability-gaps).

**Exit condition met locally for synthetic fixtures:** Restart and failure-injection tests prove at-least-once delivery after commit, with identical redelivery deduplicated and a conflicting hash rejected by a transactional test consumer. Approved synthetic component-replay payloads are stored and linked. Cached-run and unapproved-source replay provenance remain incomplete, and production content mode is rejected. See [evidence delivery operations](evidence-delivery-operations.md).

### PR 5: Production deployment and operating controls

**Design:** [PR 5 production evidence design](pr-05-production-evidence-design.md), split into 5a (metadata-only production activation, scheduled for implementation) and 5b (content-capture enablement, designed but blocked on retrieval ACL enforcement).

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

## Design decisions settled for PR 4

The three choices that needed product, security, and operations agreement were resolved in the [approved PR 4 design](pr-04-payload-publication-design.md):

1. **Delivery guarantee:** transactional PostgreSQL outbox, at-least-once after commit. Not audit-grade: runs can be lost before commit.
2. **Content policy:** redacted content only, restricted to checked-in synthetic fixtures (`restricted-synthetic-v1`). No raw tier, and production content mode is rejected.
3. **Retention:** content 30 days from object creation, run metadata 90 days from completion. Queue copies, traces, backups, datasets, and evaluation results need their own policies, decided in PR 5.

## References

- [Evaluation-plane gap analysis](../evaluation-plane-gap-analysis.md)
- [Evaluation harness design](../eval-harness-design.md)
- [Evaluation harness implementation plan](../eval-harness-plan.md)
- [OpenTelemetry Python exporters](https://opentelemetry.io/docs/languages/python/exporters/)
- [OpenTelemetry Collector deployment patterns](https://opentelemetry.io/docs/collector/deploy/)
- [OpenTelemetry generative-AI semantic attributes](https://opentelemetry.io/docs/specs/semconv/registry/attributes/gen-ai/)
