# Local environment for evaluation instrumentation

The `eval-local` Docker Compose profile runs the API and every dependency needed to develop and test production-run instrumentation without an AWS account or GPU.

## What runs locally

| Service | Host endpoint | Purpose |
|---|---|---|
| API | `http://127.0.0.1:18000` | Containerized FastAPI service using repository-pinned dependencies |
| Fake models | `http://127.0.0.1:8001` | Deterministic OpenAI-compatible chat and embedding responses |
| PostgreSQL | `127.0.0.1:15432` | Chat history and, in evidence mode, the transactional outbox |
| Redis | `127.0.0.1:16379` | Semantic-cache coordination |
| Qdrant | `http://127.0.0.1:16333` | Vector retrieval and semantic cache |
| Neo4j | `bolt://127.0.0.1:17687` | Graph retrieval |
| Moto S3 and SQS | `http://127.0.0.1:14566` | Credential-free evidence payload storage and evaluation queues |
| OTLP gRPC | `http://127.0.0.1:14317` | Trace ingestion by the local Collector |
| OTLP HTTP | `http://127.0.0.1:14318` | HTTP trace ingestion |
| Collector health | `http://127.0.0.1:13134` | Collector readiness check |

All published ports bind to loopback. The local services are not exposed to the LAN.

The stack uses the separate Compose project name `scalable-rag-eval` and alternate host ports, so it can coexist with PostgreSQL, Redis, Qdrant, or Neo4j instances already running on their standard ports.

## Start and verify the stack

Validate the configuration and local contract tests:

```bash
make local-check
```

Build and start the stack:

```bash
make local-up
```

The command waits until the API, fake models, PostgreSQL, Redis, S3 emulator, and other Compose services are ready.

Exercise the running stack:

```bash
make local-smoke
```

The smoke gate verifies:

- The fake model service is reachable.
- The OpenTelemetry Collector health endpoint is available.
- The OpenTelemetry Collector accepts a completed span over OTLP/gRPC.
- The `eval-traces` S3 bucket exists with versioning enabled.
- The API liveness endpoint succeeds.
- JWT authentication succeeds with the local-only key.
- One streaming agent request returns the deterministic answer.

Stop the stack:

```bash
make local-down
```

Compose preserves the PostgreSQL, Qdrant, and Neo4j named volumes unless they are explicitly removed. The Moto S3 service is intentionally ephemeral and recreates the versioned `eval-traces` bucket on startup.

## Run an optional load test

Load testing is separate from the correctness suite and is not installed in the API image. Install Locust in the development environment:

```bash
make load-test-install
```

With the local stack running, export a valid local JWT and start the Locust web interface:

```bash
export AUTH_TOKEN="<local-jwt>"
make load-test
```

Then open `http://127.0.0.1:8089` and choose the simulated user count, spawn rate, and duration. By default the scenario in `scripts/load_test.py` targets `http://127.0.0.1:18000`; override it with `make load-test LOAD_TEST_HOST=http://...`.

The scenario sends authenticated streaming chat requests at randomized one-to-five-second intervals, consumes each response stream, and records non-200 responses as failures. It measures traffic behavior and endpoint performance; it does not grade answer quality. Its fixed session identifier intentionally represents repeated turns in one shared conversation, so use distinct session IDs before interpreting it as independent-user traffic.

## Fake model behavior

The fake service implements:

```text
GET  /health
POST /v1/chat/completions
POST /api/embeddings
```

Chat responses include a stable request ID, model identifier, finish reason, and deterministic token counts. Planner requests return valid routing JSON. Normal generation requests return:

```text
This is a deterministic local response.
```

Embeddings are deterministic, normalized vectors with `EMBED_DIM` dimensions. They are intended to exercise storage, retrieval, tracing, and failure paths, not measure semantic quality.

Send `x-fake-scenario: error` to either endpoint to receive a provider-style `503`. Send `x-fake-scenario: malformed` to the chat endpoint to test response validation.

## Configuration

The local profile reads [.env.example](../../.env.example). Its credentials and JWT secret are local-only placeholders.

The containerized API uses service DNS names internally, while host tools use the alternate loopback ports above. Run the API through `make local-up` for reproducible dependency versions. Running it from an arbitrary host Python environment can introduce client/server version drift.

To replace the fake models with another local or hosted provider, override `RAY_LLM_ENDPOINT`, `RAY_EMBED_ENDPOINT`, `LLM_MODEL_NAME`, `EMBED_MODEL_NAME`, and `EMBED_DIM` with a compatible endpoint contract.

## Inspect API traces

The API now sends HTTP and `agent.run` spans to the local Collector. Every accepted chat request returns an `X-Run-ID` response header, and each NDJSON event includes the same `run_id` plus its conversation `session_id`. `make local-smoke` checks those identifiers alongside the deterministic answer.

Inspect exported spans after making a request:

```bash
docker compose -p scalable-rag-eval --profile eval-local --env-file .env.example logs --since 1m otel-collector
```

Look for `agent.run`, its `run_id`, `run.status`, and `cache.hit`. Its trace ID and parent ID join it to the HTTP server span. Export is batched, so allow several seconds for spans to appear. The Collector's debug exporter is a local inspection aid, not durable trace storage.

The local profile exports all root traces by default. `OTEL_TRACES_SAMPLER` supports `always_on`, `always_off`, `traceidratio`, and their `parentbased_` variants; `OTEL_TRACES_SAMPLER_ARG` controls the ratio. `OTEL_SERVICE_NAME` and `OTEL_SERVICE_VERSION` identify the API build. For host execution, console export is opt-in through `OTEL_CONSOLE_EXPORT_ENABLED`; leave it false when using OTLP. The exporter uses gRPC, with plaintext for local `http://` endpoints and TLS for `https://` endpoints.

Raw query strings and captured header values are removed from HTTP telemetry. Prompts, answer bodies, user IDs, and exception messages are not attached to domain spans. Child spans now show cache, memory, planning, retrieval, embeddings, vector/graph search, model calls, tools, persistence, and cache writes. Look for `operation.status`, `operation.latency_ms`, retrieval result counts, provider token usage when available, and retry counts. Raw retrieved content and tool arguments/results remain out of spans; durable run evidence is still a later slice.

The focused contract suite is `pytest tests/observability -q`. Its real-graph tests use the fake model service in-process and embedded Qdrant, so they need neither a GPU nor live service credentials. To confirm dependency compatibility, run the same tests inside the API image with the repository mounted read-only and pytest installed into a disposable container. Query embeddings keep their five-second per-operation timeout; document embeddings retain sixty seconds even when the HTTP client is pooled.

PR 3 also collects schema-version `1.0` run metadata independently of span sampling. After the stream closes, route-level tests inspect `RunStreamingResponse.evidence`; this server-side property is not an HTTP endpoint or an NDJSON field. It contains hashes, ranked identities, typed outcomes, provider usage, trajectory, and configuration—not raw payloads. By default the record is in memory only; the opt-in evidence mode described below persists a redacted projection of it. Missing identity hashing keys and corpus/index versions are explicit; local model API keys are still unnecessary. See [PR 3 progress and limitations](gap-01-production-run-instrumentation.md#implemented-locally-pr-3-evidence-contract-and-collector).

`OTEL_EXPORTER_OTLP_TIMEOUT` is a timeout for each export RPC. SDK retries can extend shutdown when the Collector is unavailable; a strict shutdown deadline still needs resilience testing before production deployment.

## Run the opt-in evidence delivery stack

PR 4 adds durable, redacted evidence delivery. It is off by default: `make local-up` starts no evidence runtime, clients, or workers. Opt in with:

```bash
make local-evidence-check    # validate the override and its contract tests; no running stack needed
make local-evidence-up       # migrate, recreate api-local with evidence enabled, start the delivery worker
make local-evidence-smoke    # needs the running evidence stack
make local-evidence-down     # stop; volumes are kept
```

`make local-evidence-up` uses the same `scalable-rag-eval` project as `make local-up`, so it recreates `api-local` in an already running stack. The override `docker-compose.evaluation.yml` adds three things: the `evidence-migrate` job, the evidence flags and dummy identity, KMS-request, and AWS settings on `api-local`, and the standalone `evidence-delivery` worker. The Moto service also hosts the `eval-runs` queue and its `eval-runs-dlq` dead-letter queue, created idempotently at startup with a redrive policy. All host ports stay on loopback, and the fake models and ingestion resources are untouched.

Content capture accepts only the checked-in synthetic fixtures and locally signed tokens with a `tenant_id` claim. It is not a general-purpose sanitizer and must not be pointed at real data. See [evidence delivery operations](evidence-delivery-operations.md) for configuration, migrations, retention and retry commands, metrics, and known gaps.

## Scope and limitations

This environment proves application contracts and failure behavior. It does not reproduce AWS IAM, KMS, IRSA, managed queue authentication, EKS networking, multi-replica shutdown, or production load. Local Moto verifies object-version, reference, and queue-message contracts, not KMS encryption or IAM authorization. Those remain staging checks under PR 5.

The default profile still runs no broker or delivery worker. The opt-in evidence profile delivers committed records at least once to a local SQS queue; it makes no exactly-once claim and is not audit-grade capture.
