# Safe Payload Storage and Publication Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Status:** Tasks 1–10 implemented, reviewed, and committed on `feat/evaluation-pr4`; Task 11 (operating docs and final verification) is recorded below. Boxes for Tasks 1–10 are checked against the per-plan execution ledger's spec and quality review verdicts (each task's last verdict was clean after its recorded fix rounds), not re-derived step by step. No push, merge, or cloud action has been performed or authorized.

| Task | Commits (implementation, then review fixes) |
|---|---|
| 1 Contracts and configuration | `b45d1a6`, `a8f599d` |
| 2 Redaction and safe metadata | `d8a48a8`, `735d635` |
| 3 Versioned payload storage | `9a3d633` |
| 4 Migrations and outbox | `0c84b0a`, `aec11eb`, `0cd0676` |
| 5 Publisher and delivery worker | `8fde518`, `d8d7522`, `57e9f04` |
| 6 Capture, tenant, provenance | `4892751`, `f954ab4` |
| 7 Runtime and lifecycle | `6dbbb9d` |
| 8 Retention and operator commands | `712ac78`, `a0cb370` |
| 9 Local orchestration | `3fbcef9` |
| 10 End-to-end acceptance | `18533e5`, `0a3f1af` |

**Goal:** Persist approved redacted evidence and publish committed runs reliably, while preserving answer availability and verifying the full delivery lifecycle locally.

**Architecture:** A bounded API runtime admits terminal evidence without network waits on the response path, uploads redacted payloads, then atomically commits safe metadata and an outbox row. A separate worker publishes committed envelopes with leased retries and explicit deduplication. Retention maintenance coordinates with upload intents and removes expired versions without racing commits.

**Tech Stack:** Existing Python 3.12 API image, FastAPI 0.109.0, Pydantic 2.9.2, SQLAlchemy 2.0.25/asyncpg 0.29.0, PostgreSQL 15, boto3 1.34.34, Moto 5.0.12, pytest, and Docker Compose. Add no GPU/model-provider/grader dependency.

**Spec:** [Approved PR 4 design](pr-04-payload-publication-design.md). Read it fully before executing any task; its privacy and delivery contracts apply to every task.

## Global Constraints

- Default `EVAL_EVIDENCE_ENABLED=false` and `EVAL_CAPTURE_CONTENT=false`; preserve PR 3 schema `1.0`, detached snapshots, and existing NDJSON.
- Fail-open evidence admission; `admitted` is non-durable, `committed` means the outbox transaction succeeded. S3/SQS waits never delay returning/streaming answers.
- Upload encrypted redacted payloads before outbox commit; no raw tier, PostgreSQL prompt staging, queue-only transport, graders, replay executor, Opik, cloud provisioning, push, or deployment.
- Content policy is `restricted-synthetic-v1`, local approved fixtures only. Production content mode is rejected; production metadata enablement requires valid configuration and separate operational approval.
- Capture requires verified server tenant context; JWT adapter may expose a validated optional claim, never body/header/model-derived tenant identity. No new retrieval ACL enforcement is claimed.
- Preserve independently safe partial evidence; unsafe strings, paths, raw identities, credentials, queue bodies, and exception messages never enter export/log/metric labels.
- Content retention is 30 days from object creation; metadata retention is 90 days from run completion. Other artifact/copy retention is not implicitly approved.
- Copy all engineering defaults from specification section 10: 64 KiB/payload; 1 MiB and 64 slots/run; active and pending pools each 32 runs/16 MiB; 2 runtime workers and 2 S3 executor slots; 20-second processing deadline with final 3 seconds reserved for commit; SDK connect/read 1/2 seconds and 2 attempts; 128 KiB queue body; delivery batch 10/concurrency 2; claim lease 60 seconds; 8 producer attempts with 1-second base/60-second cap; shutdown 10 seconds; intent lease 60 seconds/orphan grace 24 hours.
- Reject invalid/non-positive/inconsistent limits and excessive nesting; never silently truncate replay evidence or claim general secret-detection coverage.
- No `create_all` migration strategy for evidence tables; no dropping/recreating chat history. No database locks during remote upload/publish/delete IO.
- One green commit per functionality module; tests and implementation land together. Red tests are run locally, never committed intentionally.
- Preserve unrelated untracked docs: `docs/code-walkthrough.md`, `docs/helm-charts-notes.md`, `docs/kubernetes-notes.md`, `docs/kubernetes-service-conventions.md`.
- If `.codegraph/` exists, use CodeGraph before locating/reading code. Use `apply_patch` for edits. Do not broaden destructive operations beyond isolated test resources.

## Review Focus

1. A signed token without a usable tenant must still allow existing chat behavior but deny all content capture; Task 6 pins this.
2. A secret/path in a known metadata field or approved attribute must not bypass string/value validation; Task 2 pins this.
3. Unapproved retrieval/tool content flattened into a prompt must also deny derived model/answer capture; Task 6 pins this.
4. Cancellation of an await must not free an executor slot while its blocking SDK call runs; Task 3 pins this, Task 7 checks shutdown integration.
5. Orphan cleanup concurrent with an upload/commit must never delete an object that becomes a live reference; Tasks 4 and 8 pin this.

---

## Execution baseline and file map

Base documentation commit: `7c0e89e`; runtime baseline: PR 3 through `a7be2d0`. Record the actual execution HEAD/worktree status before starting; do not reset user changes. Existing shell sandbox may require approved escalation for read-only commands because its loopback setup fails; do not interpret that as a product/test failure.

Run baseline `pytest -q`, `make local-check`, and, if already running, `make local-smoke`. Record counts and actual skip reasons. Do not install real model providers or use cloud credentials to make local tests pass. Live new tests remain explicitly opt-in; execution completion requires running them, not merely observing their skips.

Before every commit, stage explicit owning files, export the index, run the focused and full default suite from that exported snapshot, and inspect the staged diff. This prevents future unstaged modules from making an intermediate commit appear green:

```bash
pr4_check_dir=$(mktemp -d /tmp/pr4-commit-check.XXXXXX)
git checkout-index --all --prefix="$pr4_check_dir/"
cd "$pr4_check_dir"
pytest -q --tb=short
```

Run `git diff --cached --check` and `git diff --cached --stat` back in the original checkout before committing. Live-PG/stack verification uses the same staged source and isolated resources when its owning task requires it. Record the commit and actual results; do not delete or reset the shared worktree to test a boundary.

New production code lives under `services/api/app/evaluation/` except the standalone worker/maintenance CLIs under `services/evaluation_delivery/`. Split policy, provenance, capture buffers, bounded IO, persistence, and delivery by responsibility. Use existing boto3/SQLAlchemy instead of adding another queue/store library.

New isolated tests live under `tests/evaluation_delivery/`; fixtures are local to that subtree. Existing lifecycle/model/retrieval tests remain under `tests/observability/`. Orchestration contracts extend `tests/local_env/`. Production fixture allowlists are not inferred from arbitrary test dictionaries.

### Shared contracts to maintain across tasks

Task 1 owns strict immutable data types: `EventIdentity(export_version, run_schema_version, run_id)`, `PayloadKind`, `PayloadSlot(kind, ordinal, item_ordinal=None)`, `UnavailableReason`, `DeliveryReason`, `RedactedPayload(slot, data, policy_version)`, `ManifestEntry(slot, state, reference=None, reason=None)`, `PayloadRef`, `ObjectVersion`, `SafeRun`, `FixtureRecord`, `ExportPolicy`, `PreparedExport`, `ExportEnvelope`, `DeliveryReceipt`, `PublishReceipt`, `UploadIntent`, `OutboxClaim`, `CapturedRun`, `CleanupClaim`, and `CleanupReport`.

- `RedactedPayload.data` is canonical UTF-8 JSON bytes validated by Task 2; its constructor is not an alternative to policy admission. `PreparedExport` contains safe metadata plus eligible redacted payloads and unavailable slots, never raw content. `CapturedRun` holds the detached original event and its prepared export.
- `PayloadSlot` uses positive ordinal/item ordinal; `model_request`/`model_response` carry invocation ordinal, and the safe metadata model-call projection includes that ordinal. Other slots map to retrieval/tool step ordinals. Input/output use ordinal 1.
- `SafeRun` uses separate strict nested export models; do not inherit the original non-null/hash assumptions or use unrestricted dictionaries. Missing source digests/identities are nullable. Export HMAC digest fields are explicitly named as pseudonyms, not SHA-256 original hashes.
- `ExportEnvelope` carries `export_version='1.0'`, `run_schema_version='1.0'`, server UUID, policy version, safe run projection, manifest, completeness, and canonical `export_hash`. It contains no payload bytes.
- `DeliveryReceipt.status` is `disabled|admitted|dropped|failed|committed`; a fixed reason is optional. `PublishReceipt` carries acknowledgement ID. Claims carry identity/hash, immutable envelope bytes, expiry, and random lease token; upload/cleanup claims carry generated namespace, lease/state, not prompts.
- `UnavailableReason` is exactly the payload reason enum in specification section 6. `DeliveryReason` additionally defines `active_capacity`, `pending_capacity`, `runtime_stopped`, `metadata_invalid`, `metadata_too_large`, `deadline_expired`, `database_unavailable`, `intent_failed`, `broker_transient`, `broker_denied`, `retry_exhausted`, `expired`, `identity_conflict`, and `cleanup_failed`. Reasons/labels are fixed enums, not exception-derived text.
- `ExportPolicy` carries dedicated identity key privately, environment, configured metadata allowlists, `synthetic_sources: tuple[FixtureRecord, ...]`, and resource limits. `FixtureRecord` contains a trusted tenant/case/source kind, source identity/hash, and approved template/corpus identifiers; Task 2 loads these records from the manifest at startup. Keys are excluded from representations/serialization. It does not grant arbitrary source approval or depend on Task 2's loader class.

## Task 1: Delivery contracts and disabled-safe configuration

**Commit:** `feat(evaluation): define evidence delivery contracts and configuration`

**Files:** Create `evaluation/delivery_models.py`, `evaluation/delivery_config.py`, `tests/evaluation_delivery/conftest.py`, `test_contracts.py`, `test_config.py` (production paths are prefixed `services/api/app/`). Modify `services/api/app/config.py` only to expose typed evidence settings.

**Interfaces:** `DeliveryConfig.from_settings(settings: Settings) -> DeliveryConfig`; `canonical_bytes(value: dict) -> bytes`; `ExportEnvelope.canonical_bytes() -> bytes`; all shared contracts above. `Settings` remains backward compatible with existing required DB/cache/security fields.

- [x] Write strict-contract and disabled-default tests before imports succeed. Include literal canonical JSON, unknown/extra fields, naive timestamps, NaN, invalid UUID/ordinal/version, inconsistent stored/unavailable entries, and secret-key representation checks.

```python
def test_canonical_encoding_is_order_independent():
    from services.api.app.evaluation.delivery_models import canonical_bytes
    assert canonical_bytes({'b': 2, 'a': 1}) == b'{"a":1,"b":2}'

def test_disabled_defaults_do_not_require_destinations():
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    cfg = DeliveryConfig()
    assert cfg.enabled is False and cfg.capture_content is False
    assert cfg.payload_bytes == 65536 and cfg.pending_runs == 32
```

- [x] Run `pytest tests/evaluation_delivery/test_contracts.py tests/evaluation_delivery/test_config.py -q`; verify failure is missing/new behavior, not unrelated settings import/network access.
- [x] Define strict frozen Pydantic models with `extra='forbid'`, finite numeric values, UTC timestamps, validated sizes/slot states, and canonical export hashing excluding only `export_hash`. Implement configuration in a separate model, then build it from existing `Settings` during lifespan, not at import-time network startup.

```python
def canonical_bytes(value):
    import json
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')
```

- [x] Pin all specification defaults and conditional validation: enabled metadata requires DB/queue/identity key; capture requires local synthetic mode/manifest/bucket/KMS endpoint configuration; reject identity key equal to JWT secret, unknown content policy, production content mode, deadline reserve >= deadline, and intent/claim leases shorter than their owned processing windows. A disabled runtime does not require/create S3/SQS clients.
- [x] Add a `terminal_event` fixture using `RunEvidenceCollector`, a server UUID, synthetic tenant/user/session, dedicated key, configured fake model names, and `.finish('success')`; fixtures set required settings environment before importing app modules and isolate Qdrant with its embedded client, matching existing observability fixtures. No remote connection during collection.
- [x] Run focused tests, `pytest tests/observability -q`, and `pytest -q`; inspect diff and commit only named files with implementation and tests together.

## Task 2: Restricted redaction and safe metadata projection

**Commit:** `feat(evaluation): redact approved synthetic evidence for export`

**Files:** Create `evaluation/redaction.py`, `evaluation/metadata_export.py`, `evaluation/provenance.py`, `tests/evaluation_delivery/test_redaction.py`, `test_metadata_export.py`, `test_provenance.py`, and `deploy/evaluation/restricted-synthetic-v1.json`; extend delivery test fixtures. Keep one checked-in fixture manifest, shared by tests and the opt-in local stack.

**Interfaces:** `SyntheticManifest.load(path: Path) -> SyntheticManifest`; `SourceProof` and `Provenance` in `provenance.py`; `Provenance.approved(policy: ExportPolicy) -> bool`; `redact_payload(slot: PayloadSlot, value: object, provenance: Provenance, policy: ExportPolicy) -> RedactedPayload | ManifestEntry`; `project_metadata(event: RunEvidence, policy: ExportPolicy, model_ordinals: tuple[int, ...]) -> SafeRun`; `prepare_export(event, policy, payloads: tuple[RedactedPayload, ...], unavailable: tuple[ManifestEntry, ...], model_ordinals=()) -> PreparedExport`; `seal_export(prepared: PreparedExport, entries: tuple[ManifestEntry, ...]) -> ExportEnvelope`.

- [x] Write literal redaction tests for approved synthetic input, nested credential keys, mixed-case/separator-normalized forbidden fields, unknown model message keys/roles, bearer/JWT/AWS tokens, private-key blocks (including unterminated blocks), assignments, email, +phone, overlaps, depth > 8, and payload/run byte limits.

```python
def test_redaction_has_literal_expected_output(policy, approved_input):
    from services.api.app.evaluation.delivery_models import PayloadSlot
    from services.api.app.evaluation.redaction import redact_payload
    out = redact_payload(PayloadSlot(kind='input', ordinal=1),
        {'text': 'Contact alice@example.test; token=fixture-secret'},
        approved_input, policy)
    assert out.data == b'{"text":"Contact [REDACTED_EMAIL]; token=[REDACTED_SECRET]"}'
```

- [x] Define `policy`/`approved_input` fixtures from the checked-in manifest. Manifest fixtures explicitly enumerate synthetic tenant/case text digests, allowed history, template hashes, corpus collection/document/chunk/content identities, and calculator grammar. Include the literal text above as an approved synthetic case; tests adding another case must update the manifest, not silently set `safe=true`.
- [x] Run the three Task 2 test files red. Implement schema selection and source validation before text processing; deny unknown structures/fields and absent tenant. Restrict recursion depth before traversing user-controlled nested values.

```python
# Redaction ordering is part of restricted-synthetic-v1.
for pattern, replacement in (
    (private_key_pattern, '[REDACTED_PRIVATE_KEY]'),
    (secret_pattern, '[REDACTED_SECRET]'),
    (email_pattern, '[REDACTED_EMAIL]'),
    (phone_pattern, '[REDACTED_PHONE]'),
):
    text = pattern.sub(replacement, text)
```

The four compiled patterns are module constants defined in this task: bounded matching for PEM blocks; tested bearer/JWT/AWS token alternatives; email matching bounded to 254 characters; +phone bounded to 8–15 digits. Apply an assignment-label pattern separately with a replacement preserving the label and separator (`token=fixture-secret` becomes `token=[REDACTED_SECRET]`), before generic token substitutions. Define literal patterns and expected fixture outputs in the same policy commit. Detect an unmatched PEM opening before substitutions and return `redaction_failed`. No unbounded nested regex or claim of universal DLP. Calculator accepts at most 100 characters, numeric arithmetic punctuation, whitespace, and `sqrt`; result parses to a finite numeric value, error strings are denied.

- [x] Implement domain-separated tenant HMAC metadata identifiers/source digests. Project only known typed fields, enums, configured fake model/collection names, validated hex build/config hashes, and the collector's per-key dependency attributes; map unknown errors to `OtherError`, arbitrary tool/purpose values to fixed unknown/null. Test secret/path strings in *known* fields and attributes, raw provider IDs, no-tenant hashes, cross-tenant pseudonyms, missing provenance, and unchanged original collector snapshots.
- [x] Define the exported metadata nested schemas in Task 1's models as needed without breaking their public interfaces; re-run contracts. Add `envelope` fixture by projecting `terminal_event`, sealing a metadata-only prepared export, and assert round-trip hash validation rejects tampering.
- [x] Run new policy tests, all delivery/observability tests and full pytest; commit green. Fixture manifests and policy version ship together.

## Task 3: Versioned payload storage and bounded SDK execution

**Commit:** `feat(evaluation): store and resolve versioned redacted payloads`

**Files:** Create `evaluation/bounded_io.py`, `evaluation/payloads.py`, `tests/evaluation_delivery/test_bounded_io.py`, `test_payloads.py`, and `requirements-evaluation-test.txt`. Pin `pytest==9.0.3` (the inspected host version) and `moto[s3,sqs]==5.0.12` (matching the emulator) for repeatable host/container tests. Use `asyncio.run` rather than adding pytest-asyncio. Do not alter API pins without evidence of a missing runtime dependency.

**Interfaces:** `BoundedIO(workers: int)` with `async call(fn, *, deadline: float)` and `async close(deadline: float)`; `PayloadStore.put(run_id: UUID, tenant_partition: str, payload: RedactedPayload, *, deadline: float) -> PayloadRef`; `get(reference: PayloadRef, authorized_tenant: str, *, deadline: float) -> bytes`; `delete_versions(versions: tuple[ObjectVersion, ...], *, deadline: float) -> int`; `list_versions(prefix: str, *, deadline: float) -> tuple[ObjectVersion, ...]`. All PayloadStore operations are async. `ObjectVersion` includes key, version ID, and delete-marker flag so cleanup can delete exact versions; define it in `delivery_models.py` and `S3PayloadStore`/`DisabledPayloadStore` here. Deadlines use monotonic time; creation/expiry use an injected UTC clock.

- [x] Write Moto/Stubber tests for round trips, generated key namespace, exact version ID, dedicated KMS request, wrong/missing tenant before IO, manipulated bucket/key/version/policy, expired reads, hash/size mismatch, failed upload with no reference, and deletion of all versions/delete markers.

```python
def test_wrong_tenant_is_denied_before_io(store, stored_ref, sdk_spy):
    import asyncio
    import pytest
    from services.api.app.evaluation.payloads import PayloadAccessDenied
    with pytest.raises(PayloadAccessDenied):
        asyncio.run(store.get(stored_ref, 'tenant-b', deadline=100.0))
    assert sdk_spy.get_calls == 0
```

Define `PayloadAccessDenied` and content-free `PayloadStorageError` here. Fixture deadlines derive from an injected monotonic clock, not an already expired wall-clock constant; the fixture above uses clock value 0. `store`/`stored_ref`/`sdk_spy` use Moto or a finite fake client owned by the test, not real AWS. Fixture teardown closes executors/clients.

- [x] Write a thread-event test: occupy both executor slots, cancel one waiter, attempt a third call, assert it cannot start until a real SDK call exits. Use bounded test timeouts and release events in `finally`; assert fixed thread counts and no executor backlog growth.
- [x] Run both test files red. Implement a dedicated executor plus a non-unbounded admission semaphore. Release slots on underlying future completion, not waiter cancellation. Socket timeouts/retries remain finite; stop new work at close and report remaining bounded operations.

Install the pinned test requirements into the chosen local/test-container environment during execution (`python -m pip install -r requirements-evaluation-test.txt`); Moto is currently absent on the host. Unit tests are required, not skipped because Moto is missing. Set dummy AWS credentials and `AWS_EC2_METADATA_DISABLED=true` before creating test clients.

```python
# The permit follows the blocking future, not the cancelled await.
future = executor.submit(fn)
future.add_done_callback(lambda _: loop.call_soon_threadsafe(permit.release))
return await asyncio.shield(asyncio.wrap_future(future))
```

Acquire a permit within the caller's remaining deadline before this block; if submission fails synchronously, release it. Use a bounded shutdown path rather than `executor.shutdown(wait=True)` on the event loop.

Completion callbacks must also handle an already-closed loop without emitting a thread exception or raw diagnostics; test a deliberately hung fake that finishes after the close deadline. Retain fixed executor accounting until real future completion, even when the loop no longer owns waiters.

- [x] Request S3 `aws:kms`/dedicated key on every write, require non-empty VersionId, record UTC creation+30-day expiry, validate reference authorization/prefix/expiry before reads, then verify exact bytes/hash. Preserve immutable references across retry/new versions.
- [x] Run Task 3 tests, `pytest tests/evaluation_delivery tests/observability -q`, full pytest, and pinned-container tests when the new test dependencies are available; commit green.

## Task 4: Migrations, upload intents, and transactional outbox

**Commit:** `feat(evaluation): persist safe exports with leased outbox delivery`

**Files:** Create `evaluation/storage_models.py`, `migrations/001_evidence_outbox.sql`, `evaluation/migrate.py`, `evaluation/outbox.py`, `tests/evaluation_delivery/test_migrations.py`, `test_outbox.py`, `test_outbox_postgres.py`; extend test fixtures for an opt-in real local PostgreSQL schema.

**Interfaces:** `async migrate(engine) -> tuple[int, ...]`; CLI `python -m services.api.app.evaluation.migrate`; `OutboxRepository(session_factory, clock)` exposes `begin_upload(run_id, namespace, lease_seconds) -> UploadIntent`, `enqueue(envelope: ExportEnvelope, intent: UploadIntent | None) -> DeliveryReceipt`, `claim_batch(limit, lease_seconds) -> tuple[OutboxClaim, ...]`, `mark_delivered(claim: OutboxClaim, receipt: PublishReceipt) -> bool`, `schedule_retry(claim, error_class: str, *, permanent: bool) -> bool`, `retry_quarantined(identity: EventIdentity) -> bool`, `inspect_cleanup(namespace, now) -> CleanupClaim | None`, `claim_orphan(namespace, now) -> CleanupClaim | None`, `complete_cleanup(claim) -> bool`, `expire_runs(now, limit) -> int`. All repository operations are async. Define `EvidenceConflict` and safe persistence errors here.

- [x] Write real-PG tests for transactional run/outbox/reference insertion and rollback, duplicate acceptance/conflict, expiry unchanged by duplicates, restart recovery, two dispatchers, stale token/lease updates, migration reapplication/checksum conflict/concurrent startup, and cleanup-owned intent denial.

```python
async def duplicate_contract(repo, envelope):
    a = await repo.enqueue(envelope, intent=None)
    b = await repo.enqueue(envelope, intent=None)
    assert a.status == b.status == 'committed'
    claims = await repo.claim_batch(limit=10, lease_seconds=60)
    assert len(claims) == 1
    assert claims[0].envelope_bytes == envelope.canonical_bytes()

def test_duplicate_identity(repo, envelope):
    import asyncio
    asyncio.run(duplicate_contract(repo, envelope))
```

Use `asyncio.run` wrappers or the project's chosen async pytest fixture style consistently. Real-PG fixtures require `RUN_EVAL_POSTGRES_TESTS=1` and `EVAL_TEST_DATABASE_URL`; allocate a generated isolated schema, track the exact name, and drop only that test schema at teardown. Never clear the shared chat/evidence tables. Ordinary pytest skips only these explicit live fixtures; unit tests must not silently switch to mocks when live mode is selected.

- [x] Run unit tests red and the real-PG subset red with local PostgreSQL. Implement independent evidence declarative metadata and SQL migration tracking with advisory lock/checksums; migration SQL creates runs/outbox/references/upload-intents with uniqueness/foreign-key/expiry/claim indexes.

```sql
SELECT id FROM evaluation_outbox
WHERE state IN ('pending', 'leased') AND next_attempt_at <= now()
  AND (lease_until IS NULL OR lease_until <= now())
ORDER BY next_attempt_at, id
FOR UPDATE SKIP LOCKED
LIMIT :limit;
```

Use SQLAlchemy bound parameters in the implementation. The selection and lease-token update share one short transaction; expired leased rows are eligible for reclamation. Acknowledgement/retry predicates include token and lease validity. Run/outbox/ref insertion and intent closure are one transaction; intent commit predicates exclude expired/cleanup-owned states. Store canonical envelope bytes once; never reconstruct changing payloads for retries.

- [x] Implement intent/cleanup exclusion under short transactions; begin_upload conflicts with existing cleanup markers. `claim_orphan` rejects any live intent/reference, inserts an exclusion marker for an absent intent, and returns cleanup ownership before remote deletion. DB unavailable means no cleanup permission.
- [x] Run focused tests, explicitly run real-PG tests, full pytest, and inspect SQL/diff. Commit migrations/repository/tests together; do not mark this module green based solely on mocked transaction tests.

## Task 5: Queue publisher and standalone delivery worker

**Commit:** `feat(evaluation): publish committed exports with restart-safe retries`

**Files:** Create `evaluation/publisher.py`, `services/evaluation_delivery/__init__.py`, `worker.py`, `tests/evaluation_delivery/test_publisher.py`, `test_delivery_worker.py`, and `consumer_fixture.py` (test-only transactional dedup consumer).

**Interfaces:** `RunEvidencePublisher.publish(envelope_bytes: bytes, *, deadline: float) -> PublishReceipt`; `DeliveryWorker(repo: OutboxRepository, publisher: RunEvidencePublisher, clock)` with `async run_once() -> int`, `async run(stop_event)`, `async close(deadline)`; CLI `python -m services.evaluation_delivery.worker`. `consumer_fixture.accept(engine, envelope_bytes) -> str` returns `accepted|duplicate|conflict|expired`, persisting test identities/hashes transactionally in its isolated schema. No real grading consumer is introduced.

- [x] Write tests for exact safe SQS body (no content), envelope/hash/size/expiry validation, safe error classification, denied broker, transient retry/backoff, exhausted/permanent quarantine, two workers, and acknowledgement-before-mark crash.

```python
async def acknowledgement_loss_contract(worker, repo, broker, clock):
    repo.fail_next_ack = True  # Fake repository injection, not a production flag.
    await worker.run_once()
    clock.advance(61)
    await worker.run_once()
    assert broker.bodies[0] == broker.bodies[1]

def test_acknowledgement_loss_redelivers_identical_bytes(worker, repo, broker, clock):
    import asyncio
    asyncio.run(acknowledgement_loss_contract(worker, repo, broker, clock))
```

Define finite repo/broker/clock fakes in this test file, with real-PG equivalents in Task 10. Test consumer accepts identical redelivery only once and rejects identity/hash conflicts; use separate consumer transaction state, not producer status as dedup proof.

- [x] Run tests red. Implement publisher with owned bounded SDK IO, configured SQS URL, 128 KiB body cap, envelope hash revalidation, and no raw logging. Implement worker as claim → finite send → token-checked ack or retry, with no database lock across send.

```python
receipt = await publisher.publish(claim.envelope_bytes, deadline=deadline)
updated = await repo.mark_delivered(claim, receipt)
# False means the lease is stale; it does not authorize a second mutable write.
```

Claim attempts/retry state are persisted; quarantine counts/age are distinct from consumer DLQ. Retry uses 8 attempts, capped jittered exponential delay, finite error classes, and identical bytes/hash. Worker CLI owns resources and handles signals by stopping claims then bounded draining. No Ray/document-ingestion worker reuse.

- [x] Test metadata expiry prevents publish even after a prior failure; expired payload references do not mutate the canonical envelope. Run delivery/observability/full tests; commit implementation with tests.

## Task 6: Bounded capture, trusted tenant, and provenance hooks

**Commit:** `feat(evaluation): capture approved content with tenant-scoped provenance`

**Files:** Create `evaluation/capture.py`, `tests/evaluation_delivery/test_capture.py`, `test_capture_provenance.py`, `test_tenant_context.py`. Modify `auth/jwt.py`, `evaluation/context.py`, `clients/ray_llm.py`, `agents/nodes/planner.py`, `retriever.py`, `tool.py`, `responder.py`; extend existing observability tests for reverse completion order and disabled parity.

**Interfaces:** `CapturePool(config, policy)` provides `open(run_id, tenant, fixture_case) -> CaptureSession | None`; `CaptureSession.record(slot, value, provenance) -> None`, `begin_model(provenance) -> int`, `finish_model(ordinal, metadata_index) -> None`, `detach(event: RunEvidence) -> CapturedRun`, `close() -> None`; `CapturePool` tracks/reserves bytes across sessions. A separate ContextVar carries session/provenance; expose `bind_capture(session, provenance)` context manager. `collector.record_model_call(...)` can return its metadata index without changing schema `1.0`.

- [x] Write tests before hooks: no tenant/invalid tenant denies content yet chat auth succeeds; forged request tenant ignored; signed local tenants differ; unknown/unapproved source denies flattened model request and derived model/answer output; independent safe input survives. Also test no capture allocation when disabled.

```python
def test_unapproved_context_denies_derived_capture(session, unknown_source):
    from services.api.app.evaluation.delivery_models import PayloadSlot
    session.record(PayloadSlot(kind='model_request', ordinal=1),
        {'messages': [{'role': 'user', 'content': 'unapproved context'}],
         'model': 'fake-local-llm', 'temperature': 0.0, 'max_tokens': 10},
        unknown_source)
    assert session.unavailable[0].reason == 'unapproved_source'
```

Expose immutable inspection properties `CaptureSession.unavailable` and `resident_bytes` for tests; no raw debug buffers. Add active run/count/byte, payload slot/count, oversize, nested structure, double detach, and late update tests. Reverse two model completions and assert request/response slots still point to the correct metadata indexes.

- [x] Run tests red. Extract optional valid JWT tenant using type/length/identifier validation after signature verification. Malformed optional tenant becomes null; no request authorization change, default tenant, or JWT key reuse.

```python
tenant = payload.get('tenant_id')
tenant = tenant if isinstance(tenant, str) and tenant_pattern.fullmatch(tenant) else None
# Add tenant_id to the existing verified user context; never inspect request.body.
```

Define `tenant_pattern` as a bounded opaque identifier pattern in the JWT adapter; do not expose arbitrary text as an identifier. Tests assert optional bad tenant does not turn an otherwise valid token into a capture-authorized identity.

- [x] Implement byte reservation *before* adding redacted copies, slot limit 64, 64 KiB payload/1 MiB run caps, independent global active cap, immediate redaction, prompt provenance propagation, and no raw sidecar copies. Release reservations exactly once at detach/close. Approved fixture selection comes from runtime manifest configuration, not user content flags.
- [x] At model invocation allocate ordinal before awaiting IO; capture actual messages/settings and successful response text, with model-call metadata index mapping at completion. Retrieval captures approved query/ranked items before formatting; responder checks context/template/filename proofs before assembling capture provenance. Denied context taints derived output; ordinary user response remains unchanged.
- [x] Capture only policy-approved calculator IO; all other tools produce explicit unavailable slots while preserving safe trajectory. Do not instrument unknown error text as content. Preserve PR 3 hashes/snapshots and observation ordering.
- [x] Run capture tests, all observability/model/retrieval/agent tests, full pytest, and pinned-container suite; commit hooks/policy tests together. No route/runtime enablement is required for these DI-tested hooks to be independently green.

## Task 7: Non-blocking terminal admission and lifecycle integration

**Commit:** `feat(api): persist terminal evidence through a bounded fail-open runtime`

**Files:** Create `evaluation/runtime.py`, `evaluation/delivery_metrics.py`, `tests/evaluation_delivery/test_runtime.py`, `test_runtime_metrics.py`, `tests/observability/test_evidence_delivery_lifecycle.py`; modify `routes/chat.py`, `services/api/main.py`, and lifespan tests. Metrics live in the focused evaluation module rather than expanding unrelated legacy request counters.

**Interfaces:** `EvidenceRuntime(config, policy, repo, store)` exposes `async start()`, `open_capture(run_id, tenant) -> CaptureSession | None`, `submit(captured: CapturedRun) -> DeliveryReceipt` (synchronous, no IO), `async close(deadline: float)`, `pending_count`, `pending_bytes`; internal `async process_one(captured) -> DeliveryReceipt`. `DeliveryMetrics(registry)` exposes `inc(component, outcome, reason)`, `pending(count, bytes, age)`, `quarantine(count, age)`, `observe(component, seconds, bytes)` with closed label validation.

- [x] Write tests for non-durable admission versus durable commit, pending count/bytes overflow, disabled runtime/no client allocation, partial uploads, failed intent creation, metadata-only fallback within the reserved deadline, commit failure/orphans, buffer release, shutdown, and fixed SDK/task counts.

```python
def test_submit_has_no_network_wait(runtime, captured_run, repo_spy, store_spy):
    receipt = runtime.submit(captured_run)
    assert receipt.status == 'admitted'
    assert repo_spy.calls == store_spy.calls == []
```

`captured_run` fixture constructs `CapturedRun` from Task 6 session detach; fake clients use asyncio events to prove the answer/header can be sent while store/DB remain blocked. Do not assert wall-clock microseconds as the only non-blocking evidence.

- [x] Run tests red. Implement a byte/item-bounded queue and exactly two lifespan-owned processing tasks. Transfer buffers without copying; reject overflow with fixed loss reason and release owned reservations. Stop admission before bounded close; pending dropped work is counted, committed work belongs to outbox recovery.

```python
# Within process_one, never inside the response generator's send path.
intent = await repo.begin_upload(run_id, namespace, lease_seconds=60)
# Upload allowed payloads until the content deadline; failures become entries.
envelope = seal_export(captured.prepared, tuple(entries))
receipt = await repo.enqueue(envelope, intent=intent)
```

Generate namespace from verified tenant partition/run UUID; initialize explicit entries from prepared unavailable slots and map successful writes by slot. Use monotonic deadline `start+20`, stop content work at `start+17`, reserve DB commit time, and deny late/expired intent commits. Exceptions become fixed metadata/metric reasons, not request errors. `process_one` returns `committed` only after the DB transaction.

- [x] Integrate optional capture/session into the existing request collector configuration, attach ContextVars only during generator advancement/close, and detach/submit exactly once on normal/cache/error/timeout/cancel/header/body-close paths. Add no await to synchronous finalizer. In ASGI close/finally, the idempotent terminal helper finalizes then immediately submits; a lifecycle guard owns release on pre-body disconnect.
- [x] Start/close runtime in FastAPI lifespan and clean up partially started resources if startup fails. Existing request clients/chat history behavior remains unchanged; evidence schema migrations must already be applied. Default disabled mode allocates none of the new IO resources. Test slow upload/DB does not postpone answer delivery and runtime failure does not alter agent status.
- [x] Add attempts/omissions/drops/committed/upload/retry/delivered/quarantine/cleanup counters, pending/oldest-age/quarantine gauges, and processing/payload-byte histograms with only fixed component/outcome/reason labels. Use custom `CollectorRegistry` in tests; assert no run/tenant/user/path/secret labels or unsafe log messages. Worker/maintenance use the same metric API; no external metrics platform is required.

Add optional injected `DeliveryMetrics` to the Task 5 publisher/worker in this commit, preserving existing constructor call sites when metrics are absent. Add metric tests for publish/retry/quarantine here; do not make Task 5 import a Task 7 file before that file exists. Task 8 maintenance receives the same optional metrics dependency.
- [x] Run runtime/lifecycle/lifespan tests, observability suite, full pytest, pinned API-container suite; commit green.

## Task 8: Retention maintenance and safe operator commands

**Commit:** `feat(evaluation): expire evidence and coordinate orphan cleanup`

**Files:** Create `evaluation/retention.py`, `services/evaluation_delivery/maintenance.py`, `tests/evaluation_delivery/test_retention.py`, `test_maintenance_cli.py`; extend repository/storage/worker tests for cleanup/rerun coordination.

**Interfaces:** `RetentionService(repo, store, clock).run_once(*, apply: bool, limit: int) -> CleanupReport`; maintenance CLI `python -m services.evaluation_delivery.maintenance cleanup --dry-run|--apply --limit N` and `retry --run-id UUID --export-version 1.0 --run-schema-version 1.0`. Neither command emits raw content; retry preserves stored bytes/hash and cannot revive expired metadata.

- [x] Write tests for reads at exactly the 30-day expiry, metadata at exactly 90 days, duplicate admission not extending retention, all versions/delete markers, failed delete/retry, empty bucket, paginated bounded work, active upload/commit race, absent intent marker, database unavailable, and dry-run no mutation.

```python
async def cleanup_race_contract(repo, retention, intent, envelope):
    report = await retention.run_once(apply=True, limit=10)
    assert report.deleted_versions == 0  # Live upload intent protects objects.
    receipt = await repo.enqueue(envelope, intent=intent)
    assert receipt.status == 'committed'

def test_active_intent_prevents_orphan_delete(repo, retention, intent, envelope):
    import asyncio
    asyncio.run(cleanup_race_contract(repo, retention, intent, envelope))
```

Also test expired intent → cleanup claim → attempted enqueue is denied, then delete resumes after process restart. Use injected UTC clock and explicit synthetic namespaces; no recursive filesystem deletion or shared bucket-wide clearing.

- [x] Run tests red. Implement time checks using UTC creation/completion+approved durations, resolver fail-closed expiry, and safe orphan eligibility (24-hour grace, no live reference/intent, cleanup marker committed before IO). Delete remote versions outside DB locks; record completion afterwards. If DB coordination fails, defer and count failure.

```python
claim = await repo.claim_orphan(namespace, now)
if claim is not None and apply:
    versions = await store.list_versions(claim.namespace, deadline=deadline)
    deleted = await store.delete_versions(versions, deadline=deadline)
    await repo.complete_cleanup(claim)
```

Only complete when every claimed version was resolved; partial deletion is resumable and not falsely completed. Dry-run uses read-only eligibility queries, not `claim_orphan`. Add read-only repository `inspect_cleanup(namespace, now) -> CleanupClaim | None` to support dry-run. Published metadata remains immutable; expiry does not change envelope hashes. Metadata expiry removes associated outbox/references even if quarantined.

- [x] Add explicit retry CLI with UUID/version validation and fixed safe output counts/status. Test conflicting identity, stale lease, missing/expired row, and no automatic requeue. Queue/trace/backup/dataset retention stays outside this command's scope.
- [x] Run retention/CLI/repository tests including live PostgreSQL, all delivery/observability/full suites; commit only after the race and dry-run tests pass.

## Task 9: Local orchestration and queue initialization

**Commit:** `feat(local): orchestrate evidence migrations and delivery workers`

**Files:** Modify `docker-compose.yml`, `.env.evidence`, `Makefile`, `services/aws_emulator/requirements.txt`, `services/aws_emulator/entrypoint.sh`; create `services/aws_emulator/init_eval_queue.py`, `docker-compose.evaluation.yml`, `tests/local_env/test_eval_queue.py`, and extend `test_local_configuration.py`/`test_aws_emulator.py`. The inspected entrypoint starts Moto then runs `init_s3`; keep that sequence and add evaluation queue initialization after S3 readiness. Mount the single synthetic fixture manifest created in Task 2; do not copy arbitrary private test data into images.

**Interfaces:** `init_eval_queue.ensure_queues(client, queue_name, dlq_name) -> dict[str, str]`; module CLI prints safe queue initialization status. Make targets `local-evidence-up`, `local-evidence-down`, `local-evidence-check`, `local-evidence-smoke`. Existing `local-up/check/smoke` contracts remain valid.

- [x] Write emulator and Compose contracts for idempotent queue/DLQ creation/redrive configuration, explicit SQS installation, loopback ports, dummy credentials, migrations-before-start, separate worker command, synthetic manifest mount, evidence flags off by default, and unchanged document ingestion resources.

```python
def test_queue_initialization_is_idempotent(sqs):
    from services.aws_emulator.init_eval_queue import ensure_queues
    first = ensure_queues(sqs, 'eval-runs', 'eval-runs-dlq')
    assert ensure_queues(sqs, 'eval-runs', 'eval-runs-dlq') == first
    attrs = sqs.get_queue_attributes(QueueUrl=first['queue_url'],
                                    AttributeNames=['RedrivePolicy'])['Attributes']
    assert 'RedrivePolicy' in attrs
```

`sqs` fixture uses Moto 5.0.12, not a cloud client. Local queue retention uses emulator defaults solely for transport testing; no production queue-retention policy is implied. Keep the existing S3 bucket/version healthcheck intact, and add queue readiness for delivery startup without renaming the S3 service.

- [x] Run tests red. Pin `moto[server,s3,sqs]==5.0.12` in the emulator. Implement idempotent queue/DLQ initializer, finite client waits, and safe status outputs. Update only the existing startup owner after locating it; do not invent a second competing initializer.
- [x] Add an opt-in Compose override reusing API image for migration/worker, with service-completed-successfully dependency for migration, DB/emulator readiness, configured local queue URL, fake-model endpoints, dedicated dummy identity key and KMS request key. No test override sends real AWS credentials or selects production endpoints.

```yaml
evidence-migrate:
  command: ["python", "-m", "services.api.app.evaluation.migrate"]
evidence-delivery:
  command: ["python", "-m", "services.evaluation_delivery.worker"]
  depends_on:
    evidence-migrate:
      condition: service_completed_successfully
```

Complete service build/env/profiles by reusing the existing API build context and settings; do not duplicate model services. Enable flags only in the opt-in override. Mount `deploy/evaluation/restricted-synthetic-v1.json` into API/worker; generate hashes from the approved synthetic fixtures/templates, validate synchronization with Task 2 fixture tests.
- [x] Add Make commands using the existing project/env/profile plus `-f docker-compose.yml -f docker-compose.evaluation.yml`. Down must not remove persistent volumes. Add finite wait/health checks; do not make the default local-check depend on cloud or a running stack.
- [x] Run focused local contracts, both Compose configurations with `config --quiet`, `make local-check`, full pytest, rebuild/start opt-in stack, and existing local-smoke. Commit orchestration/tests green before acceptance-test commit.

## Task 10: End-to-end acceptance and fault injection

**Commit:** `test(evaluation): verify durable local evidence delivery and expiry`

**Files:** Create `tests/evaluation_delivery/test_delivery_acceptance.py`, `test_restart_acceptance.py`, `test_retention_acceptance.py`; extend tests-only consumer fixture and signed-token fixtures. No product-only debug routes or failure switches.

**Interfaces:** Opt-in `RUN_EVAL_DELIVERY_TESTS=1`; local API URL and test DB/S3/SQS endpoints supplied explicitly. Live fixtures fail when requested services are unreachable; they do not silently skip after opt-in. Synthetic tokens carry trusted tenant IDs and cases selected by local manifest, not body flags.

- [x] Write acceptance tests against the spec before enabling fault scenarios: join `X-Run-ID`, immutable safe run/outbox bytes, SQS envelope identity/hash, and exact versioned payload objects. Assert original NDJSON answer unchanged, sensitive sentinel removed only from evidence, tenant-B read denied, and expected completeness.

```python
def test_live_delivery_links_stored_versions(live_run):
    assert live_run.header_run_id == live_run.envelope.run_id
    assert live_run.db_export_bytes == live_run.queue_body
    assert live_run.envelope.completeness == 'complete'
    assert all(ref.version_id for ref in live_run.stored_references)
    assert b'fixture-secret' not in live_run.exported_payload_bytes
```

`live_run` fixture posts an approved signed synthetic case, polls with a fixed deadline, reads producer state through a read-only test DB connection, consumes only its unique test queue namespace, and resolves objects with an authorized tenant. Do not drain another developer's queue or overwrite existing corpus fixtures.

- [x] Run new live tests before expected integration is complete and verify the observed failure. Make only scoped integration fixes supported by those failures; any spec change goes back to review.
- [x] Add real-PG/worker restart with pending committed records and broker outage. Inject acknowledgement-before-mark failure through a test-owned worker/repository wrapper, not a production flag; prove identical redelivery and transactional test-consumer dedup/conflict. Use container stop/start only for dedicated local evaluation worker, preserving DB volumes and ingestion services.
- [x] Add partial upload/DB admission failure, header/body disconnect, buffer overflow, slow SDK/shutdown, missing tenant/unapproved flattened source, failed delete/resume, and controlled-clock expiry acceptance. Clock injection is DI in test-owned runtime/maintenance components; do not add an API clock-control endpoint.
- [x] Run `RUN_EVAL_POSTGRES_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://ragadmin:changeme@127.0.0.1:15432/rag_db pytest tests/evaluation_delivery/test_outbox_postgres.py tests/evaluation_delivery/test_migrations.py -q` using the isolated schemas; the port/credentials match the inspected `.env.evidence`. Then run `make local-evidence-smoke`, `make local-smoke`, all delivery/observability tests and full pytest. Record exact counts/skips and which failures were exercised; no guessed future totals.
- [x] Run pinned API-container tests with a read-only checkout test mount and explicitly installed pinned test requirements; match the existing PR 3 verification approach. Inspect no unsafe logs/metric labels; commit acceptance tests and scoped fixes green.

## Task 11: Operating docs and final verification

**Commit:** `docs(evaluation): document safe evidence delivery and local verification`

**Files:** Update `docs/evaluation_implementation/local-environment.md`, `gap-01-production-run-instrumentation.md`, this plan, and create `docs/evaluation_implementation/evidence-delivery-operations.md`. Do not edit unrelated docs.

- [x] Document disabled/default vs opt-in startup, signed synthetic tenant fixtures, configuration/limits, migration command, `admitted`/`committed` distinction, pre-commit loss, post-commit duplicate recovery/quarantine, exact retention/cleanup/retry commands, metric names, and resolver authorization. Explain synthetic policy limits and cloud IAM/KMS/retention/SLO gates; no audit-grade or universal-DLP claim.
- [x] Provide tested operator command examples, using actual local endpoints/environment established in Task 9:

```bash
make local-evidence-up
make local-evidence-check
make local-evidence-smoke
docker compose -p scalable-rag-eval --profile eval-local --env-file .env.evidence -f docker-compose.yml -f docker-compose.evaluation.yml run --rm evidence-delivery python -m services.evaluation_delivery.maintenance cleanup --dry-run --limit 100
```

The apply/retry commands require explicit targets; never present bucket/database-wide resets as cleanup. Specify that queue/trace/backups/datasets need their own production retention policies.
- [x] Re-run full pytest, live PostgreSQL tests, default/opt-in local-check/smoke, live delivery/restart/retention acceptance, and pinned-container observability/delivery suite; run `git diff --check`. Record counts, skip reasons, exact image/commit, and local-versus-cloud evidence.
- [x] Perform final review using the user-selected execution method. Resolve correctness/security findings before claiming completion; no new authority for push/cloud actions follows from this plan.
- [x] Update checkboxes only for steps actually completed. Mark Gap 1's PR 4 local milestones, not PR 5 production gates, and explicitly describe incomplete replay provenance/cached cases. Commit docs separately and report the next production-hardening task.

**Task 11 outcome (2026-09-28):** an independent whole-branch review found no Critical issues and one Important issue: every committed envelope carried the collector's legacy `payloads_not_stored` tag even when payload references were stored. It was fixed with a failing test first (the payload manifest is authoritative), and all verification was re-run afterwards. The reviewer's Minor findings (redaction gaps for derived-provenance slots, per-namespace orphan cleanup, unmetered per-slot omissions, retention-setting checks, worker quarantine counting, startup validation while disabled) are documented in the operations guide and parked in the backlog, not fixed. The metrics gaps (no scrape endpoint, worker/maintenance not instrumented, quarantine gauges never set) are likewise documented as open. Operator commands were exercised in a disposable Compose project, not the shared local stack. Next production-hardening task: PR 5, starting with the metrics path and the redaction and provenance gaps above.

## Self-review coverage and execution handoff

Spec coverage: sections 1–3 → scope/file map/Tasks 1, 6, 7, 9; section 4 → Tasks 4, 7; section 5 → Tasks 1–3, 6; sections 6–7 → Tasks 2, 6; section 8 → Task 3; section 9 → Tasks 4–5; section 10 → Tasks 1, 3, 6–7; section 11 → Tasks 3–4, 8; section 12 → Tasks 7, 9–10; section 13 → Tasks 10–11. All five Review Focus cases have owning tests. No unselected transport branch remains.

The user approved this plan and selected **subagent-driven execution**: fresh implementation/review contexts per module plus final review. Follow the skill's per-plan ledger, preflight interface scan, sequential implementation dispatch, and specification/code-quality review gates. Resolve workspace isolation and verify the baseline before starting Task 1. Implementation approval does not authorize cloud deployment, a push, or merging a new branch into `dev`.
