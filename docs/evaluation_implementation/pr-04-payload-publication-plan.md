# PR 4: Payload Storage and Publication Plan

> **For agentic workers:** This is a decision-gated draft, not authorization to implement or enable capture. After the choices in Task 0 and the written design are approved, use `superpowers:executing-plans` and test-driven development. Pair each functionality module with its tests in a green commit.

**Status:** Policy choices and all three conversational design sections approved. The [written specification](pr-04-payload-publication-design.md) awaits user review; this planning draft is not executable. No PR 4 implementation or infrastructure changes made.

**Goal:** Turn PR 3's terminal in-memory records into safely stored, asynchronously deliverable evaluation evidence, with an explicit and tested delivery guarantee.

**Architecture:** Production captures evidence; storage and publication adapters move it; separate workers handle delivery/retries and later evaluation. Keep graders and broker calls off the response path. Add encrypted payload references only under an approved content policy.

**Tech stack:** Existing FastAPI, Pydantic, SQLAlchemy/asyncpg, boto3, PostgreSQL 15, Moto, pytest, Docker Compose. Proposed queue adapter: SQS; no Ray or GPU dependency for evidence delivery.

**Spec:** [PR 4 written design](pr-04-payload-publication-design.md), based on the [Gap 1 instrumentation design](gap-01-production-run-instrumentation.md#pr-4-redacted-payloads-and-durable-publication). The written design supersedes tentative interfaces/defaults below. After its approval, revise this draft into a single-path implementation plan and review that plan separately. This document refines PR 4, not PR 5 cloud deployment or the later scoring-worker implementation.

## Starting point

- `evaluation/models.py` defines strict schema `1.0`; payload references are optional and presently empty.
- `evaluation/context.py` finalizes detached, metadata-only snapshots, with no storage or publication hook.
- `routes/chat.py` owns stream/disconnect finalization and exposes the record server-side.
- `services/api/main.py` owns startup/shutdown and currently creates chat-history tables; it has no evidence worker or migration runner.
- The local stack already has PostgreSQL and a versioned Moto `eval-traces` bucket. Moto currently installs S3 support only; there is no local evaluation queue.
- `pipelines/jobs/sqs_ingestion_worker.py` is for document ingestion/Ray jobs. Do not repurpose it: evaluation delivery needs different messages, identities, retries, and ownership.

## Task 0: Approve policy and delivery semantics

These are product/security decisions, not values to infer from local development defaults.

| Decision | Approved choice | Consequence |
|---|---|---|
| Delivery | Transactional outbox | Committed records survive dispatcher restart; pre-commit failures can still lose evidence. |
| Content | Encrypted, redacted replay content; no raw tier | Capture sanitized questions, answers, model context, and tool inputs/results. Replay uses sanitized content, not necessarily the exact original run. |
| Retention | Redacted content: 30 days; run metadata: 90 days | User-selected engineering policy; organizational requirements and production enforcement still require validation. No other artifact retention is implicitly approved. |
| API admission | Fail-open | Evidence-persistence failure does not block the answer; report losses explicitly. This is not audit-required response gating. |
| Capture failure | Preserve independently validated safe evidence | Keep validated metadata and successfully stored safe payloads; mark unsafe, failed, or missing payloads unavailable with content-free reason codes. Replay may be incomplete. Never export questionable raw content. |
| Content durability | Upload encrypted, redacted payloads before committing the outbox | Commit only verified versioned references and explicit unavailable states. No PostgreSQL content staging. Pre-commit crashes can lose evidence and leave orphaned objects requiring cleanup. |
| Redaction approach | Restricted capture with versioned rules; no external DLP service in this slice | Only explicitly approved fields and content sources are eligible. Omit unknown/unapproved content. Validate with synthetic local fixtures; production content capture remains disabled pending security review. This is not a guarantee of detecting every secret. |

These choices were confirmed through six option-A responses, followed by separate approvals of the data flow and field/redaction design below. They do not authorize implementation, cloud deployment, or production capture enablement. The written specification must enumerate exact schemas, rules, and local fixture/source approvals; no arbitrary production source or tool is approved by these category-level choices.

Two implementation approaches:

1. **Outbox:** API commits a metadata run record and outbox row in one PostgreSQL transaction; an external dispatcher publishes it. More database/migration work, but recoverable delivery after commit. Recommended when reliability matters.
2. **Best-effort:** API submits to a bounded in-process dispatcher; it stores approved payloads and sends the metadata event. Lower coupling, but process crashes, admission overflow, or exhausted retries can lose samples. Acceptable only with measured loss and explicit consent.

**Critical distinction:** An outbox does not prevent losing a run between sending the answer and committing the outbox transaction. The current route sends its answer before persistence completes. Do not label a post-stream outbox hook “every successful answer is audited.” Audit-required mode needs a separately approved persistence/response acknowledgement boundary and failure behavior; it may add bounded database latency and reduce availability. Best-effort mode must preserve the existing answer semantics and report losses.

- [x] Record approved delivery mode, fail-open response behavior, redacted capture scope, and retention durations in this document.
- [x] Record capture-failure behavior: preserve independently validated safe evidence and explicitly mark unavailable payloads; do not drop safe evidence solely because another payload failed.
- [x] Select encrypted, redacted object uploads before outbox commit; no PostgreSQL content staging. Record pre-commit loss and orphan-cleanup requirements.
- [x] Select restricted, versioned redaction without an external DLP service; omit unapproved content and keep production content capture disabled pending security review.
- [x] Approve the field/redaction design below, including eligible input/history/context/model fields, per-tool policy, and denial of unclassifiable content. Exact rules and schemas must be enumerated in the written specification.
- [x] Select redacted component replay for the initial release; metadata-only delivery alone is not completion of the selected scope.
- [ ] Review and approve the resulting written design before converting the backend-specific tasks below into an execution plan. The unselected transport path is not implemented.

### Design review progress

**Data flow approved:** Capture/redact eligible content immediately into bounded buffers; return/stream the answer without waiting for evidence storage. At terminal finalization, upload approved payloads, then atomically commit safe metadata, verified references, explicit unavailable states, and the outbox row. A standalone worker publishes committed envelopes with restart recovery. Pre-commit loss and orphaned uploads remain possible; production content capture stays disabled pending security review. This section was approved separately from the six policy choices; no implementation authorization is implied.

**Field/redaction design approved:**

- Question/answer: only the question text and answer text, not the entire request/response or arbitrary citation dictionaries.
- Model calls: ordered message roles/content actually sent (including history and assembled context), requested model, temperature, and max tokens; successful response text. No headers, authentication values, provider response bodies, or unrelated chat history.
- Retrieval: query and ranked chunk text from explicitly approved corpus sources. No unrestricted document metadata, filenames, or source paths. Unapproved sources make associated model-context capture unavailable rather than allowing flattened text to bypass source policy.
- Tools: explicit per-tool input/result schemas and source approval, not automatic approval from registry membership. Unknown tools, extra fields, and unapproved tool IO remain unavailable; metadata may still record the safe trajectory. Sandbox code/results are not eligible by default.
- Metadata: explicit field/value allowlist; validate or pseudonymize externally supplied identifiers. Do not treat arbitrary strings or dependency attribute dictionaries as safe merely because schema `1.0` accepts them.
- Versioned rules deny credential-bearing fields and redact tested token/private-key patterns plus email/phone patterns with typed placeholders. Unsupported structures, policy errors, absent trusted tenant context, and over-budget payloads are omitted with fixed reason codes. Pattern coverage is limited; no general secret-detection guarantee. Local capture is limited to synthetic approved fixtures until production security review.

**Failure handling, retention, and testing approved:**

- Bound admission, capture bytes, upload concurrency, SDK waits/retries, and shutdown drain. On overflow or pre-commit failure, release buffers and record a content-free loss reason; never delay the answer for evidence admission. Persist safe partial evidence where possible; unsafe metadata is never exported merely to preserve a record.
- Recover committed outbox rows after worker restart using expiring leases. Broker acknowledgement loss can cause redelivery with the same identity/hash; identical duplicates are harmless, conflicts quarantined. Exhausted producer retries are quarantined with observable age/count, not falsely marked delivered.
- Measure content retention from object creation and run-metadata retention from run completion. Authorized reads reject expired content even if deletion is delayed. Scheduled cleanup removes expired object versions and delete markers; orphan cleanup must avoid active uploads and committed live references. Expired references remain historical facts, not promises of replay availability. Run metadata is deleted at 90 days; other artifact retention is not implicitly approved.
- Test deterministic redaction and tenant denial, partial upload/database/broker failures, disconnect finalization, resource limits, duplicate delivery, and concurrent/restarted workers. Use synthetic fixtures, real local PostgreSQL, Moto S3/SQS, and fake models. Verify KMS request parameters with Stubber; cloud IAM/KMS enforcement and production lifecycle guarantees remain PR 5 staging checks.

## Global constraints

- Default `EVAL_EVIDENCE_ENABLED=false` and `EVAL_CAPTURE_CONTENT=false`; existing requests and NDJSON remain compatible when disabled.
- No LLM judges, scorers, Opik dependency, cloud provisioning, production credentials, or GPU models in this slice.
- No raw-content tier in the initial scope. Redaction happens before storage/export, never in a consumer after export.
- Restricted capture requires an explicit versioned field/source allowlist, not merely a permissive free-text sanitizer. Unknown/unapproved content is unavailable. Synthetic fixtures establish tested behavior, not proof that arbitrary production text is safe.
- No raw user IDs, content, credentials, exception messages, queue bodies, or object paths containing user-provided text in logs/metrics/spans.
- Trusted tenant identity must come from authenticated server context. Missing tenant identity must not enable cross-tenant content capture or shared content lookup.
- Bound capture bytes, pending run count, upload concurrency, SDK timeouts/retries, drain time, and queue message size. Reject/omit oversized payloads with an explicit reason; never silently truncate replay evidence.
- Unknown corpus/index/ACL/cost values remain unavailable. Storage does not create missing provenance.
- Keep schema `1.0` fixtures readable. Add delivery/payload envelopes outside the immutable run contract where possible; if an incompatible wire change is unavoidable, explicitly version and migrate it.
- Preserve the four unrelated untracked documents; do not push or deploy during local implementation.

## Review focus and required failure tests

1. Crash before commit versus after commit: prove exactly which records recover; do not claim unconditional no-loss.
2. Crash after broker acknowledgement but before marking delivered: expect a duplicate, with unchanged event identity and safe consumer deduplication.
3. Failed/partial uploads: do not publish references to objects that were never stored; expose incompleteness and clean up orphaned versions.
4. Tenant mismatch or missing identity: resolver denies access before reading objects; content capture fails closed.
5. Header/body disconnect, full admission buffer, hanging SDK calls, and shutdown: finalize once, bound resources, preserve the selected answer/failure policy.

## Task 1: Export policy and delivery contracts

**Proposed commit:** `feat(evaluation): define safe evidence delivery contracts`

Files:

- Create `services/api/app/evaluation/delivery_models.py` — separate typed delivery receipt, payload reference, and completeness states.
- Create `services/api/app/evaluation/redaction.py` — approved field policy and redacted serialization.
- Modify `services/api/app/config.py` — typed enablement, content-policy, size, timeout, destinations, and encryption settings.
- Create `tests/observability/test_evidence_redaction.py` and `test_evidence_delivery_config.py`.

**Interfaces:** `prepare_export(event: RunEvidence, policy: ExportPolicy) -> ExportEnvelope`; `PayloadRef` holds bucket/key/version, redacted-byte hash, size, and policy version; `DeliveryReceipt` distinguishes committed, dropped, disabled, and failed. Define `ExportPolicy` and `ExportEnvelope` in `delivery_models.py` so storage and publishers share one contract.

- [ ] Write failing tests for disabled defaults, forbidden/extra fields, secret-bearing nested values, untrusted identifiers, missing tenant, and enabled-but-incomplete configuration.
- [ ] Use approved fixtures with literal expected redactions. In metadata-only mode, reject content regardless of field name. Do not call a handful of regexes complete DLP.
- [ ] Implement the minimal policy serializer and configuration validator; startup fails when enabled destinations/encryption/policy are inconsistent.
- [ ] Run `pytest tests/observability/test_evidence_redaction.py tests/observability/test_evidence_delivery_config.py -q`, then `pytest -q`; expect all tests to pass.
- [ ] Commit implementation and tests together.

## Task 2: Versioned encrypted payload adapter

**Proposed commit:** `feat(evaluation): store approved payloads by versioned reference`

Files:

- Create `services/api/app/evaluation/payloads.py` — `PayloadStore` protocol, disabled adapter, S3 writer, and authorized resolver.
- Create `tests/observability/test_evidence_payloads.py`.
- Modify `services/aws_emulator/init_s3.py` and `tests/local_env/test_aws_emulator.py` only for approved local bucket configuration.

**Interfaces:** `await PayloadStore.put(run_id, tenant_partition, kind, redacted_bytes) -> PayloadRef`; `await PayloadStore.get(reference, authorized_tenant) -> bytes`. Accept only the approved redacted type from Task 1, not arbitrary request dictionaries. Object keys use environment, opaque tenant partition, date, run ID, and fixed payload kind; tool steps use validated ordinals.

- [ ] Write failing tests for upload/read round trips, recorded version IDs, wrong tenant, unsafe key inputs, byte limits, upload denial/timeouts, and returned-content hash mismatch.
- [ ] Use botocore `Stubber` to verify production `ServerSideEncryption="aws:kms"` and the configured dedicated `SSEKMSKeyId`; use Moto for actual object round trips/versioning. Emulator success is not proof of KMS or IAM enforcement.
- [ ] Implement off-event-loop bounded boto3 calls with finite SDK timeouts/retries; cancelling an await must not leak unbounded background threads. Reuse owned clients and close them at shutdown.
- [ ] Resolve references through an authorized client, not public or presigned URLs. Preserve version IDs so a retry/new object version does not change an already published reference.
- [ ] Test deletion across all versions and delete markers, plus orphan cleanup. Production IAM/lifecycle enforcement is a PR 5 staging gate.
- [ ] Run `pytest tests/observability/test_evidence_payloads.py tests/local_env/test_aws_emulator.py -q`, then `pytest -q`; commit green.

## Task 3: Approved admission backend

**Outbox branch — proposed commit:** `feat(evaluation): persist terminal runs with a transactional outbox`

Files: create `services/api/app/evaluation/outbox.py`, `storage_models.py`, `migrate.py`, `migrations/001_evidence_outbox.sql`, `tests/observability/test_evidence_outbox.py`, and `tests/local_env/test_evidence_outbox_postgres.py`.

**Interfaces:** `await OutboxRepository.enqueue(event: RunEvidence) -> DeliveryReceipt`; `claim_batch(limit, lease_duration)`; `mark_delivered(row_id, lease_token, receipt)`; `schedule_retry(row_id, lease_token, error_class)`. Separate evidence database models from chat history; no opportunistic destructive schema recreation.

- [ ] Write failing tests for atomic run/outbox insertion, rollback, identical duplicate acceptance, conflicting duplicate rejection, and restart recovery against real local PostgreSQL.
- [ ] Add a unique `(schema_version, run_id)` event identity and canonical export hash. Store retry state, UTC next-attempt time, claim lease/token, acknowledgement ID, and quarantined status separately from immutable run evidence.
- [ ] Supply an idempotent, version-tracked migration command (`python -m services.api.app.evaluation.migrate`) with tests for reapplication, transactional failure, and concurrent startup. Local orchestration runs it before API/worker startup; production migrations use the approved deployment workflow rather than `create_all` as a migration strategy.
- [ ] Implement short transactions for claiming/acknowledgement; do not hold database locks while uploading or calling SQS. Lease expiry permits restart recovery, and stale workers cannot overwrite newer claims.
- [ ] Test two concurrent dispatchers, expired leases, delayed stale acknowledgements, and unavailable database admission.
- [ ] Run the focused tests and full suite; commit only after the transaction/restart contract passes.

**Best-effort branch — alternative proposed commit:** `feat(evaluation): add bounded evidence dispatch admission`

Files: create `services/api/app/evaluation/dispatch.py` and `tests/observability/test_evidence_dispatch.py` instead of outbox models/migration.

**Interfaces:** `submit(envelope) -> DeliveryReceipt` is non-blocking; `await drain(deadline)` is bounded. Enforce item and byte capacity, fixed worker concurrency, no unbounded `create_task`, and observable drop reasons.

- [ ] Test overflow, maximum resident bytes, stopped workers, upload/queue timeouts, and expired shutdown deadline.
- [ ] Implement explicit best-effort loss receipts and counters. Document crash loss; do not claim restart recovery.
- [ ] Run focused tests and full suite; commit implementation with tests. Do not also implement the outbox branch.

**Selected content/outbox boundary:** Upload approved encrypted, redacted payloads to versioned object storage before enqueue. Then atomically commit safe metadata, verified references, explicit unavailable states, and the outbox row. Failed uploads do not prevent committing independently validated safe evidence. The delivery worker publishes committed envelopes without depending on API memory or uploading missing content later. PostgreSQL stores metadata/references, not staged prompts. A crash before commit can lose the run and leave uploaded object versions orphaned; cleanup must distinguish them from committed references and active uploads. The answer remains fail-open. This choice does not make the object uploads and database commit one atomic transaction.

## Task 4: Queue adapter and standalone delivery worker

**Proposed commit:** `feat(evaluation): publish evidence with bounded retries and recovery`

Files: create `services/api/app/evaluation/publisher.py`, `services/evaluation_delivery/__init__.py`, `worker.py`, `tests/observability/test_evidence_publisher.py`, and `test_evidence_delivery_worker.py`.

**Interfaces:** `await RunEvidencePublisher.publish(envelope: ExportEnvelope) -> PublishReceipt`; `await worker.run_once() -> int`. Define `PublishReceipt` in `delivery_models.py`. Outbox mode consumes claimed rows; best-effort mode consumes admitted bounded envelopes. The worker delivers evidence only—it does not score runs.

- [ ] Write failing tests for correct metadata-only queue bodies, stable identity/hash across retries, no secret-bearing logging, finite retry schedules, broker denial, and acknowledgement loss.
- [ ] Publish only after required payload writes have succeeded. Attach storage failures to delivery completeness, not to the original agent outcome; never fabricate valid references.
- [ ] On acknowledgement-before-DB-update crash, retry the same event identity. A test consumer stores `(schema_version, run_id)` transactionally and accepts identical redelivery; conflicting payloads are quarantined.
- [ ] Distinguish producer failures from consumer failures: producer exhaustion stays in outbox quarantine (or is a measured drop in best-effort mode); SQS DLQ redrive handles received messages failing downstream processing.
- [ ] Run focused tests and full suite; commit green. No “exactly once” claim based solely on broker FIFO settings.

## Task 5: Capture and request lifecycle integration

**Proposed commit:** `feat(api): hand terminal evidence to the approved delivery path`

Files: modify `evaluation/context.py`, `routes/chat.py`, `services/api/main.py`, and `libs/observability/metrics.py`; create `evaluation/runtime.py`, `tests/observability/test_evidence_delivery_lifecycle.py`; extend lifespan and existing integration tests.

**Interfaces:** `await EvidenceRuntime.submit(event, approved_payloads) -> DeliveryReceipt` follows the approved admission semantics; runtime start/close belongs to FastAPI lifespan. Best-effort submission admits to a bounded buffer without network IO; outbox admission awaits its bounded transaction. Extend generator and ASGI-close hooks together so pre-body disconnects still submit once; neither hook may hide a database write in a synchronous callback.

- [ ] Write failing tests for disabled parity, cached/normal/error/timeout/cancelled outcomes, header/body disconnect, exactly one submission, and admission failure under the chosen fail-open/fail-closed policy.
- [ ] If content is approved, capture before flattening at retriever/model/tool boundaries, redact immediately, enforce byte budgets, and map references by repeated-call/step ordinal. Keep content buffers out of AgentState and release them promptly. Model-request capture must preserve ordered history/context and decoding settings if component replay is claimed.
- [ ] Keep the finalized collector snapshot immutable. Build an export copy with payload references; retain original hashes separately from redacted-payload hashes. Use an explicitly versioned payload manifest for model-call/retrieval query references absent from schema `1.0`.
- [ ] Add low-cardinality attempts/failures/drops counters, pending/oldest-age gauges, and latency/bytes histograms. Never use run/user/tenant IDs as metric labels. Evidence failure must not be presented as successful delivery.
- [ ] In best-effort sampling mode, no broker or S3 wait in the request generator. In audit-required mode, implement only the explicitly approved durability/acknowledgement boundary and its timeout/availability consequences.
- [ ] Run new lifecycle tests, all observability tests, full pytest, and the pinned API-container suite; commit implementation with tests.

## Task 6: Local orchestration, failure injection, and operating docs

**Proposed commit:** `test(evaluation): verify local evidence delivery and restart behavior`

Files: modify `docker-compose.yml`, `.env.example`, `Makefile`, `services/aws_emulator/requirements.txt`; create `services/aws_emulator/init_eval_queue.py`, `tests/local_env/test_eval_queue.py`, `test_evidence_delivery_stack.py`; update `local-environment.md` and the Gap 1 progress document.

- [ ] Add Moto SQS support and an idempotently initialized evaluation queue/DLQ using the existing emulator, without renaming the S3 module or weakening its health checks. Give evidence delivery its own worker service, not an API-embedded infinite consumer.
- [ ] Keep host ports loopback-only, dummy AWS credentials, deterministic fake models, and existing ingestion resources untouched. Pin any added packages deliberately.
- [ ] Add opt-in live tests that join authenticated run IDs, terminal metadata, queue messages, and versioned objects when content is approved. Metadata-only runs must explicitly retain empty payload references.
- [ ] In outbox mode, restart the worker with pending committed rows; simulate broker outage and acknowledgement-before-mark crash; prove recovery and deduplication. In best-effort mode, quantify loss rather than expecting recovery.
- [ ] Verify bounded overflow/shutdown and no answer-path quality graders. Test selected retention/deletion helpers, including object versions; cloud enforcement remains a staging check.
- [ ] Run `make local-check`, `make local-smoke`, the new opt-in delivery tests, full pytest, pinned-container tests, and `git diff --check`. Record actual counts, not assumed future totals.
- [ ] Document startup, migrations, delivery guarantee, redaction policy/version, failure counters, quarantine/retry procedures, deletion, and local-versus-cloud limitations; commit only the completed scope.

## Completion gates

- Approved policy and delivery/admission guarantee recorded, with explicit crash-loss boundaries.
- Every supported terminal path attempts admission once; failures are observable and resources bounded.
- Stored payloads are policy-approved and referenced by immutable versions; no raw payloads in telemetry/queue metadata.
- Duplicates are harmless by explicit identity/hash checks; producer and consumer failure handling are distinguished.
- Restart/failure tests prove the chosen guarantee, not a stronger one.
- Production enablement remains off until IAM/KMS, tenant authorization, retention/deletion, load/SLOs, and deployment controls are validated in PR 5.
- Metadata-only delivery does not satisfy full-payload replay; record that partial completion rather than checking all Gap 1 criteria.

## Source-backed transport constraints

SQS standard queues permit duplicate and out-of-order delivery, so application deduplication is required. [AWS SQS standard queues](https://docs.aws.amazon.com/en_gb/AWSSimpleQueueService/latest/SQSDeveloperGuide/standard-queues.html)

S3 uploads can request a dedicated KMS key, and versioned buckets return object version IDs. Verify the exact request contract locally, then permissions/encryption in staging. [S3 PutObject API](https://docs.aws.amazon.com/AmazonS3/latest/API/API_PutObject.html), [SSE-KMS configuration](https://docs.aws.amazon.com/AmazonS3/latest/userguide/specifying-kms-encryption.html)

## Handoff

Review the [written specification](pr-04-payload-publication-design.md) before converting this draft into an executable plan. The selected path is fail-open transactional outbox delivery with encrypted redacted replay content, retained for 30 days, and run metadata retained for 90 days. Upload payloads before outbox commit; no PostgreSQL content staging. Capture failures preserve independently validated safe evidence while explicitly marking unavailable payloads. This does not authorize raw content export or claim audit-grade durability. The specification distinguishes non-durable runtime admission from durable outbox commit; revise the tentative interfaces and remove unselected transport branches only after specification approval. Retain the implementation/test commit boundaries above.
