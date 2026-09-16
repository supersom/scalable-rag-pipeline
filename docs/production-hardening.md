# Production hardening plan

This note captures the main work needed to turn this repository from an internal
RAG platform scaffold into an end-user SaaS application.

## Goal

The current project has the core RAG engine: document ingestion, FastAPI chat,
LangGraph routing, hybrid Qdrant and Neo4j retrieval, Ray-based model serving,
semantic caching, and deployment assets. A SaaS version needs a stronger product
and control plane around that engine: tenants, users, document workspaces,
upload status, quotas, billing hooks, audit trails, supportability, and
production-grade operations.

## Auth and tenancy

The current auth path uses shared-secret JWT validation. For SaaS, move to a
hosted identity provider or enterprise SSO flow such as OIDC/OAuth with Auth0,
Cognito, Clerk, WorkOS, or a similar provider.

- Validate asymmetric JWTs through JWKS instead of a single shared secret.
- Add `org_id` or `tenant_id` to every request, document, vector payload, graph
  node, chat session, cache entry, and audit event.
- Enforce RBAC and scopes for upload, chat, admin, billing, API key management,
  and data export.
- Add API keys for programmatic access, with independent revocation and usage
  tracking.
- Add audit logs for login, upload, delete, query, admin changes, and exports.
- Enforce tenant filters in Qdrant, Neo4j, Postgres, and semantic cache lookups.

Tenant isolation is the most important SaaS boundary. Without it, this is an
internal app rather than a customer-facing product.

## Retries, timeouts, and degradation

The repo has some retry logic, especially around the LLM client and SQS ingestion
worker, but production behavior should be standardized across every external
dependency.

- Add explicit connect, read, write, and total timeouts for LLM, embedding,
  Qdrant, Neo4j, Postgres, Redis, S3, SQS, Ray Jobs, and web search calls.
- Use exponential backoff with jitter for transient failures only.
- Add circuit breakers around model serving and storage dependencies.
- Define per-route request budgets so streaming chat requests cannot hang
  indefinitely.
- Make startup readiness degrade cleanly when Qdrant, Neo4j, Redis, Postgres, or
  Ray are temporarily unavailable.
- Add idempotency keys for uploads and ingestion jobs.
- Avoid blind retries for non-idempotent writes unless the write path is made
  idempotent first.

## Observability

The project has early OpenTelemetry and Prometheus pieces, but a SaaS service
needs complete request tracing, production metrics, structured logs, and alerts.

- Export OpenTelemetry traces through OTLP to Datadog, Grafana Tempo, Honeycomb,
  Jaeger, or another production backend.
- Create one trace per chat request with spans for cache lookup, planner,
  retrieval, tool call, responder, LLM call, embedding call, Qdrant, Neo4j, and
  Postgres.
- Track p50, p95, and p99 latency, request count, error rate, cache hit rate,
  retrieval latency, LLM latency, token usage, cost, queue depth, ingestion
  duration, failed documents, and zero-chunk documents.
- Emit structured JSON logs with `request_id`, `session_id`, `user_id`,
  `org_id`, `trace_id`, route, dependency, and error fields.
- Alert on API error rate, LLM failures, ingestion DLQ depth, Qdrant or Neo4j
  availability, queue age, GPU saturation, and runaway token spend.

## Ingestion failure handling

The SQS worker already waits for Ray jobs, extends SQS visibility, and relies on
SQS retries. For SaaS, ingestion state should be durable, inspectable, and
visible to users.

- Add an `ingestion_jobs` table with statuses such as `queued`, `running`,
  `succeeded`, `failed`, and `partially_failed`.
- Track per-document and per-stage status: uploaded, parsed, chunked, embedded,
  indexed to Qdrant, indexed to Neo4j.
- Base idempotency on tenant, S3 key, object version, and/or content hash rather
  than SQS message ID.
- Add DLQ inspection and an admin/user reprocess action.
- Store parse errors, unsupported-file reasons, and model/indexing failures.
- Treat zero chunks or zero vectors as a failure unless explicitly expected.
- Record chunk count, vector count, graph node count, and graph edge count.
- Quarantine bad files so one failed file does not poison a whole batch.
- Expose upload and ingestion progress in the UI.

## Tests

Several important flows currently have little or no visible test coverage. A
SaaS version needs tests around correctness, isolation, reliability, and
security.

- Unit test JWT validation, planner JSON parsing, tool routing, cacheability
  rules, chunking, metadata extraction, and retry behavior.
- Add Docker Compose integration tests for Postgres, Redis, Qdrant, and Neo4j.
- Add contract tests for `/api/v1/chat/stream` NDJSON events: routing, status,
  answer, and error.
- Test ingestion with small PDF, TXT, DOCX, HTML, and PPTX fixtures, asserting
  Qdrant and Neo4j writes.
- Add multi-tenant isolation tests proving tenant A can never retrieve tenant
  B's documents, graph entities, cache entries, or chat history.
- Load test streaming chat concurrency and ingestion throughput.
- Add eval tests for answer quality, citations, no-answer behavior, and tool
  routing.
- Add security tests for auth bypass, upload validation, prompt injection, tool
  sandbox behavior, and secret leakage.

## Deployment config

The repo already includes Kubernetes, Helm, Terraform, Ray, and EKS assets.
Production SaaS deployment should make environment separation, rollouts,
secrets, migrations, and dependency health more rigorous.

- Maintain separate `dev`, `staging`, and `prod` Helm values and Terraform
  workspaces or accounts.
- Add CI/CD that builds immutable images, scans them, runs tests, and deploys by
  image digest.
- Use database migrations, such as Alembic, instead of creating tables at API
  startup.
- Use External Secrets or a cloud secret manager for all sensitive values.
- Tune readiness and liveness probes so they reflect dependency health without
  causing avoidable crash loops.
- Scale API pods on request concurrency or latency, not CPU alone.
- Set GPU node pool limits, model warmup behavior, and capacity alerts.
- Add PodDisruptionBudgets for API, Qdrant, Neo4j, Ray head/services, and other
  critical workloads.
- Add NetworkPolicies so only the intended pods can talk to databases and model
  services.
- Define backup and restore plans for Postgres, Qdrant, Neo4j, and uploaded
  documents.
- Use canary or blue/green deploys for API and model-serving changes.

## Suggested priority order

1. Tenant isolation and real auth.
2. Durable ingestion job state.
3. Observability and alerting.
4. Integration tests around chat and ingestion.
5. Deployment cleanup: migrations, secrets, probes, and CI/CD.
6. Product shell: organizations, uploads, admin UI, usage limits, billing, and
   support tooling.
