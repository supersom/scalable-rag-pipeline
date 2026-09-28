"""Retention maintenance: metadata expiry and coordinated orphan cleanup.

The first half drives RetentionService against scripted doubles (ordering, leases, fail-closed
paths). The second half is opt-in live PostgreSQL + Moto S3 (RUN_EVAL_POSTGRES_TESTS=1), using the
real OutboxRepository and S3PayloadStore with one injected UTC clock.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from services.api.app.evaluation.delivery_models import (
    DeliveryReason, ExportEnvelope, ManifestEntry, PayloadSlot, PublishReceipt, RedactedPayload,
)
from services.api.app.evaluation.retention import RetentionService

import retention_fakes as fakes  # test-only module beside this file (pytest prepend import mode)
from retention_fakes import FakeRepo, FakeStore, calls, run_namespace, versions_for

pytestmark = pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning:botocore')

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now=T0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now += timedelta(**delta)
        return self.now


class Metrics:
    def __init__(self):
        self.events = []

    def inc(self, component, outcome, reason=None):
        self.events.append((component, outcome, getattr(reason, "value", reason)))


def service(repo, store, clock, **kwargs):
    return RetentionService(repo, store, clock, monotonic=lambda: 100.0, **kwargs)


@pytest.fixture
def fake_clock():
    return Clock()


@pytest.fixture
def events():
    return []


# --- Dry-run ----------------------------------------------------------------------

def test_dry_run_uses_only_read_only_queries(fake_clock, events):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns, run_namespace(2)], eligible=[ns], expired_runs=2)
    store = FakeStore(events, {ns: versions_for(ns, 2, markers=1)})
    report = asyncio.run(service(repo, store, fake_clock).run_once(apply=False, limit=10))
    assert (report.runs_expired, report.namespaces_examined, report.namespaces_eligible) == (2, 2, 1)
    assert (report.versions_examined, report.versions_deleted, report.namespaces_cleaned) == (3, 0, 0)
    assert report.failures == 0
    assert not fakes.MUTATING_REPO_CALLS & set(calls(events, "repo"))
    assert calls(events, "store") == ["list_versions"]
    assert len(store.objects[ns]) == 3


def test_preview_claim_never_authorizes_deletion(fake_clock, events):
    # A claim whose lease ends at "now" is a preview marker; it must never be used to delete.
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    repo.preview_like_claims = True
    store = FakeStore(events, {ns: versions_for(ns, 2)})
    metrics = Metrics()
    report = asyncio.run(service(repo, store, fake_clock, metrics=metrics).run_once(apply=True, limit=10))
    assert "delete_versions" not in calls(events, "store") and "list_versions" not in calls(events, "store")
    assert "complete_cleanup" not in calls(events, "repo")
    assert calls(events, "repo").count("claim_orphan") == 3  # bounded re-claims, then defer
    assert (report.versions_deleted, report.failures, report.namespaces_cleaned) == (0, 1, 0)
    assert ("cleanup", "failed", DeliveryReason.cleanup_failed.value) in metrics.events
    assert len(store.objects[ns]) == 2


# --- Apply: ordering, leases, resumability -----------------------------------------

def test_cleanup_marker_is_committed_before_any_remote_io(fake_clock, events):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns], expired_runs=1)
    store = FakeStore(events, {ns: versions_for(ns, 2, markers=1)})
    metrics = Metrics()
    report = asyncio.run(service(repo, store, fake_clock, metrics=metrics).run_once(apply=True, limit=10))
    assert [e[1] for e in events] == ["expire_runs", "cleanup_candidates", "claim_orphan", "list_versions",
                                      "delete_versions", "complete_cleanup"]
    assert (report.runs_expired, report.versions_examined, report.versions_deleted) == (1, 3, 3)
    assert (report.namespaces_cleaned, report.failures, report.deleted_versions) == (1, 0, 3)
    assert store.objects[ns] == [] and repo.completed == repo.claims
    assert metrics.events == [("cleanup", "cleaned", None)]


def test_lapsed_lease_mid_cleanup_reclaims_instead_of_deleting(fake_clock, events):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    store = FakeStore(events, {ns: versions_for(ns, 2)})
    lapses = iter([True])

    def slow_listing(prefix):  # the first listing outlives the 5-minute cleanup lease
        if next(lapses, False):
            fake_clock.advance(minutes=6)
    store.on_list = slow_listing
    report = asyncio.run(service(repo, store, fake_clock).run_once(apply=True, limit=10))
    assert [e[1] for e in events] == ["expire_runs", "cleanup_candidates", "claim_orphan", "list_versions",
                                      "claim_orphan", "list_versions", "delete_versions", "complete_cleanup"]
    first, second = repo.claims
    assert first.expires_at <= fake_clock() < second.expires_at
    assert repo.completed == [second]
    assert (report.versions_deleted, report.namespaces_cleaned, report.failures) == (2, 1, 0)
    assert report.versions_examined == 2  # the abandoned listing is not double counted


def test_lease_lost_to_another_cleaner_defers_without_deleting(fake_clock, events):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    store = FakeStore(events, {ns: versions_for(ns, 1)})

    def lapse_and_lose(prefix):
        fake_clock.advance(minutes=6)
        repo.eligible.discard(ns)  # another cleaner re-claimed the namespace
    store.on_list = lapse_and_lose
    report = asyncio.run(service(repo, store, fake_clock).run_once(apply=True, limit=10))
    assert "delete_versions" not in calls(events, "store")
    assert (report.versions_deleted, report.namespaces_cleaned) == (0, 0)


@pytest.mark.parametrize("fault", ["fail_deletes", "undeletable", "fail_lists"])
def test_failed_or_partial_delete_is_not_completed_and_resumes(fake_clock, events, fault):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    store = FakeStore(events, {ns: versions_for(ns, 2, markers=1)})
    setattr(store, fault, 1)
    metrics = Metrics()
    first = asyncio.run(service(repo, store, fake_clock, metrics=metrics).run_once(apply=True, limit=10))
    assert "complete_cleanup" not in calls(events, "repo")
    assert (first.failures, first.namespaces_cleaned) == (1, 0)
    assert metrics.events == [("cleanup", "failed", DeliveryReason.cleanup_failed.value)]
    store.undeletable = 0
    fake_clock.advance(minutes=6)  # the unfinished claim lapses; a later run resumes
    second = asyncio.run(service(repo, store, fake_clock).run_once(apply=True, limit=10))
    assert (second.failures, second.namespaces_cleaned) == (0, 1)
    assert store.objects[ns] == []
    assert repo.completed == [repo.claims[-1]]


def test_completion_refused_after_delete_leaves_namespace_for_next_run(fake_clock, events):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    store = FakeStore(events, {ns: versions_for(ns, 1)})
    original = store.delete_versions

    async def slow_delete(versions, *, deadline):
        fake_clock.advance(minutes=6)
        return await original(versions, deadline=deadline)
    store.delete_versions = slow_delete
    report = asyncio.run(service(repo, store, fake_clock).run_once(apply=True, limit=10))
    assert (report.versions_deleted, report.namespaces_cleaned, report.failures) == (1, 0, 1)
    assert repo.completed == []


def test_empty_namespace_completes_without_delete_call(fake_clock, events):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    report = asyncio.run(service(repo, FakeStore(events), fake_clock).run_once(apply=True, limit=10))
    assert "delete_versions" not in calls(events, "store")
    assert (report.namespaces_cleaned, report.versions_examined, report.versions_deleted) == (1, 0, 0)


def test_empty_bucket_and_no_candidates_is_a_clean_noop(fake_clock, events):
    repo = FakeRepo(fake_clock, events)
    report = asyncio.run(service(repo, FakeStore(events), fake_clock).run_once(apply=True, limit=10))
    assert calls(events, "store") == []
    assert report.model_dump() == {"versions_examined": 0, "versions_deleted": 0, "failures": 0,
                                   "runs_expired": 0, "namespaces_examined": 0, "namespaces_eligible": 0,
                                   "namespaces_cleaned": 0}


def test_work_is_paginated_and_bounded_by_limit(fake_clock, events):
    namespaces = [run_namespace(n) for n in range(1, 6)]
    repo = FakeRepo(fake_clock, events, candidates=namespaces)
    store = FakeStore(events, {ns: versions_for(ns, 1) for ns in namespaces})
    report = asyncio.run(service(repo, store, fake_clock, page_size=2).run_once(apply=True, limit=3))
    assert report.namespaces_examined == 3 and report.namespaces_cleaned == 3
    assert repo.pages == [(None, 2), (namespaces[1], 1)]
    assert [ns for ns in namespaces if store.objects[ns]] == namespaces[3:]


def test_limit_must_be_positive(fake_clock, events):
    with pytest.raises(ValueError):
        asyncio.run(service(FakeRepo(fake_clock, events), FakeStore(events), fake_clock).run_once(apply=True, limit=0))


# --- Fail-closed coordination ------------------------------------------------------

@pytest.mark.parametrize("failing", ["expire_runs", "cleanup_candidates", "claim_orphan", "complete_cleanup"])
def test_database_unavailable_defers_and_counts_failure(fake_clock, events, failing):
    namespaces = [run_namespace(1), run_namespace(2)]
    repo = FakeRepo(fake_clock, events, candidates=namespaces)
    repo.unavailable.add(failing)
    store = FakeStore(events, {ns: versions_for(ns, 1) for ns in namespaces})
    metrics = Metrics()
    report = asyncio.run(service(repo, store, fake_clock, metrics=metrics).run_once(apply=True, limit=10))
    assert report.failures == 1 and report.namespaces_cleaned == 0
    assert metrics.events == [("cleanup", "failed", DeliveryReason.database_unavailable.value)]
    if failing != "complete_cleanup":
        assert "delete_versions" not in calls(events, "store")  # no cleanup permission without the DB
    assert calls(events, "repo").count("claim_orphan") <= 1  # stop the cycle once the DB is gone


@pytest.mark.parametrize("failing", ["count_expired_runs", "cleanup_candidates", "inspect_cleanup"])
def test_dry_run_database_unavailable_counts_failure(fake_clock, events, failing):
    ns = run_namespace(1)
    repo = FakeRepo(fake_clock, events, candidates=[ns])
    repo.unavailable.add(failing)
    report = asyncio.run(service(repo, FakeStore(events), fake_clock).run_once(apply=False, limit=10))
    assert report.failures == 1 and report.namespaces_eligible == 0


# --- Explicit operator targets -------------------------------------------------------

def test_explicit_namespaces_replace_discovery(fake_clock, events):
    target = run_namespace(9)
    repo = FakeRepo(fake_clock, events, candidates=[run_namespace(1)], eligible=[target])
    store = FakeStore(events, {target: versions_for(target, 1)})
    report = asyncio.run(service(repo, store, fake_clock).run_once(apply=True, limit=10, namespaces=(target,)))
    assert "cleanup_candidates" not in calls(events, "repo")
    assert [e[2] for e in events if e[1] == "claim_orphan"] == [target]
    assert report.namespaces_cleaned == 1


@pytest.mark.parametrize("namespace", [
    f"evidence/test/{fakes.TENANT}/", "evidence/test/", "evidence/", "",
    f"evidence/test/{fakes.TENANT}/not-a-uuid/", f"evidence/test/{fakes.TENANT}/{uuid4()}",
    f"evidence/test/{fakes.TENANT}/{uuid4()}/2026-01-01/", f"/evidence/test/{fakes.TENANT}/{uuid4()}/",
])
def test_explicit_targets_must_be_exact_run_namespaces(fake_clock, events, namespace):
    repo = FakeRepo(fake_clock, events)
    with pytest.raises(ValueError):
        asyncio.run(service(repo, FakeStore(events), fake_clock).run_once(apply=True, limit=10,
                                                                         namespaces=(namespace,)))
    assert events == []  # rejected before any database or storage call


def test_discovered_non_run_namespace_is_skipped(fake_clock, events):
    broad = f"evidence/test/{fakes.TENANT}/"
    repo = FakeRepo(fake_clock, events, candidates=[broad])
    store = FakeStore(events, {broad: versions_for(broad, 1)})
    report = asyncio.run(service(repo, store, fake_clock).run_once(apply=True, limit=10))
    assert "claim_orphan" not in calls(events, "repo") and calls(events, "store") == []
    assert report.namespaces_examined == 1 and report.namespaces_cleaned == 0


def test_without_content_store_only_metadata_expires(fake_clock, events):
    repo = FakeRepo(fake_clock, events, candidates=[run_namespace(1)], expired_runs=4)
    report = asyncio.run(service(repo, None, fake_clock).run_once(apply=True, limit=10))
    assert calls(events, "repo") == ["expire_runs"]
    assert report.runs_expired == 4 and report.namespaces_examined == 0


def test_database_outage_with_real_repository_never_deletes(s3_client, fake_clock):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from services.api.app.evaluation.outbox import OutboxRepository
    store = make_store(s3_client, fake_clock)
    ref = asyncio.run(store.put(uuid4(), fakes.TENANT, payload(), deadline=1e12))
    namespace = ref.key.rsplit("2026-", 1)[0]
    engine = create_async_engine("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none")
    repo = OutboxRepository(async_sessionmaker(engine), fake_clock)
    try:
        report = asyncio.run(RetentionService(repo, store, fake_clock).run_once(
            apply=True, limit=10, namespaces=(namespace,)))
        assert report.failures == 1 and report.versions_deleted == 0
        assert len(asyncio.run(store.list_versions(namespace, deadline=1e12))) == 1
    finally:
        asyncio.run(engine.dispose())
        asyncio.run(store.close(1e12))


# --- Live PostgreSQL + Moto S3 ---------------------------------------------------------

EVIDENCE_BUCKET = "synthetic-evidence-payloads"
POLICY = "restricted-synthetic-v1"
ACK = PublishReceipt(acknowledgement_id="synthetic-ack-1")


def make_store(client, clock):
    from services.api.app.evaluation.bounded_io import BoundedIO
    from services.api.app.evaluation.payloads import S3PayloadStore
    return S3PayloadStore(client, bucket=EVIDENCE_BUCKET, kms_key_id="synthetic-dedicated-evidence-key",
                          environment="test", io=BoundedIO(2), clock=clock, policy_versions=(POLICY,))


def payload(kind="retrieval_chunk", item=1, data=b'{"text":"synthetic [REDACTED]"}'):
    slot = PayloadSlot(kind=kind, ordinal=1, item_ordinal=None if kind in ("input", "output") else item)
    return RedactedPayload(slot=slot, data=data, policy_version=POLICY)


@pytest.fixture
def evidence_bucket():
    return EVIDENCE_BUCKET


@pytest.fixture
def store(s3_client, clock):
    store = make_store(s3_client, clock)
    yield store
    assert asyncio.run(store.close(1e12)) == 0


@pytest.fixture
def retention(repo, store, clock):
    return RetentionService(repo, store, clock)


@pytest.fixture
def intent(repo, store, terminal_event):
    run_id = UUID(str(terminal_event.run_id))
    return asyncio.run(repo.begin_upload(run_id, store.namespace(fakes.TENANT, run_id), lease_seconds=60))


def stored_envelope(base, store, run_id, *, items=2):
    entries = []
    for item in range(1, items + 1):
        ref = asyncio.run(store.put(run_id, fakes.TENANT, payload(item=item), deadline=1e12))
        entries.append(ManifestEntry(slot=ref.slot, state="stored", reference=ref))
    safe_run = base.safe_run.model_copy(update={"run_id": run_id})
    return ExportEnvelope(run_id=run_id, safe_run=safe_run, redaction_policy_version=base.redaction_policy_version,
                          manifest=tuple(entries), completeness="complete")


@pytest.fixture
def envelope(envelope, intent, store):
    """Overrides the metadata-only envelope: two payloads really uploaded under the open intent."""
    return stored_envelope(envelope, store, intent.run_id)


async def table_rows(engine, table):
    from sqlalchemy import text
    async with engine.connect() as conn:
        return sorted(tuple(r) for r in await conn.execute(text(f"SELECT * FROM {table}")))


async def query(engine, sql):
    from sqlalchemy import text
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(text(sql))]


async def snapshot(engine):
    return {t: await table_rows(engine, t) for t in
            ("evaluation_runs", "evaluation_outbox", "evaluation_payload_refs", "evaluation_upload_intents")}


async def listed(store, namespace):
    return await store.list_versions(namespace, deadline=1e12)


# The brief's literal race contract.
async def cleanup_race_contract(repo, retention, intent, envelope):
    report = await retention.run_once(apply=True, limit=10)
    assert report.deleted_versions == 0  # Live upload intent protects objects.
    receipt = await repo.enqueue(envelope, intent=intent)
    assert receipt.status == 'committed'


def test_active_intent_prevents_orphan_delete(repo, retention, intent, envelope):
    asyncio.run(cleanup_race_contract(repo, retention, intent, envelope))


def test_upload_commit_race_never_deletes_live_objects(repo, retention, intent, envelope, store, clock, pg_engine):
    async def scenario():
        namespace = intent.namespace
        # Upload window: the open intent protects objects even from an explicit operator target.
        clock.advance(seconds=59)
        report = await retention.run_once(apply=True, limit=10, namespaces=(namespace,))
        assert (report.namespaces_eligible, report.deleted_versions) == (0, 0)
        assert (await repo.enqueue(envelope, intent=intent)).status == "committed"
        # Past intent expiry + 24h grace the committed references still protect every version.
        clock.advance(days=2)
        for targets in (None, (namespace,)):
            report = await retention.run_once(apply=True, limit=10, namespaces=targets)
            assert report.deleted_versions == 0
        assert len(await listed(store, namespace)) == 2
        for entry in envelope.manifest:
            assert await store.get(entry.reference, fakes.TENANT, deadline=1e12)
        assert await query(pg_engine, "SELECT envelope, export_hash FROM evaluation_runs") == [
            (envelope.canonical_bytes(), envelope.export_hash)]
    asyncio.run(scenario())


def test_expired_intent_claim_denies_commit_then_delete_resumes_after_restart(
        repo_factory, repo, retention, intent, envelope, store, s3_client, clock, pg_engine):
    from services.api.app.evaluation.payloads import PayloadStorageError

    async def scenario():
        namespace = intent.namespace
        key = envelope.manifest[0].reference.key
        s3_client.put_object(Bucket=EVIDENCE_BUCKET, Key=key, Body=b"{}")  # second version
        s3_client.delete_object(Bucket=EVIDENCE_BUCKET, Key=key)              # delete marker
        assert len(await listed(store, namespace)) == 4
        clock.advance(seconds=60, hours=24)  # intent expiry + grace, exactly
        original = store.delete_versions

        async def crash(versions, *, deadline):  # the cleaner dies after committing its claim
            raise PayloadStorageError("failed")
        store.delete_versions = crash
        report = await retention.run_once(apply=True, limit=10)
        assert (report.namespaces_eligible, report.failures, report.deleted_versions) == (1, 1, 0)
        store.delete_versions = original
        (state, token, done), = await _intent_rows(pg_engine)
        assert state == "cleanup_owned" and token is not None and done is None
        # The attempted commit is denied, including with a forged still-open intent.
        denied = await repo.enqueue(envelope, intent=intent)
        forged = intent.model_copy(update={"expires_at": clock() + timedelta(hours=1)})
        denied_forged = await repo.enqueue(envelope, intent=forged)
        assert {denied.reason, denied_forged.reason} == {DeliveryReason.intent_failed}
        assert (await snapshot(pg_engine))["evaluation_runs"] == []
        # While the crashed cleaner's lease is live, nobody else deletes.
        assert (await retention.run_once(apply=True, limit=10)).deleted_versions == 0
        # Process restart: fresh repository, store and service after the lease lapses.
        clock.advance(minutes=5)
        restarted = RetentionService(repo_factory(), make_store(s3_client, clock), clock)
        report = await restarted.run_once(apply=True, limit=10)
        assert (report.versions_deleted, report.namespaces_cleaned, report.failures) == (4, 1, 0)
        assert await listed(store, namespace) == ()
        (state, token, done), = await _intent_rows(pg_engine)
        assert state == "cleanup_owned" and token is None and done == clock()
        # Completed cleanup is not rediscovered.
        assert (await restarted.run_once(apply=True, limit=10)).namespaces_examined == 0
    asyncio.run(scenario())


async def _intent_rows(engine):
    from sqlalchemy import text
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(text(
            "SELECT state, cleanup_token, cleanup_completed_at FROM evaluation_upload_intents"))]


def test_dry_run_causes_no_mutation(repo, retention, intent, envelope, store, s3_client, clock, pg_engine):
    async def scenario():
        # One quarantined, expiring run plus one expired orphan namespace with versions/markers.
        committed = await repo.enqueue(envelope, intent=intent)
        assert committed.status == "committed"
        claim, = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "broker_denied", permanent=True)
        orphan_run = uuid4()
        orphan_intent = await repo.begin_upload(orphan_run, store.namespace(fakes.TENANT, orphan_run), 60)
        orphan_ref = await store.put(orphan_run, fakes.TENANT, payload(), deadline=1e12)
        s3_client.delete_object(Bucket=EVIDENCE_BUCKET, Key=orphan_ref.key)  # adds a delete marker
        clock.now = envelope.safe_run.completed_at + timedelta(days=90)
        before_db, before_s3 = await snapshot(pg_engine), await _all_versions(store, s3_client)
        preview = await retention.run_once(apply=False, limit=10)
        assert await snapshot(pg_engine) == before_db
        assert await _all_versions(store, s3_client) == before_s3
        assert (preview.runs_expired, preview.namespaces_eligible, preview.versions_examined) == (1, 2, 4)
        assert (preview.versions_deleted, preview.namespaces_cleaned, preview.failures) == (0, 0, 0)
        # Apply behaves exactly as if the dry-run never happened.
        applied = await retention.run_once(apply=True, limit=10)
        assert (applied.runs_expired, applied.namespaces_eligible, applied.versions_examined) == (1, 2, 4)
        assert (applied.versions_deleted, applied.namespaces_cleaned, applied.failures) == (4, 2, 0)
        assert await _all_versions(store, s3_client) == []
    asyncio.run(scenario())


async def _all_versions(store, s3_client):
    listing = s3_client.list_object_versions(Bucket=EVIDENCE_BUCKET)
    return sorted((v["Key"], v["VersionId"]) for group in ("Versions", "DeleteMarkers")
                  for v in listing.get(group, ()))


def test_content_expires_at_exactly_30_days_without_touching_metadata(
        repo, retention, intent, envelope, store, clock, pg_engine):
    async def scenario():
        assert (await repo.enqueue(envelope, intent=intent)).status == "committed"
        ref = envelope.manifest[0].reference
        clock.now = ref.expires_at - timedelta(seconds=1)
        assert await store.get(ref, fakes.TENANT, deadline=1e12)
        assert (await retention.run_once(apply=True, limit=10)).deleted_versions == 0
        before = await table_rows(pg_engine, "evaluation_runs")
        clock.now = ref.expires_at
        from services.api.app.evaluation.payloads import PayloadAccessDenied
        with pytest.raises(PayloadAccessDenied):
            await store.get(ref, fakes.TENANT, deadline=1e12)
        report = await retention.run_once(apply=True, limit=10)
        assert (report.deleted_versions, report.namespaces_cleaned, report.runs_expired) == (2, 1, 0)
        assert await listed(store, intent.namespace) == ()
        # Metadata is immutable and outlives content: same bytes and hash, still deliverable.
        assert await table_rows(pg_engine, "evaluation_runs") == before
        claim, = await repo.claim_batch(limit=1, lease_seconds=60)
        assert claim.envelope_bytes == envelope.canonical_bytes() and claim.export_hash == envelope.export_hash
    asyncio.run(scenario())


def test_metadata_expires_at_exactly_90_days_even_if_quarantined(repo, retention, envelope, intent, clock, pg_engine):
    async def scenario():
        assert (await repo.enqueue(envelope, intent=intent)).status == "committed"
        claim, = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "broker_denied", permanent=True)
        expiry = envelope.safe_run.completed_at + timedelta(days=90)
        clock.now = expiry - timedelta(microseconds=1)
        assert (await retention.run_once(apply=True, limit=10)).runs_expired == 0
        assert len((await snapshot(pg_engine))["evaluation_outbox"]) == 1
        clock.now = expiry
        assert (await retention.run_once(apply=True, limit=10)).runs_expired == 1
        remaining = await snapshot(pg_engine)
        assert remaining["evaluation_runs"] == remaining["evaluation_outbox"] == []
        assert remaining["evaluation_payload_refs"] == []
    asyncio.run(scenario())


def test_duplicate_admission_does_not_extend_retention(repo, retention, clock, pg_engine, terminal_event, policy):
    from services.api.app.evaluation.metadata_export import prepare_export, seal_export
    metadata_only = seal_export(prepare_export(terminal_event, policy, (), ()), ())

    async def scenario():
        assert (await repo.enqueue(metadata_only, intent=None)).status == "committed"
        clock.advance(days=10)
        assert (await repo.enqueue(metadata_only, intent=None)).status == "committed"
        clock.now = metadata_only.safe_run.completed_at + timedelta(days=90) - timedelta(microseconds=1)
        assert (await retention.run_once(apply=True, limit=10)).runs_expired == 0
        clock.advance(microseconds=1)
        assert (await retention.run_once(apply=True, limit=10)).runs_expired == 1
        assert (await snapshot(pg_engine))["evaluation_runs"] == []
    asyncio.run(scenario())


def test_absent_intent_namespace_gets_marker_only_via_explicit_target(repo, retention, store, clock, pg_engine):
    async def scenario():
        run_id = uuid4()
        namespace = store.namespace(fakes.TENANT, run_id)
        await store.put(run_id, fakes.TENANT, payload(), deadline=1e12)  # no intent row at all
        assert (await retention.run_once(apply=True, limit=10)).namespaces_examined == 0
        report = await retention.run_once(apply=True, limit=10, namespaces=(namespace,))
        assert (report.versions_deleted, report.namespaces_cleaned) == (1, 1)
        from sqlalchemy import text
        async with pg_engine.connect() as conn:
            row, = (await conn.execute(text(
                "SELECT namespace, run_id, state, cleanup_token, cleanup_completed_at "
                "FROM evaluation_upload_intents"))).all()
        assert tuple(row) == (namespace, None, "cleanup_owned", None, clock())
    asyncio.run(scenario())


def test_quarantine_is_never_automatically_requeued(repo, retention, envelope, intent, clock, pg_engine):
    async def scenario():
        assert (await repo.enqueue(envelope, intent=intent)).status == "committed"
        claim, = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "broker_denied", permanent=True)
        clock.advance(days=3)
        await retention.run_once(apply=True, limit=10)
        assert await repo.claim_batch(limit=10, lease_seconds=60) == ()
        assert await query(pg_engine, "SELECT state FROM evaluation_outbox") == [("quarantined",)]
    asyncio.run(scenario())
