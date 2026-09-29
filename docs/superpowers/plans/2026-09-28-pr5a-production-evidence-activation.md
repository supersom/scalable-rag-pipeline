# PR 5a: Production Evidence Delivery Activation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Take PR 4's locally-verified metadata-only evidence delivery to a state that can run safely in production — real IAM/KMS, a staging environment to prove it, working metrics, and a drain consumer so the queue never silently backs up before Phase 3's evaluation workers exist.

**Architecture:** New Terraform resources (KMS key, S3 archive bucket, SQS queue/DLQ, two IRSA roles) back a new drain-consumer service and the existing worker/maintenance components, deployed as plain Kubernetes manifests mirroring the existing ingestion-worker pattern. A parallel, separate-state staging environment (its own small RDS instance, its own copy of the same evidence resources, no GPU) proves the whole path against real AWS before production is touched. Production activation is dark-launched: code ships with delivery disabled, then a separate scoped change flips `EVAL_EVIDENCE_ENABLED`.

**Tech Stack:** Existing Python 3.12 API image, FastAPI, SQLAlchemy/asyncpg, boto3, Terraform ~1.5, `terraform-aws-modules` (iam, vpc — reused via data lookups, not reapplied), Helm 3, kubectl, pytest, Moto (offline tests only).

**Spec:** [PR 5 production evidence design](../../evaluation_implementation/pr-05-production-evidence-design.md), Sections 1–3 and 5–7. Section 4 (PR 5b) is explicitly out of scope for this plan.

## Global Constraints

- **5a only.** No content-capture code, no `production-redacted-v1` policy, no redaction changes. `EVAL_CAPTURE_CONTENT` stays `false` everywhere this plan touches.
- **No new production database.** Evidence migrations run against the existing chat-history PostgreSQL instance (`var.db_tier`, Aurora in prod). Only the new staging environment gets its own database, and it uses `db_tier = "rds"` (`db.t3.micro`), not Aurora — staging only needs to prove PostgreSQL transaction semantics, not HA.
- **The drain consumer is a placeholder**, not Phase 3's real evaluation-worker consumer. It archives to `queue-archive/`, nothing more.
- **Dark-launch discipline.** Every production deploy in this plan ships with `EVAL_EVIDENCE_ENABLED=false`. Flipping it to `true` is a separate, explicit final task, not bundled into a deploy.
- **No infrastructure command runs against real AWS without a live, explicit go-ahead at execution time.** `terraform apply`, `kubectl apply` against a real cluster, and any Helm release against a real cluster all require the user's confirmation immediately before that specific command, per standing project practice — a plan approval is not that confirmation. Tasks 7–14 name the exact commands; none are auto-run.
- **Never echo credentials, secrets, or exception text.** Matches every existing CLI in this codebase (`maintenance.py`, `worker.py`); the drain-consumer CLI follows the same rule.
- **`EVAL_IDENTITY_HASH_KEY` is generated the same way `JWT_SECRET_KEY` already is** (`openssl rand -hex 32`, manually placed into `app-env-secret`), not provisioned by Terraform — matching the existing stage-11 pattern in `scripts/bootstrap_cluster.sh`.

## Review Focus

- A malformed or undecodable SQS message reaching the drain consumer (bug upstream, or queue tampering) must not crash the poll loop or delete the message before it's safely redriven to the DLQ. Owned by Task 6.
- Identical redelivery of the same `run_id` with byte-identical content must never be recorded as a conflict, and a genuinely different payload for the same `run_id` must never silently overwrite the first archived copy. Owned by Task 5.
- The evidence bucket's KMS-enforcement policy must be proven to actually reject an unauthorized or non-KMS write against real AWS, not just read as correct from the policy JSON — Moto cannot exercise this. Owned by Task 13.
- The worker, publisher, and drain consumer all calling `default_metrics()` in one process must not raise a duplicate-timeseries registration error. Owned by Task 2.
- The maintenance CronJob must never run two overlapping `cleanup --apply` invocations if one run overruns its schedule. Owned by Task 9.

---

## Task 1: Quarantine summary query

**Files:**
- Modify: `services/api/app/evaluation/outbox.py`
- Test: `tests/evaluation_delivery/test_outbox_postgres.py`

**Interfaces:**
- Produces: `OutboxRepository.quarantine_summary(self, now: datetime) -> tuple[int, float]` — `(count, oldest_age_seconds)` for rows in `state = 'quarantined'`; `(0, 0.0)` when none.

- [ ] **Step 1: Write the failing test**

Add near the other retention-adjacent tests in `tests/evaluation_delivery/test_outbox_postgres.py` (after `test_count_expired_runs_is_bounded_read_only_and_exact_at_expiry`):

```python
def test_quarantine_summary_reports_count_and_oldest_age(repo_factory, pg_engine, envelope, clock):
    async def scenario():
        repo = repo_factory(max_attempts=1)
        assert await repo.quarantine_summary(clock()) == (0, 0.0)
        await repo.enqueue(envelope, intent=None)
        [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "broker_denied", permanent=True) is True
        assert await rows(pg_engine, "SELECT state FROM evaluation_outbox") == [("quarantined",)]
        clock.advance(seconds=90)
        count, age = await repo.quarantine_summary(clock())
        assert count == 1
        assert 89 <= age <= 91
    asyncio.run(scenario())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `RUN_EVAL_POSTGRES_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://ragadmin:changeme@127.0.0.1:15432/rag_db pytest tests/evaluation_delivery/test_outbox_postgres.py::test_quarantine_summary_reports_count_and_oldest_age -v`
Expected: FAIL with `AttributeError: 'OutboxRepository' object has no attribute 'quarantine_summary'`

- [ ] **Step 3: Implement the query**

In `services/api/app/evaluation/outbox.py`, add near `count_expired_runs` (after its closing line, still inside the `# --- retention ---` section):

```python
    async def quarantine_summary(self, now: datetime) -> tuple[int, float]:
        """Read-only count and oldest age (seconds) of currently quarantined outbox rows."""
        async with self._transaction() as session:
            result = await session.execute(text(
                "SELECT count(*), min(created_at) FROM evaluation_outbox WHERE state = 'quarantined'"))
            row = result.one()
        count = row[0] or 0
        if count == 0 or row[1] is None:
            return 0, 0.0
        return count, max(0.0, (_utc(now) - _utc(row[1])).total_seconds())
```

Check the file's existing imports already include `text` from `sqlalchemy` (used by `_COUNT_EXPIRED_SQL` etc.) and `_utc` is already defined in this module — both are, so no new imports are needed.

- [ ] **Step 4: Run test to verify it passes**

Run: `RUN_EVAL_POSTGRES_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://ragadmin:changeme@127.0.0.1:15432/rag_db pytest tests/evaluation_delivery/test_outbox_postgres.py::test_quarantine_summary_reports_count_and_oldest_age -v`
Expected: PASS

- [ ] **Step 5: Run the full offline and PG suites to confirm no regression**

Run: `pytest tests/evaluation_delivery -q` then `RUN_EVAL_POSTGRES_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://ragadmin:changeme@127.0.0.1:15432/rag_db pytest tests/evaluation_delivery/test_outbox_postgres.py -q`
Expected: both green, no new failures

- [ ] **Step 6: Commit**

```bash
git add services/api/app/evaluation/outbox.py tests/evaluation_delivery/test_outbox_postgres.py
git commit -m "feat(evaluation): add a read-only quarantine count/age query"
```

---

## Task 2: Wire metrics into the delivery worker

**Files:**
- Modify: `services/evaluation_delivery/worker.py`
- Modify: `tests/evaluation_delivery/test_delivery_worker.py`

**Interfaces:**
- Consumes: `OutboxRepository.quarantine_summary(now) -> tuple[int, float]` (Task 1), `DeliveryMetrics.quarantine(count, age)` (existing), `DeliveryMetrics.default_metrics()` (existing).
- Produces: `DeliveryWorker` now polls quarantine state once per `_cycle()` when metrics are attached; `main()`'s `serve()` constructs and passes a `DeliveryMetrics` instance to both the publisher and the worker.

- [ ] **Step 1: Add `quarantine_summary` to the in-memory `FakeRepo`**

In `tests/evaluation_delivery/test_delivery_worker.py`, inside `class FakeRepo`, add a method (near `_owned` or any other read helper):

```python
    async def quarantine_summary(self, now):
        quarantined = [r for r in self.rows if r.state == "quarantined"]
        if not quarantined:
            return 0, 0.0
        oldest = min(r.next_attempt_at for r in quarantined)
        return len(quarantined), max(0.0, (now - oldest).total_seconds())
```

- [ ] **Step 2: Write the failing test**

Add to `tests/evaluation_delivery/test_delivery_worker.py`, near the other metrics-adjacent tests (after `test_close_waits_for_in_flight_sends_that_finish_in_time` or any convenient point):

```python
def test_quarantine_gauges_update_once_per_cycle(repo, clock, envelope):
    from prometheus_client import CollectorRegistry
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    registry = CollectorRegistry()
    metrics = DeliveryMetrics(registry)
    repo.add(envelope, expires_at=clock() + timedelta(days=1))
    [claim] = repo_claim_sync(repo, clock)
    asyncio.run(repo.schedule_retry(claim, "broker_denied", permanent=True))
    worker = DeliveryWorker(repo, FakeBroker(), clock, batch=1, metrics=metrics)
    asyncio.run(worker.run_once())
    assert registry.get_sample_value("evidence_quarantine_records") == 1
    assert registry.get_sample_value("evidence_quarantine_oldest_age_seconds") is not None
```

This test needs a small sync helper for the claim, since `repo.claim_batch` is a coroutine and the rest of the test body is sync for clarity. Add it right above the test:

```python
def repo_claim_sync(repo, clock):
    return asyncio.run(repo.claim_batch(limit=1, lease_seconds=60))
```

- [ ] **Step 3: Run test to verify it fails**

Run: `pytest tests/evaluation_delivery/test_delivery_worker.py::test_quarantine_gauges_update_once_per_cycle -v`
Expected: FAIL — `registry.get_sample_value("evidence_quarantine_records")` is `None` (gauge never touched)

- [ ] **Step 4: Poll quarantine state once per cycle**

In `services/evaluation_delivery/worker.py`, modify `_cycle`:

```python
    async def _cycle(self) -> int:
        try:
            claims = await self._repo.claim_batch(self._batch, self._lease_seconds)
        except EvidencePersistenceError:
            logger.warning("evidence delivery claim failed reason=%s", DeliveryReason.database_unavailable.value)
            self._count("failed", DeliveryReason.database_unavailable)
            return 0
        if self._metrics is not None:
            try:
                count, age = await self._repo.quarantine_summary(self._clock())
                self._metrics.quarantine(count, age)
            except EvidencePersistenceError:
                logger.warning("evidence delivery quarantine summary failed reason=%s",
                               DeliveryReason.database_unavailable.value)
        slots = asyncio.Semaphore(self._send_workers)
```

(The rest of `_cycle` is unchanged — `slots = ...` onward stays exactly as it is today.)

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/evaluation_delivery/test_delivery_worker.py::test_quarantine_gauges_update_once_per_cycle -v`
Expected: PASS

- [ ] **Step 6: Wire `default_metrics()` into `main()`/`serve()`**

In `services/evaluation_delivery/worker.py`, modify `serve()` and `main()`:

```python
async def serve(config, publisher) -> int:
    """Own the engine, repository and worker; always drain, unregister signals and dispose on exit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from services.api.app.evaluation.delivery_metrics import default_metrics
    from services.api.app.evaluation.outbox import OutboxRepository

    loop = asyncio.get_running_loop()
    engine = None
    worker = None
    metrics = default_metrics()
    try:
        engine = create_async_engine(config.database_url, pool_size=config.send_workers + 1, pool_pre_ping=True)
        repo = OutboxRepository(
            async_sessionmaker(engine, expire_on_commit=False), utc_now, max_attempts=config.producer_attempts,
            backoff_base_seconds=config.backoff_base_seconds, backoff_cap_seconds=config.backoff_cap_seconds,
            metadata_retention_days=config.metadata_retention_days,
            content_retention_days=config.content_retention_days, orphan_grace_seconds=config.orphan_grace_seconds)
        worker = DeliveryWorker(
            repo, publisher, utc_now, batch=config.delivery_batch, send_workers=config.send_workers,
            lease_seconds=config.claim_lease_seconds, ack_reserve_seconds=config.ack_reserve_seconds,
            send_timeout_seconds=config.sdk_attempts * (config.sdk_connect_timeout_seconds
                                                        + config.sdk_read_timeout_seconds), metrics=metrics)
```

(Only the `from ... import default_metrics`, the `metrics = default_metrics()` line, and the trailing `, metrics=metrics)` on the `DeliveryWorker(...)` call are new — everything else in `serve()` is unchanged.)

```python
def main() -> int:
    ...
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    from services.api.app.evaluation.delivery_metrics import default_metrics
    from services.api.app.evaluation.publisher import RunEvidencePublisher
    try:
        from services.api.app.config import Settings
        config = DeliveryConfig.from_settings(Settings())
        publisher = RunEvidencePublisher.from_config(config, metrics=default_metrics()) if config.enabled else None
```

(Only the `from ... import default_metrics` and the added `, metrics=default_metrics()` on the `RunEvidencePublisher.from_config(...)` call change — the rest of `main()` is unchanged.)

- [ ] **Step 7: Run the full delivery worker test file to confirm no regression**

Run: `pytest tests/evaluation_delivery/test_delivery_worker.py -q`
Expected: all passing (no duplicate-timeseries error — `default_metrics()` is a process-wide singleton guarded by a lock, so calling it twice in `main()` and once more later in this same process is safe; confirm by also running the full suite once)

Run: `pytest tests/evaluation_delivery -q`
Expected: all green, no `ValueError: Duplicated timeseries`

- [ ] **Step 8: Commit**

```bash
git add services/evaluation_delivery/worker.py tests/evaluation_delivery/test_delivery_worker.py
git commit -m "feat(evaluation): record delivery worker and quarantine metrics"
```

---

## Task 3: Wire metrics into the maintenance CLI

**Files:**
- Modify: `services/evaluation_delivery/maintenance.py`
- Modify: `tests/evaluation_delivery/test_maintenance_cli.py`

**Interfaces:**
- Consumes: `DeliveryMetrics.default_metrics()` (existing), `RetentionService.__init__(repo, store, clock, *, metrics=None, ...)` (existing, already accepts `metrics`).
- Produces: `open_configured_resources` (production path, `open_resources=None`) now always constructs a `RetentionService` with `metrics=default_metrics()`.

- [ ] **Step 1: Write the failing test**

Add to `tests/evaluation_delivery/test_maintenance_cli.py`:

```python
def test_production_resource_path_wires_metrics_into_retention(monkeypatch, events):
    captured = {}

    class SpyRetentionService:
        def __init__(self, repo, store, clock, **kwargs):
            captured.update(kwargs)

        async def run_once(self, **_kwargs):
            from services.api.app.evaluation.delivery_models import CleanupReport
            return CleanupReport()

    monkeypatch.setattr(maintenance, "RetentionService", SpyRetentionService)
    ns = run_namespace(1)
    repo = FakeRepo(lambda: T0, events, candidates=[ns], expired_runs=0)
    store = FakeStore(events, {})
    code = maintenance.main(["cleanup", "--dry-run", "--limit", "5"],
                            open_resources=resources_for(repo, store, lambda: T0))
    assert code == maintenance.EXIT_OK
    assert "metrics" in captured
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    assert isinstance(captured["metrics"], DeliveryMetrics)
```

`CleanupReport` (`services/api/app/evaluation/delivery_models.py`) is a strict Pydantic model whose seven count fields (`runs_expired`, `namespaces_examined`, `namespaces_eligible`, `namespaces_cleaned`, `versions_examined`, `versions_deleted`, `failures`) all default to `0`, so the zero-argument constructor above works as written.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/evaluation_delivery/test_maintenance_cli.py::test_production_resource_path_wires_metrics_into_retention -v`
Expected: FAIL — `assert "metrics" in captured` fails (empty dict)

- [ ] **Step 3: Pass metrics into `RetentionService`**

In `services/evaluation_delivery/maintenance.py`, modify `_cleanup`:

```python
async def _cleanup(args, resources) -> int:
    async with resources() as (repo, store, clock):
        report = await RetentionService(repo, store, clock, metrics=default_metrics()).run_once(
            apply=args.apply, limit=args.limit, namespaces=tuple(args.namespaces) if args.namespaces else None)
```

Add the import near the top of the file, alongside the existing module-level imports:

```python
from services.api.app.evaluation.delivery_metrics import default_metrics
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/evaluation_delivery/test_maintenance_cli.py::test_production_resource_path_wires_metrics_into_retention -v`
Expected: PASS

- [ ] **Step 5: Run the full maintenance CLI test file and the full delivery suite**

Run: `pytest tests/evaluation_delivery/test_maintenance_cli.py -q` then `pytest tests/evaluation_delivery -q`
Expected: both green

- [ ] **Step 6: Commit**

```bash
git add services/evaluation_delivery/maintenance.py tests/evaluation_delivery/test_maintenance_cli.py
git commit -m "feat(evaluation): record retention maintenance metrics"
```

---

## Task 4: `/metrics` route on the API

**Files:**
- Modify: `services/api/main.py`
- Test: `tests/observability/test_metrics_route.py`

**Interfaces:**
- Produces: `GET /metrics` on the FastAPI app, `text/plain; version=0.0.4; charset=utf-8` body from `prometheus_client.generate_latest()`.

- [ ] **Step 1: Write the failing test**

Create `tests/observability/test_metrics_route.py`:

```python
"""The /metrics route serves the process-wide Prometheus registry, unauthenticated (cluster-internal scrape only)."""
from fastapi.testclient import TestClient


def test_metrics_route_serves_the_prometheus_registry(monkeypatch):
    monkeypatch.setenv("EVAL_EVIDENCE_ENABLED", "false")
    from services.api.main import app
    with TestClient(app) as client:
        response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP" in response.text
```

Check whether `TestClient(app)` triggers the full lifespan (it does by default, entering the `async with`) — if the existing test suite already has a lighter-weight pattern for exercising a single route without booting Neo4j/Redis/Qdrant clients (search `tests/observability/` and `tests/local_env/` for existing `TestClient` usage with mocked clients), reuse that pattern instead of this raw one. Grep first:

Run: `grep -rn "TestClient(app)" tests/ | head`

If existing tests already mock or skip the heavy lifespan dependencies for route-only tests, follow that exact setup instead of assuming a bare `TestClient(app)` works standalone.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/observability/test_metrics_route.py -v`
Expected: FAIL with 404 (no `/metrics` route registered)

- [ ] **Step 3: Add the route**

In `services/api/main.py`, add the import near the top:

```python
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
```

Add the route after `app.include_router(health.router, prefix="/health", tags=["Health"])`:

```python
@app.get("/metrics")
async def metrics():
    """Prometheus scrape endpoint. Cluster-internal only (PodMonitor), no auth — matches other
    in-cluster-only endpoints; nothing here is derived from request content."""
    from fastapi import Response
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/observability/test_metrics_route.py -v`
Expected: PASS

- [ ] **Step 5: Run the full observability suite**

Run: `pytest tests/observability -q`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add services/api/main.py tests/observability/test_metrics_route.py
git commit -m "feat(api): expose the Prometheus registry at /metrics"
```

---

## Task 5: Drain consumer core logic

**Files:**
- Create: `services/evidence_drain_consumer/__init__.py`
- Create: `services/evidence_drain_consumer/consumer.py`
- Create: `tests/evidence_drain_consumer/__init__.py`
- Create: `tests/evidence_drain_consumer/test_consumer.py`

**Interfaces:**
- Consumes: `services.api.app.evaluation.publisher.parse_envelope(bytes) -> ExportEnvelope` (existing), `services.api.app.evaluation.publisher.PublishError` (existing), `services.api.app.evaluation.bounded_io.BoundedIO` (existing).
- Produces: `DrainConsumer` class with `async def process_message(self, body: bytes) -> str` returning one of `"archived"`, `"conflict"`, `"invalid"`. Later tasks (6, 9) build the poll loop and SQS wiring around this.

- [ ] **Step 1: Write `__init__.py` files**

```python
# services/evidence_drain_consumer/__init__.py
```

```python
# tests/evidence_drain_consumer/__init__.py
```

- [ ] **Step 2: Write the failing tests**

Create `tests/evidence_drain_consumer/test_consumer.py`:

```python
"""Drain consumer: a temporary placeholder until Phase 3's evaluation worker exists.

It archives committed evidence envelopes read off the queue to S3, keyed by run_id, so nothing
is silently lost while nothing else consumes eval-runs. It is not a scorer and stores no state
of its own beyond the archive objects and a conflict counter.
"""
import boto3
import pytest
from moto import mock_aws
from prometheus_client import CollectorRegistry

from services.evidence_drain_consumer.consumer import DrainConsumer


@pytest.fixture
def envelope_bytes():
    from datetime import datetime, timezone
    from uuid import uuid4
    from services.api.app.evaluation.delivery_models import SafeConfiguration, SafeRun
    from services.api.app.evaluation.metadata_export import seal_export
    from services.api.app.evaluation.delivery_models import ExportEnvelope
    safe_run = SafeRun(run_id=uuid4(), started_at=datetime.now(timezone.utc),
                       completed_at=datetime.now(timezone.utc), status="success",
                       configuration=SafeConfiguration(generation_model="fake-chat",
                                                               embedding_model="fake-embed"))
    envelope = ExportEnvelope(run_id=safe_run.run_id, safe_run=safe_run,
                              redaction_policy_version="restricted-synthetic-v1", completeness="metadata_only")
    return envelope.canonical_bytes(), envelope.run_id


@pytest.fixture
def consumer():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="evidence-archive-test")
        client.put_bucket_versioning(Bucket="evidence-archive-test",
                                     VersioningConfiguration={"Status": "Enabled"})
        registry = CollectorRegistry()
        yield DrainConsumer(client, bucket="evidence-archive-test", registry=registry)


def test_archives_a_valid_envelope_by_run_id(consumer, envelope_bytes):
    body, run_id = envelope_bytes
    outcome = pytest_run(consumer.process_message(body))
    assert outcome == "archived"
    obj = consumer._client.get_object(Bucket="evidence-archive-test", Key=f"queue-archive/{run_id}.json")
    assert obj["Body"].read() == body


def test_identical_redelivery_is_a_no_op_not_a_conflict(consumer, envelope_bytes):
    body, run_id = envelope_bytes
    pytest_run(consumer.process_message(body))
    outcome = pytest_run(consumer.process_message(body))
    assert outcome == "archived"
    listing = consumer._client.list_objects_v2(Bucket="evidence-archive-test", Prefix="queue-archive-conflict/")
    assert listing.get("KeyCount", 0) == 0


def test_conflicting_hash_for_the_same_run_id_never_overwrites_the_first(consumer, envelope_bytes):
    import json
    body, run_id = envelope_bytes
    pytest_run(consumer.process_message(body))
    tampered = json.loads(body)
    tampered["safe_run"]["status"] = "error"
    tampered_bytes = json.dumps(tampered).encode()
    outcome = pytest_run(consumer.process_message(tampered_bytes))
    assert outcome == "conflict"
    original = consumer._client.get_object(Bucket="evidence-archive-test", Key=f"queue-archive/{run_id}.json")
    assert original["Body"].read() == body
    listing = consumer._client.list_objects_v2(Bucket="evidence-archive-test", Prefix="queue-archive-conflict/")
    assert listing["KeyCount"] == 1


def test_malformed_body_is_invalid_not_a_crash(consumer):
    outcome = pytest_run(consumer.process_message(b"not json"))
    assert outcome == "invalid"


def pytest_run(coro):
    import asyncio
    return asyncio.run(coro)
```

Note: the tampered envelope in `test_conflicting_hash_for_the_same_run_id_never_overwrites_the_first` will actually fail `parse_envelope`'s hash re-check (its `export_hash` no longer matches its content) rather than parse successfully with a different hash — `parse_envelope` recomputes and requires the hash to match its own content, so a hand-edited body without also fixing the hash is just invalid, not "valid but different." Fix the fixture to produce a genuinely valid-but-different envelope instead: build a second real `ExportEnvelope` with the same `run_id` but a different `status`, so it has its own internally-consistent hash. Replace the test body with:

```python
def test_conflicting_hash_for_the_same_run_id_never_overwrites_the_first(consumer, envelope_bytes):
    from datetime import datetime, timezone
    from services.api.app.evaluation.delivery_models import SafeConfiguration, ExportEnvelope, SafeRun
    body, run_id = envelope_bytes
    pytest_run(consumer.process_message(body))
    other_safe_run = SafeRun(run_id=run_id, started_at=datetime.now(timezone.utc),
                             completed_at=datetime.now(timezone.utc), status="error",
                             configuration=SafeConfiguration(generation_model="fake-chat",
                                                                     embedding_model="fake-embed"))
    conflicting = ExportEnvelope(run_id=run_id, safe_run=other_safe_run,
                                 redaction_policy_version="restricted-synthetic-v1",
                                 completeness="metadata_only").canonical_bytes()
    outcome = pytest_run(consumer.process_message(conflicting))
    assert outcome == "conflict"
    original = consumer._client.get_object(Bucket="evidence-archive-test", Key=f"queue-archive/{run_id}.json")
    assert original["Body"].read() == body
    listing = consumer._client.list_objects_v2(Bucket="evidence-archive-test", Prefix="queue-archive-conflict/")
    assert listing["KeyCount"] == 1
```

Delete the earlier draft of this test (the one using `json.loads`/tampering) — this replacement is the one to keep.

- [ ] **Step 3: Run tests to verify they fail**

Run: `pytest tests/evidence_drain_consumer/test_consumer.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.evidence_drain_consumer.consumer'`

- [ ] **Step 4: Implement `DrainConsumer`**

Create `services/evidence_drain_consumer/consumer.py`:

```python
"""Archives committed evidence envelopes read off the queue to S3, keyed by run_id.

Temporary: exists only because nothing yet consumes eval-runs (Phase 3's evaluation worker
does that). Using run_id as the object key makes identical redelivery an idempotent overwrite;
a genuinely conflicting hash for the same run_id is preserved separately, never silently merged
over the first copy. This is not a scorer and stores no state beyond archive objects and a
conflict counter.
"""
from __future__ import annotations

import logging
from typing import Any

from prometheus_client import REGISTRY, Counter, CollectorRegistry

from services.api.app.evaluation.publisher import PublishError, parse_envelope

logger = logging.getLogger(__name__)

ARCHIVED = "archived"
CONFLICT = "conflict"
INVALID = "invalid"


class DrainConsumer:
    def __init__(self, client: Any, *, bucket: str, registry: CollectorRegistry = REGISTRY) -> None:
        self._client = client
        self._bucket = bucket
        self._conflicts = Counter("evidence_archive_conflicts_total",
                                  "Envelopes whose run_id already had different archived content",
                                  registry=registry)

    async def process_message(self, body: bytes) -> str:
        try:
            envelope = parse_envelope(body)
        except PublishError:
            logger.warning("evidence archive skipped reason=invalid_envelope")
            return INVALID
        key = f"queue-archive/{envelope.run_id}.json"
        existing = self._get(key)
        if existing is not None:
            if existing == body:
                return ARCHIVED  # identical redelivery; nothing to do
            conflict_key = f"queue-archive-conflict/{envelope.run_id}-{_short_token()}.json"
            self._put(conflict_key, body)
            self._conflicts.inc()
            logger.warning("evidence archive conflict run_id=%s", envelope.run_id)
            return CONFLICT
        self._put(key, body)
        return ARCHIVED

    def _get(self, key: str) -> bytes | None:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise

    def _put(self, key: str, body: bytes) -> None:
        self._client.put_object(Bucket=self._bucket, Key=key, Body=body,
                                ServerSideEncryption="aws:kms", ContentType="application/json")


def _short_token() -> str:
    from uuid import uuid4
    return uuid4().hex[:12]
```

This is synchronous boto3 wrapped in an `async def` method for now — Task 6 wraps every call site through `BoundedIO` when it builds the poll loop; this task proves the archive/conflict/invalid logic in isolation first.

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/evidence_drain_consumer/test_consumer.py -v`
Expected: all 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add services/evidence_drain_consumer tests/evidence_drain_consumer
git commit -m "feat(evaluation): add the evidence drain consumer's archive logic"
```

---

## Task 6: Drain consumer CLI entrypoint

**Files:**
- Create: `services/evidence_drain_consumer/main.py`
- Modify: `services/evidence_drain_consumer/consumer.py`
- Modify: `tests/evidence_drain_consumer/test_consumer.py`

**Interfaces:**
- Consumes: `DrainConsumer.process_message(body)` (Task 5).
- Produces: `python -m services.evidence_drain_consumer.main`, reading `EVIDENCE_QUEUE_URL`, `EVIDENCE_ARCHIVE_BUCKET`, `AWS_REGION` from the environment; exits 2 on invalid/missing configuration, 0 on a clean signal-triggered stop, 1 on an unexpected runtime failure — same exit-code convention as `services/evaluation_delivery/worker.py`.

- [ ] **Step 1: Write the failing test for malformed-message safety in the poll loop**

Add to `tests/evidence_drain_consumer/test_consumer.py`:

```python
def test_poll_loop_survives_a_malformed_message_and_still_processes_the_next(consumer, envelope_bytes):
    from services.evidence_drain_consumer.consumer import DrainConsumer
    body, run_id = envelope_bytes

    class FakeSQS:
        def __init__(self, messages):
            self.messages = list(messages)
            self.deleted = []

        def receive_message(self, **_kwargs):
            if not self.messages:
                return {}
            return {"Messages": [self.messages.pop(0)]}

        def delete_message(self, *, QueueUrl, ReceiptHandle):
            self.deleted.append(ReceiptHandle)

    sqs = FakeSQS([
        {"Body": "not json", "ReceiptHandle": "r1"},
        {"Body": body.decode(), "ReceiptHandle": "r2"},
    ])
    outcomes = pytest_run(_drain_two(consumer, sqs))
    assert outcomes == ["invalid", "archived"]
    assert sqs.deleted == ["r2"]  # the malformed message is left for the queue's own redrive, not deleted


async def _drain_two(consumer, sqs):
    from services.evidence_drain_consumer.main import poll_once
    outcomes = []
    for _ in range(2):
        outcome = await poll_once(consumer, sqs, queue_url="http://queue")
        outcomes.append(outcome)
    return outcomes
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/evidence_drain_consumer/test_consumer.py::test_poll_loop_survives_a_malformed_message_and_still_processes_the_next -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'services.evidence_drain_consumer.main'`

- [ ] **Step 3: Implement `main.py`**

Create `services/evidence_drain_consumer/main.py`:

```python
"""Drain consumer entrypoint: poll the evidence queue, archive each envelope, delete on success.

A malformed message is left on the queue rather than deleted, so SQS's own redrive policy moves
it to the DLQ after its configured receive count — this process never makes that call itself.

Run: ``python -m services.evidence_drain_consumer.main``
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

logger = logging.getLogger(__name__)

EXIT_RUNTIME_FAILURE = 1
EXIT_CONFIGURATION = 2


async def poll_once(consumer, sqs_client, *, queue_url: str) -> str | None:
    """One receive-and-process step; returns the outcome, or None when the queue was empty."""
    response = sqs_client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=1)
    messages = response.get("Messages", [])
    if not messages:
        return None
    message = messages[0]
    outcome = await consumer.process_message(message["Body"].encode()
                                              if isinstance(message["Body"], str) else message["Body"])
    if outcome in ("archived", "conflict"):
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return outcome


async def serve(consumer, sqs_client, *, queue_url: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        outcome = await poll_once(consumer, sqs_client, queue_url=queue_url)
        if outcome is None:
            await asyncio.sleep(1)


def _load_config():
    import os
    queue_url = os.environ.get("EVIDENCE_QUEUE_URL")
    bucket = os.environ.get("EVIDENCE_ARCHIVE_BUCKET")
    region = os.environ.get("AWS_REGION", "us-east-1")
    if not queue_url or not bucket:
        raise ValueError("EVIDENCE_QUEUE_URL and EVIDENCE_ARCHIVE_BUCKET are required")
    return queue_url, bucket, region


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    try:
        queue_url, bucket, region = _load_config()
    except ValueError:
        logger.error("evidence drain consumer configuration invalid")
        return EXIT_CONFIGURATION
    try:
        import boto3
        from urllib.parse import urlsplit
        from services.evidence_drain_consumer.consumer import DrainConsumer
        parts = urlsplit(queue_url)
        sqs_client = boto3.client("sqs", region_name=region, endpoint_url=f"{parts.scheme}://{parts.netloc}")
        s3_client = boto3.client("s3", region_name=region)
        consumer = DrainConsumer(s3_client, bucket=bucket)
        stop_event = asyncio.Event()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop_event.set)
        logger.info("evidence drain consumer started")
        loop.run_until_complete(serve(consumer, sqs_client, queue_url=queue_url, stop_event=stop_event))
        logger.info("evidence drain consumer stopped")
        return 0
    except Exception:
        logger.error("evidence drain consumer failed")
        return EXIT_RUNTIME_FAILURE


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/evidence_drain_consumer/test_consumer.py::test_poll_loop_survives_a_malformed_message_and_still_processes_the_next -v`
Expected: PASS

- [ ] **Step 5: Write and run a configuration-validation test**

Add to `tests/evidence_drain_consumer/test_consumer.py`:

```python
def test_missing_configuration_is_a_content_free_exit_2(monkeypatch, capsys):
    from services.evidence_drain_consumer import main
    monkeypatch.delenv("EVIDENCE_QUEUE_URL", raising=False)
    monkeypatch.delenv("EVIDENCE_ARCHIVE_BUCKET", raising=False)
    code = main.main()
    out, err = capsys.readouterr()
    assert code == main.EXIT_CONFIGURATION
    assert "EVIDENCE_QUEUE_URL" not in err  # fixed message only, no echoed env var names/values
```

Run: `pytest tests/evidence_drain_consumer/test_consumer.py::test_missing_configuration_is_a_content_free_exit_2 -v`
Expected: PASS

- [ ] **Step 6: Run the full drain-consumer test file**

Run: `pytest tests/evidence_drain_consumer -q`
Expected: all green

- [ ] **Step 7: Commit**

```bash
git add services/evidence_drain_consumer tests/evidence_drain_consumer
git commit -m "feat(evaluation): add the evidence drain consumer's poll loop and CLI entrypoint"
```

---

## Task 7: Production Terraform — evidence infrastructure

**Files:**
- Create: `infra/terraform/evidence.tf`
- Modify: `infra/terraform/outputs.tf`

**Interfaces:**
- Produces (Terraform outputs): `evidence_queue_url`, `evidence_bucket_name`, `evidence_kms_key_arn`, `evidence_worker_irsa_role_arn`, `evidence_drain_consumer_irsa_role_arn`.

- [ ] **Step 1: Write `infra/terraform/evidence.tf`**

```hcl
# infra/terraform/evidence.tf
#
# PR 5a: metadata-only evidence delivery infrastructure. No new database — the outbox tables
# live in the existing chat-history PostgreSQL instance (var.db_tier). This bucket and KMS key
# back only the drain consumer's queue-archive/ objects while EVAL_CAPTURE_CONTENT stays
# disabled; PR 5b would be the first thing to also write real payload content here.

resource "aws_kms_key" "evidence" {
  description             = "Evidence delivery archive encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
}

resource "aws_kms_alias" "evidence" {
  name          = "alias/${var.cluster_name}-evidence"
  target_key_id = aws_kms_key.evidence.key_id
}

resource "aws_s3_bucket" "evidence" {
  bucket = "${var.cluster_name}-evidence-prod-7649"

  tags = {
    Name = "Evidence delivery archive"
  }
}

resource "aws_s3_bucket_versioning" "evidence_ver" {
  bucket = aws_s3_bucket.evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidence_sse" {
  bucket = aws_s3_bucket.evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.evidence.arn
    }
    bucket_key_enabled = true
  }
}

# Denies any write that doesn't request this key's SSE-KMS, enforcing server-side what the
# application already requests client-side.
data "aws_iam_policy_document" "evidence_bucket" {
  statement {
    sid       = "DenyNonKmsWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.evidence.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "evidence" {
  bucket = aws_s3_bucket.evidence.id
  policy = data.aws_iam_policy_document.evidence_bucket.json
}

resource "aws_s3_bucket_lifecycle_configuration" "evidence_lifecycle" {
  bucket = aws_s3_bucket.evidence.id

  rule {
    id     = "expire-archive-content"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_sqs_queue" "evidence_dlq" {
  name                      = "${var.cluster_name}-evidence-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue" "evidence" {
  name                       = "${var.cluster_name}-evidence"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600 # 4 days

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.evidence_dlq.arn
    maxReceiveCount     = 5
  })
}

# --- Delivery worker: publishes committed outbox rows --------------------------------------

resource "aws_iam_policy" "evidence_worker" {
  name        = "RAG_Evidence_Worker_Policy"
  description = "Allows the evidence delivery worker to publish committed envelopes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["sqs:SendMessage"]
        Effect   = "Allow"
        Resource = aws_sqs_queue.evidence.arn
      }
    ]
  })
}

module "evidence_worker_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-evidence-worker-role"

  oidc_providers = {
    main = {
      provider_arn                = module.eks.oidc_provider_arn
      namespace_service_accounts  = ["default:evidence-worker"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.evidence_worker.arn
  }
}

# --- Drain consumer: temporary consumer until Phase 3's evaluation worker exists -----------

resource "aws_iam_policy" "evidence_drain_consumer" {
  name        = "RAG_Evidence_Drain_Consumer_Policy"
  description = "Allows the drain consumer to read the evidence queue and archive envelopes to S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ]
        Effect   = "Allow"
        Resource = aws_sqs_queue.evidence.arn
      },
      {
        Action = ["s3:PutObject", "s3:GetObject"]
        Effect = "Allow"
        Resource = [
          "${aws_s3_bucket.evidence.arn}/queue-archive/*",
          "${aws_s3_bucket.evidence.arn}/queue-archive-conflict/*"
        ]
      },
      {
        Action   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"]
        Effect   = "Allow"
        Resource = aws_kms_key.evidence.arn
      }
    ]
  })
}

module "evidence_drain_consumer_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-evidence-drain-consumer-role"

  oidc_providers = {
    main = {
      provider_arn               = module.eks.oidc_provider_arn
      namespace_service_accounts = ["default:evidence-drain-consumer"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.evidence_drain_consumer.arn
  }
}
```

- [ ] **Step 2: Add outputs**

In `infra/terraform/outputs.tf`, append:

```hcl
output "evidence_queue_url" {
  description = "SQS queue for committed evidence envelopes."
  value       = aws_sqs_queue.evidence.id
}

output "evidence_bucket_name" {
  description = "S3 bucket for the drain consumer's queue-archive objects (and future PR 5b payload content)."
  value       = aws_s3_bucket.evidence.id
}

output "evidence_kms_key_arn" {
  description = "KMS key ARN required on every write to the evidence bucket."
  value       = aws_kms_key.evidence.arn
}

output "evidence_worker_irsa_role_arn" {
  description = "IAM role used by the evidence delivery worker."
  value       = module.evidence_worker_irsa_role.iam_role_arn
}

output "evidence_drain_consumer_irsa_role_arn" {
  description = "IAM role used by the evidence drain consumer."
  value       = module.evidence_drain_consumer_irsa_role.iam_role_arn
}
```

- [ ] **Step 3: Format and validate**

Run: `cd infra/terraform && terraform fmt -check evidence.tf outputs.tf`
Expected: no diff (if there is one, run `terraform fmt evidence.tf outputs.tf` and re-check)

Run: `cd infra/terraform && terraform validate`
Expected: `Success! The configuration is valid.` — this only checks HCL syntax and internal consistency; it does not require AWS credentials or reach out to AWS.

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/evidence.tf infra/terraform/outputs.tf
git commit -m "feat(infra): provision evidence delivery KMS, S3, SQS and IAM"
```

**Do not run `terraform plan` or `terraform apply` in this task.** That happens only in Task 14, against the user's explicit go-ahead, after staging (Task 13) has validated this exact configuration shape.

---

## Task 8: Staging Terraform environment

**Files:**
- Create: `infra/terraform-staging/main.tf`
- Create: `infra/terraform-staging/variables.tf`
- Create: `infra/terraform-staging/data.tf`
- Create: `infra/terraform-staging/rds.tf`
- Create: `infra/terraform-staging/evidence.tf`
- Create: `infra/terraform-staging/outputs.tf`
- Create: `infra/terraform-staging/terraform.tfvars.example`

**Interfaces:**
- Produces (Terraform outputs): `staging_db_endpoint`, `staging_db_secret_arn`, `staging_evidence_queue_url`, `staging_evidence_bucket_name`, `staging_evidence_kms_key_arn`, `staging_evidence_worker_irsa_role_arn`, `staging_evidence_drain_consumer_irsa_role_arn`, `staging_api_irsa_role_arn` is not needed (the staging API needs no new AWS permissions — see design Section 3).

- [ ] **Step 1: Write `main.tf`**

```hcl
# infra/terraform-staging/main.tf
#
# Separate state from infra/terraform's production state — a mistake here cannot corrupt
# production. Reads the existing cluster/VPC by data lookup rather than duplicating them.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }

  backend "s3" {
    bucket       = "rag-platform-terraform-state-prod-7649"
    key          = "platform/staging-evidence.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project     = "Enterprise-RAG"
      Environment = "staging-evidence"
      ManagedBy   = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}
```

- [ ] **Step 2: Write `variables.tf`**

```hcl
# infra/terraform-staging/variables.tf

variable "aws_region" {
  description = "AWS region — must match the existing cluster's region."
  type        = string
  default     = "us-east-1"
}

variable "cluster_name" {
  description = "Name of the existing EKS cluster to attach staging resources to."
  type        = string
  default     = "rag-platform-cluster"
}

variable "vpc_cidr" {
  description = "CIDR of the existing VPC — used only for the staging RDS security group ingress rule."
  type        = string
  default     = "10.0.0.0/16"
}
```

- [ ] **Step 3: Write `data.tf`**

```hcl
# infra/terraform-staging/data.tf
#
# Looks up the existing cluster/VPC/OIDC provider rather than reapplying those modules —
# staging must never be able to recreate or modify production networking or the cluster itself.

data "aws_eks_cluster" "existing" {
  name = var.cluster_name
}

data "aws_vpc" "existing" {
  filter {
    name   = "tag:Name"
    values = ["${var.cluster_name}-vpc"]
  }
}

data "aws_subnets" "database" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.existing.id]
  }

  filter {
    name   = "tag:Name"
    values = ["*-db-*"]
  }
}

data "aws_subnet" "database" {
  for_each = toset(data.aws_subnets.database.ids)
  id       = each.value
}

data "aws_db_subnet_group" "existing" {
  name = "${var.cluster_name}-vpc"
}

data "aws_iam_openid_connect_provider" "existing" {
  url = data.aws_eks_cluster.existing.identity[0].oidc[0].issuer
}
```

Note: `data.aws_db_subnet_group.existing`'s name assumes the VPC module's default DB subnet group naming (`<vpc-name>`). Confirm the actual name before running `terraform plan` in Task 13:

Run (needs AWS credentials, Task 13 only): `aws rds describe-db-subnet-groups --query 'DBSubnetGroups[].DBSubnetGroupName' --output table`

If the name differs, fix the `name` argument here before applying — do not guess further at implementation time; this is a Task 13 verification step, not something to resolve blind.

- [ ] **Step 4: Write `rds.tf`**

```hcl
# infra/terraform-staging/rds.tf
#
# db.t3.micro, no HA — staging only needs to prove PostgreSQL transaction semantics
# (advisory-lock migrations, atomic outbox commit, lease-token claims), not Aurora's
# autoscaling or multi-AZ properties. Matches infra/terraform's own db_tier = "rds" default.

resource "aws_security_group" "staging_evidence_postgres" {
  name        = "${var.cluster_name}-staging-evidence-postgres"
  description = "Allow PostgreSQL from within the VPC"
  vpc_id      = data.aws_vpc.existing.id

  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = [var.vpc_cidr]
  }
}

resource "aws_db_instance" "staging_evidence_postgres" {
  identifier     = "${var.cluster_name}-staging-evidence-postgres"
  engine         = "postgres"
  engine_version = "15"
  instance_class = "db.t3.micro"

  allocated_storage           = 20
  db_name                     = "evidence_staging"
  username                    = "ragadmin"
  manage_master_user_password = true

  db_subnet_group_name   = data.aws_db_subnet_group.existing.name
  vpc_security_group_ids = [aws_security_group.staging_evidence_postgres.id]

  publicly_accessible = false
  multi_az            = false
  skip_final_snapshot  = true # disposable staging data — safe to drop on teardown

  tags = {
    Name = "${var.cluster_name}-staging-evidence-postgres"
  }
}
```

- [ ] **Step 5: Write `evidence.tf`**

Same shape as `infra/terraform/evidence.tf` (Task 7), staging-scoped — different bucket/queue names, `staging-evidence` namespace in the IRSA trust relationships, and it references `data.aws_eks_cluster.existing`/`data.aws_iam_openid_connect_provider.existing` instead of `module.eks`:

```hcl
# infra/terraform-staging/evidence.tf

resource "aws_kms_key" "staging_evidence" {
  description             = "Staging evidence delivery archive encryption"
  deletion_window_in_days = 7 # shorter than prod — staging is disposable
  enable_key_rotation     = true
}

resource "aws_s3_bucket" "staging_evidence" {
  bucket        = "${var.cluster_name}-evidence-staging-7649"
  force_destroy = true # disposable — safe to tear down without manual emptying

  tags = {
    Name = "Staging evidence delivery archive"
  }
}

resource "aws_s3_bucket_versioning" "staging_evidence_ver" {
  bucket = aws_s3_bucket.staging_evidence.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "staging_evidence_sse" {
  bucket = aws_s3_bucket.staging_evidence.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.staging_evidence.arn
    }
    bucket_key_enabled = true
  }
}

data "aws_iam_policy_document" "staging_evidence_bucket" {
  statement {
    sid       = "DenyNonKmsWrites"
    effect    = "Deny"
    actions   = ["s3:PutObject"]
    resources = ["${aws_s3_bucket.staging_evidence.arn}/*"]

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    condition {
      test     = "StringNotEquals"
      variable = "s3:x-amz-server-side-encryption-aws-kms-key-id"
      values   = [aws_kms_key.staging_evidence.arn]
    }
  }
}

resource "aws_s3_bucket_policy" "staging_evidence" {
  bucket = aws_s3_bucket.staging_evidence.id
  policy = data.aws_iam_policy_document.staging_evidence_bucket.json
}

resource "aws_s3_bucket_lifecycle_configuration" "staging_evidence_lifecycle" {
  bucket = aws_s3_bucket.staging_evidence.id

  rule {
    id     = "expire-archive-content"
    status = "Enabled"

    filter {
      prefix = ""
    }

    expiration {
      days = 30
    }

    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}

resource "aws_sqs_queue" "staging_evidence_dlq" {
  name                      = "${var.cluster_name}-staging-evidence-dlq"
  message_retention_seconds = 1209600
}

resource "aws_sqs_queue" "staging_evidence" {
  name                       = "${var.cluster_name}-staging-evidence"
  visibility_timeout_seconds = 60
  message_retention_seconds  = 345600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.staging_evidence_dlq.arn
    maxReceiveCount     = 5
  })
}

resource "aws_iam_policy" "staging_evidence_worker" {
  name        = "RAG_Staging_Evidence_Worker_Policy"
  description = "Allows the staging evidence delivery worker to publish committed envelopes"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action   = ["sqs:SendMessage"]
        Effect   = "Allow"
        Resource = aws_sqs_queue.staging_evidence.arn
      }
    ]
  })
}

module "staging_evidence_worker_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-staging-evidence-worker-role"

  oidc_providers = {
    main = {
      provider_arn               = data.aws_iam_openid_connect_provider.existing.arn
      namespace_service_accounts = ["staging-evidence:evidence-worker"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.staging_evidence_worker.arn
  }
}

resource "aws_iam_policy" "staging_evidence_drain_consumer" {
  name        = "RAG_Staging_Evidence_Drain_Consumer_Policy"
  description = "Allows the staging drain consumer to read the evidence queue and archive envelopes to S3"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:ChangeMessageVisibility",
          "sqs:GetQueueAttributes"
        ]
        Effect   = "Allow"
        Resource = aws_sqs_queue.staging_evidence.arn
      },
      {
        Action = ["s3:PutObject", "s3:GetObject"]
        Effect = "Allow"
        Resource = [
          "${aws_s3_bucket.staging_evidence.arn}/queue-archive/*",
          "${aws_s3_bucket.staging_evidence.arn}/queue-archive-conflict/*"
        ]
      },
      {
        Action   = ["kms:GenerateDataKey", "kms:Encrypt", "kms:Decrypt"]
        Effect   = "Allow"
        Resource = aws_kms_key.staging_evidence.arn
      }
    ]
  })
}

module "staging_evidence_drain_consumer_irsa_role" {
  source  = "terraform-aws-modules/iam/aws//modules/iam-role-for-service-accounts-eks"
  version = "~> 5.0"

  role_name = "rag-staging-evidence-drain-consumer-role"

  oidc_providers = {
    main = {
      provider_arn               = data.aws_iam_openid_connect_provider.existing.arn
      namespace_service_accounts = ["staging-evidence:evidence-drain-consumer"]
    }
  }

  role_policy_arns = {
    policy = aws_iam_policy.staging_evidence_drain_consumer.arn
  }
}
```

- [ ] **Step 6: Write `outputs.tf`**

```hcl
# infra/terraform-staging/outputs.tf

output "staging_db_endpoint" {
  value = aws_db_instance.staging_evidence_postgres.address
}

output "staging_db_secret_arn" {
  value = aws_db_instance.staging_evidence_postgres.master_user_secret[0].secret_arn
}

output "staging_evidence_queue_url" {
  value = aws_sqs_queue.staging_evidence.id
}

output "staging_evidence_bucket_name" {
  value = aws_s3_bucket.staging_evidence.id
}

output "staging_evidence_kms_key_arn" {
  value = aws_kms_key.staging_evidence.arn
}

output "staging_evidence_worker_irsa_role_arn" {
  value = module.staging_evidence_worker_irsa_role.iam_role_arn
}

output "staging_evidence_drain_consumer_irsa_role_arn" {
  value = module.staging_evidence_drain_consumer_irsa_role.iam_role_arn
}
```

- [ ] **Step 7: Write `terraform.tfvars.example`**

```hcl
# Copy to terraform.tfvars and adjust if the existing cluster's name/region/VPC CIDR differ
# from infra/terraform's defaults. terraform.tfvars is gitignored.

aws_region   = "us-east-1"
cluster_name = "rag-platform-cluster"
vpc_cidr     = "10.0.0.0/16"
```

- [ ] **Step 8: Format and validate**

Run: `cd infra/terraform-staging && terraform fmt -check *.tf`
Expected: no diff (run `terraform fmt *.tf` if there is one, then re-check)

Run: `cd infra/terraform-staging && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.` — `-backend=false` avoids needing real S3 state access for this offline syntax/consistency check.

- [ ] **Step 9: Commit**

```bash
git add infra/terraform-staging
git commit -m "feat(infra): add a staging environment for evidence delivery validation"
```

**Do not run `terraform plan` or `terraform apply` in this task.** That happens in Task 13, against the user's explicit go-ahead.

---

## Task 9: Kubernetes manifests — migrate, worker, drain consumer, maintenance

**Files:**
- Create: `deploy/k8s/evidence-migrate-job.yaml`
- Create: `deploy/k8s/evidence-delivery-worker.yaml`
- Create: `deploy/k8s/evidence-drain-consumer.yaml`
- Create: `deploy/k8s/evidence-maintenance-cronjob.yaml`

**Interfaces:**
- Produces: four manifests using the placeholder-token pattern already used by `deploy/k8s/ingestion-worker.yaml` (`API_IMAGE`, `NAMESPACE` via `sed` substitution in Task 11's deploy script, not Helm templating).

- [ ] **Step 1: Write `evidence-migrate-job.yaml`**

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: evidence-migrate
  namespace: default
spec:
  backoffLimit: 2
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: migrate
          image: API_IMAGE
          imagePullPolicy: IfNotPresent
          command: ["python", "-m", "services.api.app.evaluation.migrate"]
          envFrom:
            - secretRef:
                name: app-env-secret
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: 500m, memory: 512Mi}
```

- [ ] **Step 2: Write `evidence-delivery-worker.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: evidence-worker
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: evidence-worker
  template:
    metadata:
      labels:
        app: evidence-worker
    spec:
      serviceAccountName: evidence-worker
      containers:
        - name: worker
          image: API_IMAGE
          imagePullPolicy: IfNotPresent
          command: ["python", "-m", "services.evaluation_delivery.worker"]
          envFrom:
            - secretRef:
                name: app-env-secret
          env:
            - name: AWS_REGION
              value: us-east-1
            - name: EVAL_QUEUE_URL
              value: EVIDENCE_QUEUE_URL_VALUE
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: 500m, memory: 512Mi}
```

- [ ] **Step 3: Write `evidence-drain-consumer.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: evidence-drain-consumer
  namespace: default
spec:
  replicas: 1
  selector:
    matchLabels:
      app: evidence-drain-consumer
  template:
    metadata:
      labels:
        app: evidence-drain-consumer
    spec:
      serviceAccountName: evidence-drain-consumer
      containers:
        - name: drain-consumer
          image: API_IMAGE
          imagePullPolicy: IfNotPresent
          command: ["python", "-m", "services.evidence_drain_consumer.main"]
          env:
            - name: AWS_REGION
              value: us-east-1
            - name: EVIDENCE_QUEUE_URL
              value: EVIDENCE_QUEUE_URL_VALUE
            - name: EVIDENCE_ARCHIVE_BUCKET
              value: EVIDENCE_BUCKET_NAME_VALUE
          resources:
            requests: {cpu: 100m, memory: 256Mi}
            limits: {cpu: 500m, memory: 512Mi}
```

- [ ] **Step 4: Write `evidence-maintenance-cronjob.yaml`**

`concurrencyPolicy: Forbid` closes this plan's fifth Review Focus item — a slow `cleanup --apply` must never overlap the next scheduled run:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: evidence-maintenance
  namespace: default
spec:
  schedule: "0 6 * * *" # once daily; tune after Task 13's staging dry-run sizes --limit
  concurrencyPolicy: Forbid
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 3
  jobTemplate:
    spec:
      backoffLimit: 1
      template:
        spec:
          restartPolicy: Never
          containers:
            - name: maintenance
              image: API_IMAGE
              imagePullPolicy: IfNotPresent
              command: ["python", "-m", "services.evaluation_delivery.maintenance", "cleanup",
                       "--apply", "--limit", "MAINTENANCE_LIMIT_VALUE"]
              envFrom:
                - secretRef:
                    name: app-env-secret
              resources:
                requests: {cpu: 100m, memory: 256Mi}
                limits: {cpu: 500m, memory: 512Mi}
```

- [ ] **Step 5: Validate YAML syntax**

Run:

```bash
python3 -c "
import yaml, sys
for path in ['deploy/k8s/evidence-migrate-job.yaml', 'deploy/k8s/evidence-delivery-worker.yaml',
             'deploy/k8s/evidence-drain-consumer.yaml', 'deploy/k8s/evidence-maintenance-cronjob.yaml']:
    with open(path) as f:
        text = f.read().replace('API_IMAGE', 'example/image:tag').replace(
            'EVIDENCE_QUEUE_URL_VALUE', 'https://sqs.us-east-1.amazonaws.com/123456789012/x').replace(
            'EVIDENCE_BUCKET_NAME_VALUE', 'example-bucket').replace('MAINTENANCE_LIMIT_VALUE', '100')
        list(yaml.safe_load_all(text))
    print(path, 'OK')
"
```

Expected: `OK` printed for all four files, no exception. (The `sed`-substituted placeholder tokens are not valid on their own — e.g. an unquoted bare value is fine here since YAML parses them as strings regardless — this step only proves the YAML structure itself is well-formed, not that the final substituted values are semantically correct; that is proven live in Task 13.)

If `kubectl` has access to a real cluster at this point, also run a structural dry-run against one of the substituted files, e.g.:

Run: `sed -e "s|API_IMAGE|example/image:tag|" deploy/k8s/evidence-migrate-job.yaml | kubectl apply --dry-run=client -f -`
Expected: `job.batch/evidence-migrate created (dry run)` — skip this extra check if no cluster is reachable from this environment; the Python YAML check above is sufficient to unblock this task.

- [ ] **Step 6: Commit**

```bash
git add deploy/k8s/evidence-migrate-job.yaml deploy/k8s/evidence-delivery-worker.yaml \
       deploy/k8s/evidence-drain-consumer.yaml deploy/k8s/evidence-maintenance-cronjob.yaml
git commit -m "feat(deploy): add evidence delivery Kubernetes manifests"
```

---

## Task 10: PodMonitor for the API's /metrics endpoint

**Files:**
- Create: `deploy/monitoring/evidence-service-monitor.yaml`

**Interfaces:**
- Produces: a `PodMonitor` scraping the existing API pods' `http` port at `/metrics`, matching the existing `deploy/monitoring/ray-service-monitors.yaml` pattern and its `kube-prometheus-stack` release label.

- [ ] **Step 1: Write the PodMonitor**

```yaml
# deploy/monitoring/evidence-service-monitor.yaml
#
# PodMonitor for kube-prometheus-stack to scrape the API's /metrics endpoint, which now serves
# the evidence delivery metrics (services/api/app/evaluation/delivery_metrics.py) alongside the
# existing libs/observability/metrics.py counters, all on the same process-wide registry.
#
#   kubectl apply -f deploy/monitoring/evidence-service-monitor.yaml

apiVersion: monitoring.coreos.com/v1
kind: PodMonitor
metadata:
  name: api-metrics
  namespace: monitoring
  labels:
    release: kube-prometheus-stack
spec:
  namespaceSelector:
    matchNames:
      - default
  selector:
    matchLabels:
      app.kubernetes.io/name: api
  podMetricsEndpoints:
    - port: http
      path: /metrics
      interval: 30s
```

Check the API Helm chart's pod labels actually include `app.kubernetes.io/name: api` (the `api.selectorLabels` template in `deploy/helm/api/templates/_helpers.tpl`) before relying on this selector:

Run: `grep -n "app.kubernetes.io/name" deploy/helm/api/templates/_helpers.tpl`

If the label key or value differs, fix `selector.matchLabels` here to match exactly what `_helpers.tpl` actually renders — do not guess; read the file.

- [ ] **Step 2: Validate YAML syntax**

Run: `python3 -c "import yaml; list(yaml.safe_load_all(open('deploy/monitoring/evidence-service-monitor.yaml')))"`
Expected: no exception

- [ ] **Step 3: Commit**

```bash
git add deploy/monitoring/evidence-service-monitor.yaml
git commit -m "feat(deploy): scrape the API's evidence delivery metrics"
```

---

## Task 11: Deploy script for the evidence delivery components

**Files:**
- Create: `scripts/deploy_evidence_delivery.sh`
- Modify: `scripts/bootstrap_cluster.sh`

**Interfaces:**
- Produces: `bash scripts/deploy_evidence_delivery.sh`, parameterized by `NAMESPACE` and `TERRAFORM_DIR` exactly like `scripts/deploy_ingestion.sh`, so the same script deploys to both `default` (prod, `infra/terraform`) and `staging-evidence` (`infra/terraform-staging`).

- [ ] **Step 1: Write `scripts/deploy_evidence_delivery.sh`**

```bash
#!/bin/bash
# Deploy or update the evidence delivery components (migration job, delivery worker, drain
# consumer, maintenance CronJob) in an existing cluster. Mirrors scripts/deploy_ingestion.sh.
#
# Required infrastructure:
#   - Terraform evidence resources have been applied (infra/terraform or infra/terraform-staging).
#   - The services/api image has been pushed to ECR (it hosts the worker/consumer/maintenance
#     entrypoints, not a separate image).
#   - app-env-secret exists in the target namespace with DATABASE_URL and EVAL_IDENTITY_HASH_KEY set.
#
# EVAL_EVIDENCE_ENABLED is NOT set by this script. Ships dark: components run with delivery
# disabled until a separate, explicit change flips the flag in app-env-secret (see Task 14).
#
# All values can be exported explicitly. When omitted, this script derives them from AWS/ECR
# and Terraform outputs.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

REGION="${REGION:-us-east-1}"
NAMESPACE="${NAMESPACE:-default}"
TERRAFORM_DIR="${TERRAFORM_DIR:-${ROOT_DIR}/infra/terraform}"
MAINTENANCE_LIMIT="${MAINTENANCE_LIMIT:-100}"

for command in aws kubectl terraform sed; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

API_IMAGE_TAG="${API_IMAGE_TAG:-$(aws ecr describe-images \
  --repository-name services/api \
  --region "$REGION" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' \
  --output text)}"

if [[ -z "$API_IMAGE_TAG" || "$API_IMAGE_TAG" == "None" ]]; then
  echo "ERROR: no services/api image tag found in ECR." >&2
  exit 1
fi

API_IMAGE="${ECR_BASE}/services/api:${API_IMAGE_TAG}"

QUEUE_URL_OUTPUT="evidence_queue_url"
BUCKET_OUTPUT="evidence_bucket_name"
WORKER_ROLE_OUTPUT="evidence_worker_irsa_role_arn"
CONSUMER_ROLE_OUTPUT="evidence_drain_consumer_irsa_role_arn"
if [[ "$TERRAFORM_DIR" == *terraform-staging ]]; then
  QUEUE_URL_OUTPUT="staging_evidence_queue_url"
  BUCKET_OUTPUT="staging_evidence_bucket_name"
  WORKER_ROLE_OUTPUT="staging_evidence_worker_irsa_role_arn"
  CONSUMER_ROLE_OUTPUT="staging_evidence_drain_consumer_irsa_role_arn"
fi

EVIDENCE_QUEUE_URL="${EVIDENCE_QUEUE_URL:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$QUEUE_URL_OUTPUT")}"
EVIDENCE_BUCKET_NAME="${EVIDENCE_BUCKET_NAME:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$BUCKET_OUTPUT")}"
EVIDENCE_WORKER_ROLE_ARN="${EVIDENCE_WORKER_ROLE_ARN:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$WORKER_ROLE_OUTPUT")}"
EVIDENCE_CONSUMER_ROLE_ARN="${EVIDENCE_CONSUMER_ROLE_ARN:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$CONSUMER_ROLE_OUTPUT")}"

kubectl cluster-info >/dev/null
kubectl get secret app-env-secret --namespace "$NAMESPACE" >/dev/null

echo "--- Evidence delivery deployment ---"
echo "Namespace     : ${NAMESPACE}"
echo "Image         : ${API_IMAGE}"
echo "Queue         : ${EVIDENCE_QUEUE_URL}"
echo "Bucket        : ${EVIDENCE_BUCKET_NAME}"
echo "Worker role   : ${EVIDENCE_WORKER_ROLE_ARN}"
echo "Consumer role : ${EVIDENCE_CONSUMER_ROLE_ARN}"

echo "--- 1. Service accounts ---"
for name_role in "evidence-worker:${EVIDENCE_WORKER_ROLE_ARN}" "evidence-drain-consumer:${EVIDENCE_CONSUMER_ROLE_ARN}"; do
  sa="${name_role%%:*}"
  role="${name_role##*:}"
  kubectl create serviceaccount "$sa" --namespace "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl annotate serviceaccount "$sa" --namespace "$NAMESPACE" \
    "eks.amazonaws.com/role-arn=${role}" --overwrite
done

echo "--- 2. Migration job ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-migrate-job.yaml" | kubectl apply -f -

kubectl wait job/evidence-migrate --namespace "$NAMESPACE" --for=condition=complete --timeout=5m

echo "--- 3. Delivery worker ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  -e "s|EVIDENCE_QUEUE_URL_VALUE|${EVIDENCE_QUEUE_URL}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-delivery-worker.yaml" | kubectl apply -f -

kubectl rollout status deployment/evidence-worker --namespace "$NAMESPACE" --timeout=5m

echo "--- 4. Drain consumer ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  -e "s|EVIDENCE_QUEUE_URL_VALUE|${EVIDENCE_QUEUE_URL}|g" \
  -e "s|EVIDENCE_BUCKET_NAME_VALUE|${EVIDENCE_BUCKET_NAME}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-drain-consumer.yaml" | kubectl apply -f -

kubectl rollout status deployment/evidence-drain-consumer --namespace "$NAMESPACE" --timeout=5m

echo "--- 5. Maintenance CronJob ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  -e "s|MAINTENANCE_LIMIT_VALUE|${MAINTENANCE_LIMIT}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-maintenance-cronjob.yaml" | kubectl apply -f -

echo "--- Evidence delivery deployment complete ---"
echo "EVAL_EVIDENCE_ENABLED was not touched — delivery stays disabled until app-env-secret is updated explicitly."
echo "Watch worker logs:   kubectl logs -f deployment/evidence-worker -n ${NAMESPACE}"
echo "Watch consumer logs: kubectl logs -f deployment/evidence-drain-consumer -n ${NAMESPACE}"
```

- [ ] **Step 2: Syntax-check the script**

Run: `bash -n scripts/deploy_evidence_delivery.sh`
Expected: no output (exit 0)

If `shellcheck` is available, also run: `shellcheck scripts/deploy_evidence_delivery.sh`
Expected: no errors (warnings about the pattern already present in `deploy_ingestion.sh` are acceptable — match its existing style, don't invent a stricter one)

- [ ] **Step 3: Make it executable and add the bootstrap stage**

Run: `chmod +x scripts/deploy_evidence_delivery.sh`

In `scripts/bootstrap_cluster.sh`, change `MAX_STAGE=14` to `MAX_STAGE=15`, and add a new stage after the existing stage 14 block:

```bash
if stage_active 15; then
  echo "--- 15. Evidence delivery (dark-launched: EVAL_EVIDENCE_ENABLED stays false) ---"
  bash scripts/deploy_evidence_delivery.sh
fi
```

- [ ] **Step 4: Syntax-check `bootstrap_cluster.sh`**

Run: `bash -n scripts/bootstrap_cluster.sh`
Expected: no output (exit 0)

- [ ] **Step 5: Commit**

```bash
git add scripts/deploy_evidence_delivery.sh scripts/bootstrap_cluster.sh
git commit -m "feat(deploy): add the evidence delivery deploy script and bootstrap stage 15"
```

---

## Task 12: Staging fake models and staging API deployment

**Files:**
- Create: `deploy/k8s/staging-fake-models.yaml`
- Create: `scripts/deploy_staging_evidence.sh`

**Interfaces:**
- Produces: `bash scripts/deploy_staging_evidence.sh` — stands up the `staging-evidence` namespace end to end (fake models, staging `app-env-secret`, staging API Helm release, then delegates to `scripts/deploy_evidence_delivery.sh NAMESPACE=staging-evidence TERRAFORM_DIR=infra/terraform-staging`).

- [ ] **Step 1: Write `deploy/k8s/staging-fake-models.yaml`**

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: fake-models
  namespace: staging-evidence
spec:
  replicas: 1
  selector:
    matchLabels:
      app: fake-models
  template:
    metadata:
      labels:
        app: fake-models
    spec:
      containers:
        - name: fake-models
          image: FAKE_MODELS_IMAGE
          imagePullPolicy: IfNotPresent
          ports:
            - containerPort: 8000
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: 250m, memory: 256Mi}
---
apiVersion: v1
kind: Service
metadata:
  name: fake-models
  namespace: staging-evidence
spec:
  selector:
    app: fake-models
  ports:
    - port: 8000
      targetPort: 8000
```

- [ ] **Step 2: Write `scripts/deploy_staging_evidence.sh`**

```bash
#!/bin/bash
# Stand up the staging-evidence namespace end to end: fake models, a staging app-env-secret,
# the API Helm release pointed at staging's own database, then the evidence delivery
# components via deploy_evidence_delivery.sh. Requires infra/terraform-staging to already be
# applied (Task 13) and the services/api and services/fake_models images already pushed to ECR.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

REGION="${REGION:-us-east-1}"
NAMESPACE="staging-evidence"
TERRAFORM_DIR="${ROOT_DIR}/infra/terraform-staging"

for command in aws kubectl terraform helm sed openssl; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

API_IMAGE_TAG="${API_IMAGE_TAG:-$(aws ecr describe-images --repository-name services/api --region "$REGION" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' --output text)}"
FAKE_MODELS_IMAGE_TAG="${FAKE_MODELS_IMAGE_TAG:-$(aws ecr describe-images --repository-name services/fake-models \
  --region "$REGION" --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' --output text 2>/dev/null || echo None)}"

if [[ -z "$FAKE_MODELS_IMAGE_TAG" || "$FAKE_MODELS_IMAGE_TAG" == "None" ]]; then
  echo "ERROR: no services/fake-models image found in ECR. Build and push it first:" >&2
  echo "  docker build -f services/fake_models/Dockerfile -t \${ECR_BASE}/services/fake-models:staging ." >&2
  exit 1
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "--- 1. Fake models ---"
sed -e "s|FAKE_MODELS_IMAGE|${ECR_BASE}/services/fake-models:${FAKE_MODELS_IMAGE_TAG}|g" \
  "${ROOT_DIR}/deploy/k8s/staging-fake-models.yaml" | kubectl apply -f -
kubectl rollout status deployment/fake-models --namespace "$NAMESPACE" --timeout=5m

echo "--- 2. Staging app-env-secret ---"
STAGING_DB_ENDPOINT="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_db_endpoint)"
STAGING_DB_SECRET_ARN="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_db_secret_arn)"
STAGING_DB_PASSWORD="$(aws secretsmanager get-secret-value --secret-id "$STAGING_DB_SECRET_ARN" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
STAGING_DATABASE_URL="postgresql+asyncpg://ragadmin:${STAGING_DB_PASSWORD}@${STAGING_DB_ENDPOINT}:5432/evidence_staging"
STAGING_JWT_SECRET="${STAGING_JWT_SECRET:-$(openssl rand -hex 32)}"
STAGING_IDENTITY_KEY="${STAGING_IDENTITY_KEY:-$(openssl rand -hex 32)}"

kubectl create secret generic app-env-secret --namespace "$NAMESPACE" \
  --from-literal=DATABASE_URL="$STAGING_DATABASE_URL" \
  --from-literal=JWT_SECRET_KEY="$STAGING_JWT_SECRET" \
  --from-literal=EVAL_IDENTITY_HASH_KEY="$STAGING_IDENTITY_KEY" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "--- 3. Staging API Helm release ---"
helm upgrade --install api-staging "${ROOT_DIR}/deploy/helm/api" \
  --namespace "$NAMESPACE" \
  --set image.repository="${ECR_BASE}/services/api" \
  --set image.tag="${API_IMAGE_TAG}" \
  --set replicaCount=1 \
  --set fullnameOverride=api-staging \
  --set-string env[0].name=ENV --set-string env[0].value=local \
  --set-string env[1].name=LOG_LEVEL --set-string env[1].value=INFO \
  --set-string env[2].name=AWS_REGION --set-string env[2].value="$REGION" \
  --set-string env[3].name=RAY_LLM_ENDPOINT --set-string env[3].value="http://fake-models:8000/v1/chat/completions" \
  --set-string env[4].name=RAY_EMBED_ENDPOINT --set-string env[4].value="http://fake-models:8000/api/embeddings" \
  --set-string env[5].name=EVAL_EVIDENCE_ENABLED --set-string env[5].value="true" \
  --set-string env[6].name=EVAL_CAPTURE_CONTENT --set-string env[6].value="false" \
  --set-string env[7].name=EVAL_QUEUE_URL --set-string env[7].value="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_evidence_queue_url)" \
  --wait --timeout 5m

echo "--- 4. Evidence delivery components ---"
NAMESPACE="$NAMESPACE" TERRAFORM_DIR="$TERRAFORM_DIR" bash "${SCRIPT_DIR}/deploy_evidence_delivery.sh"

echo "--- Staging evidence stack deployed ---"
echo "Port-forward the staging API: kubectl port-forward -n ${NAMESPACE} deployment/api-staging 18000:8000"
```

- [ ] **Step 3: Syntax-check both files**

Run: `bash -n scripts/deploy_staging_evidence.sh`
Expected: no output

Run: `python3 -c "import yaml; list(yaml.safe_load_all(open('deploy/k8s/staging-fake-models.yaml')))"`
Expected: no exception

- [ ] **Step 4: Make executable and commit**

```bash
chmod +x scripts/deploy_staging_evidence.sh
git add deploy/k8s/staging-fake-models.yaml scripts/deploy_staging_evidence.sh
git commit -m "feat(deploy): stand up the staging-evidence namespace for validation"
```

---

## Task 13: Staging validation (requires live AWS access and explicit go-ahead)

**This task runs real commands against real AWS infrastructure.** Every command below needs the user's explicit go-ahead immediately before it runs, and needs AWS credentials with access to the `rag-platform-cluster` account — confirm both are available before starting. Do not proceed past Step 1 without that confirmation.

**Files:** none (validation only; may produce a small fix commit if staging surfaces a bug in an earlier task's code).

- [ ] **Step 1: Confirm access, then apply staging Terraform**

Confirm with the user: AWS credentials are active, and they approve applying `infra/terraform-staging` now.

Run: `cd infra/terraform-staging && terraform init && terraform plan`

Review the plan output with the user before applying — in particular confirm `data.aws_db_subnet_group.existing`'s name resolved correctly (Task 8, Step 3) and the KMS/S3/SQS/IAM resource count matches what Task 8 wrote.

Run (after explicit approval of the plan): `terraform apply`

- [ ] **Step 2: Push images and deploy the staging stack**

Confirm `services/api` and `services/fake-models` images exist in ECR (build and push if not, following the existing image-build process this repo already uses for those images).

Run: `bash scripts/deploy_staging_evidence.sh`

- [ ] **Step 3: Exercise the signed synthetic path**

```bash
kubectl port-forward -n staging-evidence deployment/api-staging 18000:8000 &
python3 - <<'EOF'
import time, jwt, httpx
token = jwt.encode({"sub": "synthetic-user", "tenant_id": "synthetic-tenant", "exp": time.time() + 300},
                   "<the STAGING_JWT_SECRET printed by deploy_staging_evidence.sh>", algorithm="HS256")
r = httpx.post("http://127.0.0.1:18000/api/v1/chat/stream", json={"message": "hello there"},
              headers={"Authorization": "Bearer " + token}, timeout=60)
print(r.status_code, r.headers.get("x-run-id"))
EOF
```

Expected: `200` and a run ID.

- [ ] **Step 4: Confirm the outbox row, delivery, and archive**

```bash
STAGING_DB_ENDPOINT=$(terraform -chdir=infra/terraform-staging output -raw staging_db_endpoint)
# retrieve the password the same way deploy_staging_evidence.sh did, via Secrets Manager
psql "postgresql://ragadmin:<password>@${STAGING_DB_ENDPOINT}:5432/evidence_staging" \
  -c "select r.run_id, o.state, o.attempts from evaluation_runs r join evaluation_outbox o using (run_pk) order by r.created_at desc limit 3;"
```

Expected: the row from Step 3 shows `state = 'delivered'`.

Run: `aws s3 ls s3://$(terraform -chdir=infra/terraform-staging output -raw staging_evidence_bucket_name)/queue-archive/`
Expected: one object named after the run ID from Step 3.

- [ ] **Step 5: Kill the worker mid-delivery and confirm restart recovery**

```bash
kubectl delete pod -n staging-evidence -l app=evidence-worker
# send another signed request per Step 3 while the pod is restarting
```

Expected: the new pod picks up and delivers it; check the outbox row again as in Step 4.

- [ ] **Step 6: Prove the KMS bucket policy actually rejects a non-KMS write**

This is the negative test Moto cannot exercise — the whole reason this must run against real AWS:

```bash
aws s3api put-object --bucket "$(terraform -chdir=infra/terraform-staging output -raw staging_evidence_bucket_name)" \
  --key queue-archive/should-be-rejected.json --body /dev/null
```

Expected: an `AccessDenied` error, because this request does not include `--server-side-encryption aws:kms --ssekms-key-id <key>`. If it succeeds instead, the bucket policy from Task 8 has a bug — stop, fix `data.aws_iam_policy_document.staging_evidence_bucket` (and the matching production one in Task 7), re-apply, and re-run this check before continuing.

- [ ] **Step 7: Exercise the maintenance CLI**

```bash
kubectl create job --from=cronjob/evidence-maintenance evidence-maintenance-manual-dryrun -n staging-evidence
kubectl logs -n staging-evidence job/evidence-maintenance-manual-dryrun -f
```

Note: the CronJob template runs `--apply`; for a first dry inspection, temporarily edit a copy of the job command to `--dry-run` before creating it manually, or just trust `--apply --limit 100` against staging's disposable data — either is acceptable for a first staging run since nothing here is real user data.

- [ ] **Step 8: Confirm /metrics is populated**

```bash
kubectl port-forward -n staging-evidence deployment/api-staging 18000:8000 &
curl -s http://127.0.0.1:18000/metrics | grep evidence_
```

Expected: `evidence_delivery_events_total`, `evidence_pending_runs`, and `evidence_quarantine_records` all present with real sample values.

- [ ] **Step 9: Record results and decide on teardown**

Write a short note (in a commit message or the PR description, not a new doc file unless the user asks) recording what passed, any fixes needed, and whether staging stays running or gets torn down (`terraform -chdir=infra/terraform-staging destroy`, again with explicit go-ahead). If any step in this task required a code fix, that fix is its own small commit before proceeding to Task 14.

---

## Task 14: Production dark-launch, flag flip, and operations documentation

**This task deploys to and modifies real production infrastructure.** Every command needs the user's explicit go-ahead immediately before it runs. Only proceed after Task 13 passed cleanly.

**Files:**
- Modify: `docs/evaluation_implementation/evidence-delivery-operations.md`

- [ ] **Step 1: Apply production Terraform**

Confirm with the user, then run: `cd infra/terraform && terraform plan` (review together), then `terraform apply` after explicit approval.

- [ ] **Step 2: Dark-launch the deploy**

Confirm with the user, then run: `bash scripts/bootstrap_cluster.sh --start 15 --end 15` (or `bash scripts/deploy_evidence_delivery.sh` directly — same effect).

Confirm `EVAL_EVIDENCE_ENABLED` is not in `app-env-secret` yet (or is `false`):

Run: `kubectl get secret app-env-secret -o jsonpath='{.data.EVAL_EVIDENCE_ENABLED}' | base64 -d 2>/dev/null; echo`
Expected: empty or `false`

Apply the PodMonitor: `kubectl apply -f deploy/monitoring/evidence-service-monitor.yaml`

- [ ] **Step 3: Bake period with delivery disabled**

Watch `kubectl logs -f deployment/evidence-worker` and `kubectl logs -f deployment/evidence-drain-consumer` briefly to confirm both start cleanly with no errors, even though there's nothing to claim yet.

- [ ] **Step 4: Generate and set `EVAL_IDENTITY_HASH_KEY`, then flip the flag**

Confirm with the user, then:

```bash
EVAL_IDENTITY_HASH_KEY="$(openssl rand -hex 32)"
kubectl patch secret app-env-secret --type merge -p \
  "{\"stringData\": {\"EVAL_IDENTITY_HASH_KEY\": \"${EVAL_IDENTITY_HASH_KEY}\", \"EVAL_EVIDENCE_ENABLED\": \"true\", \"EVAL_CAPTURE_CONTENT\": \"false\", \"EVAL_QUEUE_URL\": \"$(terraform -chdir=infra/terraform output -raw evidence_queue_url)\"}}"
kubectl rollout restart deployment/api
kubectl rollout status deployment/api --timeout=5m
```

- [ ] **Step 5: Watch the bake period**

For a duration the user decides at the time (design Section 3 leaves this open), watch:

```bash
curl -s http://<api-pod-via-port-forward>:8000/metrics | grep -E "evidence_pending_runs|evidence_quarantine_records"
kubectl logs -f deployment/evidence-drain-consumer | grep -i conflict
```

Expected: `evidence_pending_runs` stays low (not climbing unboundedly), `evidence_quarantine_records` stays at or near 0, no conflict log lines.

If anything looks wrong: `kubectl patch secret app-env-secret --type merge -p '{"stringData": {"EVAL_EVIDENCE_ENABLED": "false"}}' && kubectl rollout restart deployment/api` — this is the instant, safe rollback from Section 3 of the design.

- [ ] **Step 6: Update the operations doc**

In `docs/evaluation_implementation/evidence-delivery-operations.md`, add a new section after the existing "Verification commands" section (before "Production gates that local success does not satisfy" — and update that section's framing, since some of what it lists is no longer purely local):

```markdown
## Staging and production (PR 5a)

Metadata-only delivery now runs outside local Compose. The staging environment
(`infra/terraform-staging`, namespace `staging-evidence`) mirrors production evidence
infrastructure without GPUs or real inference — see [PR 5 design](pr-05-production-evidence-design.md).

Deploy or update the evidence components (migration job, delivery worker, drain consumer,
maintenance CronJob) in either environment:

```bash
NAMESPACE=default TERRAFORM_DIR=infra/terraform bash scripts/deploy_evidence_delivery.sh        # production
NAMESPACE=staging-evidence TERRAFORM_DIR=infra/terraform-staging bash scripts/deploy_evidence_delivery.sh  # staging
```

Both ship dark — `EVAL_EVIDENCE_ENABLED` is never set by these scripts. Flipping it in production
is a separate, explicit `kubectl patch secret app-env-secret` step (see Task 14 of the PR 5a
implementation plan for the exact commands), and disabling it again is the instant rollback.

### The drain consumer

A temporary placeholder until Phase 3's evaluation-worker consumer exists. It archives every
committed envelope to `s3://<evidence-bucket>/queue-archive/<run_id>.json`; a redelivery with
identical bytes is a no-op, and a genuinely conflicting hash for the same run_id is written to
`queue-archive-conflict/` instead of overwriting the original, and increments
`evidence_archive_conflicts_total`. It is not a scorer and makes no grading decisions.

### Metrics

`/metrics` on the API now serves the full Prometheus registry, including
`evidence_delivery_events_total`, `evidence_pending_runs`, `evidence_pending_bytes`,
`evidence_pending_oldest_age_seconds`, `evidence_quarantine_records`,
`evidence_quarantine_oldest_age_seconds`, `evidence_processing_seconds`, and
`evidence_payload_bytes` — scraped in-cluster only via `deploy/monitoring/evidence-service-monitor.yaml`.
Unlike the local-only state this doc originally described, the worker and maintenance CronJob
now also record their own `publisher`, `worker`, `cleanup`, and `maintenance` component events,
and quarantine gauges are refreshed once per worker claim cycle.

### What's still not covered

Retrieval ACL enforcement does not exist, so content capture (`EVAL_CAPTURE_CONTENT=true`)
must not be enabled in production regardless of this section — that is PR 5b, and it is blocked
on that prerequisite. See [PR 5 design, Section 4](pr-05-production-evidence-design.md#pr-5b-content-capture-enablement).
```

- [ ] **Step 7: Commit the docs update**

```bash
git add docs/evaluation_implementation/evidence-delivery-operations.md
git commit -m "docs(evaluation): document staging and production evidence delivery"
```

- [ ] **Step 8: Update Gap 1's PR 5 status**

In `docs/evaluation_implementation/gap-01-production-run-instrumentation.md`, change the PR 5 row in the status table from `Not started` to reflect 5a's completion and 5b's remaining block, and add a short "Implemented: PR 5a" subsection analogous to the existing PR 4 one, recording the exact verification results from Task 13 (staging) and Task 14 (production bake). This mirrors exactly how PR 4's Task 11 updated that same document — follow that section's structure and level of detail.

```bash
git add docs/evaluation_implementation/gap-01-production-run-instrumentation.md
git commit -m "docs(evaluation): record PR 5a completion in Gap 1 progress"
```
