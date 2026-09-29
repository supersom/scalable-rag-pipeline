"""Outbox, upload-intent, and cleanup coordination against a real local PostgreSQL schema.

Opt-in: RUN_EVAL_POSTGRES_TESTS=1 EVAL_TEST_DATABASE_URL=postgresql+asyncpg://... ; each test runs
in its own generated schema that is dropped afterwards.
"""
import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text

from services.api.app.evaluation.delivery_models import (
    DeliveryReason, EventIdentity, ExportEnvelope, ManifestEntry, PayloadKind, PayloadRef, PayloadSlot,
    PublishReceipt,
)
from services.api.app.evaluation.outbox import EvidenceConflict

ACK = PublishReceipt(acknowledgement_id="synthetic-ack-1")


def namespace_for(run_id):
    return f"evidence/test/synthetic-tenant/{run_id}/"


def make_envelope(base, *, run_id=None, refs=0, created=None, completeness=None, namespace=None):
    run_id = run_id or uuid4()
    namespace = namespace or namespace_for(run_id)
    safe_run = base.safe_run.model_copy(update={"run_id": run_id})
    manifest = []
    for ordinal in range(1, refs + 1):
        slot = PayloadSlot(kind=PayloadKind.retrieval_chunk, ordinal=1, item_ordinal=ordinal)
        ref = PayloadRef(bucket="synthetic-evidence-payloads",
                         key=f"{namespace}2026-01-01/retrieval_chunk/1/{ordinal}",
                         version_id=f"v{ordinal}", tenant_partition="synthetic-tenant", slot=slot,
                         byte_size=10, sha256="b" * 64, policy_version=base.redaction_policy_version,
                         created_at=created, expires_at=created + timedelta(days=30))
        manifest.append(ManifestEntry(slot=slot, state="stored", reference=ref))
    return ExportEnvelope(run_id=run_id, safe_run=safe_run, redaction_policy_version=base.redaction_policy_version,
                          manifest=tuple(manifest),
                          completeness=completeness or ("complete" if manifest else "metadata_only"))


async def rows(engine, sql, **params):
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(text(sql), params)]


async def count(engine, table):
    return (await rows(engine, f"SELECT count(*) FROM {table}"))[0][0]


# --- The brief's literal duplicate contract --------------------------------------------

async def duplicate_contract(repo, envelope):
    a = await repo.enqueue(envelope, intent=None)
    b = await repo.enqueue(envelope, intent=None)
    assert a.status == b.status == 'committed'
    claims = await repo.claim_batch(limit=10, lease_seconds=60)
    assert len(claims) == 1
    assert claims[0].envelope_bytes == envelope.canonical_bytes()


def test_duplicate_identity(repo, envelope):
    asyncio.run(duplicate_contract(repo, envelope))


# --- Transactional insertion --------------------------------------------------------

def test_commit_inserts_run_outbox_refs_and_closes_intent(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        assert intent.state == "open" and intent.expires_at == clock() + timedelta(seconds=60)
        stored = make_envelope(envelope, run_id=run_id, refs=2, created=clock())
        receipt = await repo.enqueue(stored, intent=intent)
        assert receipt.status == "committed"
        assert receipt.identity == EventIdentity(run_id=run_id) and receipt.export_hash == stored.export_hash
        run = await rows(pg_engine, "SELECT envelope, export_hash, expires_at FROM evaluation_runs")
        assert run == [(stored.canonical_bytes(), stored.export_hash,
                        stored.safe_run.completed_at + timedelta(days=90))]
        assert await rows(pg_engine, "SELECT state, attempts FROM evaluation_outbox") == [("pending", 0)]
        assert await count(pg_engine, "evaluation_payload_refs") == 2
        assert await rows(pg_engine, "SELECT state FROM evaluation_upload_intents") == [("closed",)]
    asyncio.run(scenario())


def test_failed_reference_insert_rolls_back_everything(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        good = make_envelope(envelope, run_id=run_id, refs=2, created=clock())
        # Two manifest slots claiming the same object version violate reference uniqueness late in the txn.
        first, second = good.manifest
        clash = second.model_copy(update={"reference": second.reference.model_copy(
            update={"key": first.reference.key, "version_id": first.reference.version_id})})
        bad = ExportEnvelope(run_id=run_id, safe_run=good.safe_run, redaction_policy_version=good.redaction_policy_version,
                             manifest=(first, clash), completeness="complete")
        receipt = await repo.enqueue(bad, intent=intent)
        assert receipt.status == "failed"
        for table in ("evaluation_runs", "evaluation_outbox", "evaluation_payload_refs"):
            assert await count(pg_engine, table) == 0
        assert await rows(pg_engine, "SELECT state FROM evaluation_upload_intents") == [("open",)]
        # The intent is still usable: a correct envelope commits afterwards.
        assert (await repo.enqueue(good, intent=intent)).status == "committed"
    asyncio.run(scenario())


def test_conflicting_duplicate_never_overwrites_evidence(repo, pg_engine, envelope):
    async def scenario():
        original = make_envelope(envelope)
        assert (await repo.enqueue(original, intent=None)).status == "committed"
        changed = make_envelope(envelope, run_id=original.run_id, completeness="partial")
        assert changed.export_hash != original.export_hash
        receipt = await repo.enqueue(changed, intent=None)
        assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.identity_conflict)
        assert await rows(pg_engine, "SELECT export_hash FROM evaluation_runs") == [(original.export_hash,)]
    asyncio.run(scenario())


def test_duplicate_admission_does_not_extend_expiry(repo, pg_engine, envelope, clock):
    async def scenario():
        await repo.enqueue(envelope, intent=None)
        before = await rows(pg_engine, "SELECT created_at, expires_at FROM evaluation_runs")
        clock.advance(days=10)
        assert (await repo.enqueue(envelope, intent=None)).status == "committed"
        assert await rows(pg_engine, "SELECT created_at, expires_at FROM evaluation_runs") == before
        assert await count(pg_engine, "evaluation_outbox") == 1
    asyncio.run(scenario())


def test_concurrent_duplicates_commit_once(repo_factory, pg_engine, envelope):
    async def scenario():
        receipts = await asyncio.gather(*(repo_factory().enqueue(envelope, intent=None) for _ in range(4)))
        assert {r.status for r in receipts} == {"committed"}
        assert await count(pg_engine, "evaluation_outbox") == 1
    asyncio.run(scenario())


# --- Leased delivery ----------------------------------------------------------------

def test_restart_recovers_claims_and_rejects_stale_acknowledgement(repo_factory, envelope, clock):
    async def scenario():
        first = repo_factory()
        await first.enqueue(envelope, intent=None)
        [stale] = await first.claim_batch(limit=10, lease_seconds=60)
        assert stale.attempts == 1 and stale.lease_expires_at == clock() + timedelta(seconds=60)
        restarted = repo_factory()  # the first dispatcher "crashed" without acknowledging
        assert await restarted.claim_batch(limit=10, lease_seconds=60) == ()
        clock.advance(seconds=60)
        [fresh] = await restarted.claim_batch(limit=10, lease_seconds=60)
        assert fresh.lease_token != stale.lease_token and fresh.attempts == 2
        assert (fresh.envelope_bytes, fresh.export_hash) == (stale.envelope_bytes, stale.export_hash)
        assert await first.mark_delivered(stale, ACK) is False
        assert await first.schedule_retry(stale, "broker_transient", permanent=False) is False
        assert await restarted.mark_delivered(fresh, ACK) is True
        assert await restarted.mark_delivered(fresh, ACK) is False
        clock.advance(days=1)
        assert await restarted.claim_batch(limit=10, lease_seconds=60) == ()
    asyncio.run(scenario())


def test_expired_lease_cannot_acknowledge_even_without_reclaim(repo, envelope, clock):
    async def scenario():
        await repo.enqueue(envelope, intent=None)
        [claim] = await repo.claim_batch(limit=10, lease_seconds=60)
        forged = claim.model_copy(update={"lease_token": uuid4()})
        assert await repo.mark_delivered(forged, ACK) is False
        clock.advance(seconds=60)
        assert await repo.mark_delivered(claim, ACK) is False
        [again] = await repo.claim_batch(limit=10, lease_seconds=60)
        assert again.envelope_bytes == envelope.canonical_bytes()
    asyncio.run(scenario())


def test_two_dispatchers_claim_disjoint_rows_without_blocking(repo_factory, pg_engine, envelope):
    async def scenario():
        repo_a, repo_b = repo_factory(), repo_factory()
        envelopes = [make_envelope(envelope) for _ in range(6)]
        for item in envelopes:
            assert (await repo_a.enqueue(item, intent=None)).status == "committed"
        async with pg_engine.connect() as holder:
            # Another transaction holds row locks on two rows; claims must skip them, not wait.
            locked = await holder.execute(text(
                "SELECT id FROM evaluation_outbox ORDER BY id LIMIT 2 FOR UPDATE"))
            assert len(locked.all()) == 2
            got_a, got_b = await asyncio.wait_for(asyncio.gather(
                repo_a.claim_batch(limit=2, lease_seconds=60), repo_b.claim_batch(limit=2, lease_seconds=60)), 10)
            await holder.rollback()
        ids_a = {c.identity.run_id for c in got_a}
        ids_b = {c.identity.run_id for c in got_b}
        assert len(ids_a) == len(ids_b) == 2 and not ids_a & ids_b
        rest = await repo_a.claim_batch(limit=10, lease_seconds=60)
        assert len(rest) == 2 and not {c.identity.run_id for c in rest} & (ids_a | ids_b)
    asyncio.run(scenario())


def test_retry_backoff_then_exhaustion_quarantines(repo_factory, pg_engine, envelope, clock):
    async def scenario():
        repo = repo_factory(max_attempts=2)
        await repo.enqueue(envelope, intent=None)
        [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "broker_transient", permanent=False) is True
        assert await repo.claim_batch(limit=1, lease_seconds=60) == ()  # backoff not yet elapsed
        clock.advance(seconds=61)
        [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
        assert claim.attempts == 2 and claim.envelope_bytes == envelope.canonical_bytes()
        assert await repo.schedule_retry(claim, "broker_transient", permanent=False) is True
        assert await rows(pg_engine, "SELECT state, error_class FROM evaluation_outbox") == [
            ("quarantined", "retry_exhausted")]
        clock.advance(days=1)
        assert await repo.claim_batch(limit=1, lease_seconds=60) == ()
        identity = EventIdentity(run_id=envelope.run_id)
        assert await repo.retry_quarantined(identity) is True
        assert await repo.retry_quarantined(identity) is False  # already pending
        [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
        assert claim.envelope_bytes == envelope.canonical_bytes() and claim.export_hash == envelope.export_hash
    asyncio.run(scenario())


def test_lapsed_leases_count_toward_the_attempt_cap(repo_factory, pg_engine, envelope, clock):
    async def scenario():
        # Dispatchers killed mid-send never call schedule_retry; the cap must still hold.
        repo = repo_factory(max_attempts=2)
        await repo.enqueue(envelope, intent=None)
        for attempt in (1, 2):
            [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
            assert claim.attempts == attempt
            clock.advance(seconds=60)
        assert await repo.claim_batch(limit=1, lease_seconds=60) == ()
        assert await rows(pg_engine, "SELECT state, error_class, lease_token FROM evaluation_outbox") == [
            ("quarantined", "retry_exhausted", None)]
        assert await repo.mark_delivered(claim, ACK) is False
        assert await repo.retry_quarantined(EventIdentity(run_id=envelope.run_id)) is True
        [again] = await repo.claim_batch(limit=1, lease_seconds=60)
        assert again.attempts == 1 and again.envelope_bytes == envelope.canonical_bytes()
    asyncio.run(scenario())


def test_permanent_failure_quarantines_immediately_with_safe_class(repo, pg_engine, envelope):
    async def scenario():
        await repo.enqueue(envelope, intent=None)
        [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "AccessDenied: arn:aws:iam::123", permanent=True) is True
        assert await rows(pg_engine, "SELECT state, error_class FROM evaluation_outbox") == [
            ("quarantined", "unclassified")]
    asyncio.run(scenario())


def test_expired_metadata_is_never_claimed_or_revived(repo, envelope, clock):
    async def scenario():
        await repo.enqueue(envelope, intent=None)
        [claim] = await repo.claim_batch(limit=1, lease_seconds=60)
        await repo.schedule_retry(claim, "broker_denied", permanent=True)
        clock.now = envelope.safe_run.completed_at + timedelta(days=90)
        assert await repo.retry_quarantined(EventIdentity(run_id=envelope.run_id)) is False
        assert await repo.claim_batch(limit=1, lease_seconds=60) == ()
    asyncio.run(scenario())


def test_pending_rows_past_metadata_expiry_are_not_claimed(repo, envelope, clock):
    async def scenario():
        await repo.enqueue(envelope, intent=None)
        clock.now = envelope.safe_run.completed_at + timedelta(days=90)
        assert await repo.claim_batch(limit=1, lease_seconds=60) == ()
    asyncio.run(scenario())


def test_expire_runs_removes_quarantined_outbox_and_references(repo, pg_engine, envelope, clock):
    async def scenario():
        kept = make_envelope(envelope)
        for item in (make_envelope(envelope, refs=0), kept):
            await repo.enqueue(item, intent=None)
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        await repo.enqueue(make_envelope(envelope, run_id=run_id, refs=2, created=clock()), intent=intent)
        for claim in await repo.claim_batch(limit=10, lease_seconds=60):
            await repo.schedule_retry(claim, "broker_denied", permanent=True)
        async with pg_engine.begin() as conn:  # make one run younger so it survives
            await conn.execute(text("UPDATE evaluation_runs SET expires_at = expires_at + interval '1 day' "
                                    "WHERE run_id = :r"), {"r": kept.run_id})
        clock.now = envelope.safe_run.completed_at + timedelta(days=90)
        assert await repo.expire_runs(clock(), limit=1) == 1
        assert await repo.expire_runs(clock(), limit=10) == 1
        assert await repo.expire_runs(clock(), limit=10) == 0
        assert await rows(pg_engine, "SELECT run_id FROM evaluation_runs") == [(kept.run_id,)]
        assert await count(pg_engine, "evaluation_outbox") == 1
        assert await count(pg_engine, "evaluation_payload_refs") == 0
    asyncio.run(scenario())


# --- Upload intents and orphan cleanup ------------------------------------------------

def test_expired_intent_cannot_commit(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        stored = make_envelope(envelope, run_id=run_id, refs=1, created=clock())
        clock.advance(seconds=60)
        receipt = await repo.enqueue(stored, intent=intent)
        assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)
        assert await count(pg_engine, "evaluation_runs") == 0
    asyncio.run(scenario())


def test_forged_intent_token_cannot_commit(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        forged = intent.model_copy(update={"lease_token": uuid4()})
        receipt = await repo.enqueue(make_envelope(envelope, run_id=run_id, refs=1, created=clock()), intent=forged)
        assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)
        assert await count(pg_engine, "evaluation_runs") == 0
    asyncio.run(scenario())


def test_live_intent_blocks_orphan_cleanup(repo, pg_engine, clock):
    async def scenario():
        run_id = uuid4()
        namespace = namespace_for(run_id)
        await repo.begin_upload(run_id, namespace, lease_seconds=60)
        assert await repo.inspect_cleanup(namespace, clock()) is None
        assert await repo.claim_orphan(namespace, clock()) is None
        clock.advance(seconds=61)  # expired, but still inside the 24h orphan grace
        assert await repo.claim_orphan(namespace, clock()) is None
        assert await rows(pg_engine, "SELECT state FROM evaluation_upload_intents") == [("open",)]
    asyncio.run(scenario())


def test_cleanup_owned_intent_denies_commit_and_new_uploads(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        namespace = namespace_for(run_id)
        intent = await repo.begin_upload(run_id, namespace, lease_seconds=60)
        stored = make_envelope(envelope, run_id=run_id, refs=1, created=clock())
        clock.advance(seconds=60, hours=24)
        claim = await repo.claim_orphan(namespace, clock())
        assert claim is not None and claim.namespace == namespace and claim.state == "cleanup_owned"
        assert await rows(pg_engine, "SELECT state FROM evaluation_upload_intents") == [("cleanup_owned",)]
        receipt = await repo.enqueue(stored, intent=intent)
        assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)
        assert await count(pg_engine, "evaluation_payload_refs") == 0
        with pytest.raises(EvidenceConflict):
            await repo.begin_upload(run_id, namespace, lease_seconds=60)
        assert await repo.complete_cleanup(claim) is True
    asyncio.run(scenario())


def test_cleanup_ownership_denies_commit_even_before_the_committers_clock_expires_it(repo, envelope, clock):
    async def scenario():
        # A maintenance host whose clock runs ahead claims the namespace; the API host still sees a
        # live intent by its own clock, so only the cleanup-owned state predicate can deny the commit.
        run_id = uuid4()
        namespace = namespace_for(run_id)
        intent = await repo.begin_upload(run_id, namespace, lease_seconds=60)
        assert await repo.claim_orphan(namespace, clock() + timedelta(hours=25)) is not None
        receipt = await repo.enqueue(make_envelope(envelope, run_id=run_id, refs=1, created=clock()), intent=intent)
        assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)
    asyncio.run(scenario())


TENANT_PREFIX = "evidence/test/synthetic-tenant/"


def test_parent_prefix_claim_cannot_cover_live_child_intent_or_reference(repo, pg_engine, envelope, clock):
    async def scenario():
        open_run, committed_run = uuid4(), uuid4()
        await repo.begin_upload(open_run, namespace_for(open_run), lease_seconds=60)
        intent = await repo.begin_upload(committed_run, namespace_for(committed_run), lease_seconds=60)
        assert (await repo.enqueue(make_envelope(envelope, run_id=committed_run, refs=1, created=clock()),
                                   intent=intent)).status == "committed"
        for parent in (TENANT_PREFIX, "evidence/test/", "evidence/"):
            assert await repo.inspect_cleanup(parent, clock()) is None
            assert await repo.claim_orphan(parent, clock()) is None
        # Past the open child's grace, the committed child's live reference still protects the parent.
        clock.advance(days=2)
        assert await repo.claim_orphan(TENANT_PREFIX, clock()) is None
        assert set(await rows(pg_engine, "SELECT namespace, state FROM evaluation_upload_intents")) == {
            (namespace_for(open_run), "open"), (namespace_for(committed_run), "closed")}
    asyncio.run(scenario())


def test_child_prefix_claim_cannot_delete_in_flight_keys_of_parent_intent(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        parent = namespace_for(run_id)
        child = parent + "2026-01-01/"
        intent = await repo.begin_upload(run_id, parent, lease_seconds=60)
        assert await repo.inspect_cleanup(child, clock()) is None
        assert await repo.claim_orphan(child, clock()) is None
        assert await count(pg_engine, "evaluation_upload_intents") == 1
        receipt = await repo.enqueue(make_envelope(envelope, run_id=run_id, refs=1, created=clock()), intent=intent)
        assert receipt.status == "committed"
        clock.advance(days=2)  # parent intent now expired past grace, but its reference is live
        assert await repo.claim_orphan(child, clock()) is None
    asyncio.run(scenario())


def test_parent_claim_takes_ownership_of_expired_child_intents(repo, pg_engine, envelope, clock):
    async def scenario():
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        stored = make_envelope(envelope, run_id=run_id, refs=1, created=clock())
        claim = await repo.claim_orphan(TENANT_PREFIX, clock() + timedelta(hours=25))
        assert claim is not None and claim.namespace == TENANT_PREFIX
        assert set(await rows(pg_engine, "SELECT namespace, state FROM evaluation_upload_intents")) == {
            (TENANT_PREFIX, "cleanup_owned"), (namespace_for(run_id), "cleanup_owned")}
        # The committer's clock still sees its intent as live; only the ownership state denies it.
        receipt = await repo.enqueue(stored, intent=intent)
        assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)
    asyncio.run(scenario())


def test_begin_upload_conflicts_with_any_overlapping_namespace(repo, clock):
    async def scenario():
        run_id = uuid4()
        await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        with pytest.raises(EvidenceConflict):  # ancestor of an existing intent
            await repo.begin_upload(uuid4(), TENANT_PREFIX, lease_seconds=60)
        with pytest.raises(EvidenceConflict):  # descendant of an existing intent
            await repo.begin_upload(run_id, namespace_for(run_id) + "nested/", lease_seconds=60)
        other_tenant = "evidence/test/other-tenant/"
        assert await repo.claim_orphan(other_tenant, clock()) is not None
        with pytest.raises(EvidenceConflict):  # descendant of a cleanup marker
            await repo.begin_upload(run_id, f"{other_tenant}{run_id}/", lease_seconds=60)
        sibling = uuid4()
        assert (await repo.begin_upload(sibling, namespace_for(sibling), lease_seconds=60)).state == "open"
    asyncio.run(scenario())


def test_uploads_resume_under_a_parent_once_its_cleanup_completes(repo, clock):
    async def scenario():
        claim = await repo.claim_orphan(TENANT_PREFIX, clock())
        blocked = uuid4()
        with pytest.raises(EvidenceConflict):  # parent cleanup in progress
            await repo.begin_upload(blocked, namespace_for(blocked), lease_seconds=60)
        clock.now = claim.expires_at
        with pytest.raises(EvidenceConflict):  # lease lapsed but deletion may still be running
            await repo.begin_upload(blocked, namespace_for(blocked), lease_seconds=60)
        resumed = await repo.claim_orphan(TENANT_PREFIX, clock())
        assert await repo.complete_cleanup(resumed) is True
        fresh = uuid4()
        intent = await repo.begin_upload(fresh, namespace_for(fresh), lease_seconds=60)
        assert intent.state == "open"
        # The resumed upload is protected in turn: the parent cannot be re-claimed over it.
        assert await repo.claim_orphan(TENANT_PREFIX, clock()) is None
        with pytest.raises(EvidenceConflict):  # the completed marker's own namespace is never reused
            await repo.begin_upload(uuid4(), TENANT_PREFIX, lease_seconds=60)
    asyncio.run(scenario())


def test_concurrent_parent_claim_and_child_upload_are_exclusive(repo_factory, clock):
    async def scenario():
        for index in range(5):
            run_id = uuid4()
            parent = f"evidence/race-{index}/"
            upload, orphan = await asyncio.gather(
                repo_factory().begin_upload(run_id, f"{parent}{run_id}/", lease_seconds=60),
                repo_factory().claim_orphan(parent, clock()), return_exceptions=True)
            granted_upload = not isinstance(upload, BaseException)
            granted_cleanup = orphan is not None and not isinstance(orphan, BaseException)
            assert granted_upload != granted_cleanup
    asyncio.run(scenario())


def test_live_references_block_cleanup_until_content_expiry(repo, envelope, clock):
    async def scenario():
        run_id = uuid4()
        namespace = namespace_for(run_id)
        intent = await repo.begin_upload(run_id, namespace, lease_seconds=60)
        assert (await repo.enqueue(make_envelope(envelope, run_id=run_id, refs=1, created=clock()),
                                   intent=intent)).status == "committed"
        clock.advance(days=2)
        assert await repo.inspect_cleanup(namespace, clock()) is None
        assert await repo.claim_orphan(namespace, clock()) is None
        clock.advance(days=28)  # reference content expiry reached
        assert await repo.claim_orphan(namespace, clock()) is not None
    asyncio.run(scenario())


def test_absent_intent_gets_exclusion_marker_and_resumable_cleanup(repo, pg_engine, clock):
    async def scenario():
        run_id = uuid4()
        namespace = namespace_for(run_id)
        claim = await repo.claim_orphan(namespace, clock())
        assert claim is not None and claim.expires_at > clock()
        assert await rows(pg_engine, "SELECT namespace, state, run_id FROM evaluation_upload_intents") == [
            (namespace, "cleanup_owned", None)]
        with pytest.raises(EvidenceConflict):
            await repo.begin_upload(run_id, namespace, lease_seconds=60)
        assert await repo.claim_orphan(namespace, clock()) is None  # another cleaner owns the lease
        clock.advance(seconds=claim_lease_seconds(claim, clock))
        resumed = await repo.claim_orphan(namespace, clock())  # restart resumes incomplete cleanup
        assert resumed is not None and resumed.lease_token != claim.lease_token
        assert await repo.complete_cleanup(claim) is False  # stale owner cannot record completion
        assert await repo.complete_cleanup(resumed) is True
        assert await repo.complete_cleanup(resumed) is False
    asyncio.run(scenario())


def test_lapsed_cleanup_lease_cannot_record_completion(repo, clock):
    async def scenario():
        namespace = namespace_for(uuid4())
        claim = await repo.claim_orphan(namespace, clock())
        clock.now = claim.expires_at
        assert await repo.complete_cleanup(claim) is False  # deletion may be incomplete: resume instead
        assert await repo.claim_orphan(namespace, clock()) is not None
    asyncio.run(scenario())


def claim_lease_seconds(claim, clock):
    return (claim.expires_at - clock()).total_seconds()


def test_inspect_cleanup_never_mutates(repo, pg_engine, clock):
    async def scenario():
        absent = namespace_for(uuid4())
        run_id = uuid4()
        expired = namespace_for(run_id)
        await repo.begin_upload(run_id, expired, lease_seconds=60)
        clock.advance(seconds=60, hours=24)
        before = await rows(pg_engine, "SELECT namespace, state, cleanup_token FROM evaluation_upload_intents")
        preview = await repo.inspect_cleanup(absent, clock())
        assert preview is not None and preview.namespace == absent
        assert await repo.complete_cleanup(preview) is False
        assert (await repo.inspect_cleanup(expired, clock())).namespace == expired
        assert await rows(pg_engine, "SELECT namespace, state, cleanup_token FROM evaluation_upload_intents") == before
    asyncio.run(scenario())


def test_concurrent_orphan_claims_grant_single_owner(repo_factory, clock):
    async def scenario():
        namespace = namespace_for(uuid4())
        claims = await asyncio.gather(*(repo_factory().claim_orphan(namespace, clock()) for _ in range(4)))
        assert sum(c is not None for c in claims) == 1
    asyncio.run(scenario())


def test_concurrent_begin_upload_and_orphan_claim_are_exclusive(repo_factory, clock):
    async def scenario():
        for _ in range(5):
            run_id = uuid4()
            namespace = namespace_for(run_id)
            upload, orphan = await asyncio.gather(
                repo_factory().begin_upload(run_id, namespace, lease_seconds=60),
                repo_factory().claim_orphan(namespace, clock()), return_exceptions=True)
            granted_upload = not isinstance(upload, BaseException)
            granted_cleanup = orphan is not None and not isinstance(orphan, BaseException)
            assert granted_upload != granted_cleanup
            if not granted_upload:
                assert isinstance(upload, EvidenceConflict)
    asyncio.run(scenario())


# --- Retention discovery (read-only) --------------------------------------------------

ALL_INTENT_COLUMNS = "SELECT * FROM evaluation_upload_intents ORDER BY namespace"


def test_cleanup_candidates_cover_only_resolvable_namespaces(repo, clock):
    async def scenario():
        runs = {name: uuid4() for name in ("open", "early", "expired", "claimed", "done")}
        ns = {name: namespace_for(run_id) for name, run_id in runs.items()}
        for name in ("expired", "claimed", "done"):
            await repo.begin_upload(runs[name], ns[name], lease_seconds=60)
        clock.advance(seconds=60, hours=24, microseconds=-1)
        await repo.begin_upload(runs["early"], ns["early"], lease_seconds=60)
        assert await repo.cleanup_candidates(clock(), 10) == ()  # one microsecond inside the grace period
        clock.advance(microseconds=1)  # exactly intent expiry + grace
        assert await repo.cleanup_candidates(clock(), 10) == tuple(sorted([ns["expired"], ns["claimed"], ns["done"]]))
        claimed = await repo.claim_orphan(ns["claimed"], clock())
        done = await repo.claim_orphan(ns["done"], clock())
        assert await repo.complete_cleanup(done)
        await repo.begin_upload(runs["open"], ns["open"], lease_seconds=60)
        # A held cleanup lease, a completed cleanup, and open intents are not candidates.
        assert await repo.cleanup_candidates(clock(), 10) == (ns["expired"],)
        clock.now = claimed.expires_at  # a lapsed, incomplete cleanup is resumable
        assert ns["claimed"] in await repo.cleanup_candidates(clock(), 10)
    asyncio.run(scenario())


def test_cleanup_candidates_skip_namespaces_with_live_references(repo, envelope, clock):
    async def scenario():
        run_id = uuid4()
        intent = await repo.begin_upload(run_id, namespace_for(run_id), lease_seconds=60)
        stored = make_envelope(envelope, run_id=run_id, refs=2, created=clock())
        assert (await repo.enqueue(stored, intent=intent)).status == "committed"
        expiry = stored.manifest[0].reference.expires_at
        clock.now = expiry - timedelta(microseconds=1)
        assert await repo.cleanup_candidates(clock(), 10) == ()
        clock.now = expiry
        assert await repo.cleanup_candidates(clock(), 10) == (intent.namespace,)
    asyncio.run(scenario())


def test_cleanup_candidates_include_swept_child_markers(repo, pg_engine, clock):
    async def scenario():
        run_id = uuid4()
        child = namespace_for(run_id)
        await repo.begin_upload(run_id, child, lease_seconds=60)
        clock.advance(seconds=60, hours=24)
        parent = await repo.claim_orphan(TENANT_PREFIX, clock())
        assert await repo.complete_cleanup(parent)
        # The parent sweep left the child cleanup-owned without token or completion stamp.
        assert await rows(pg_engine, "SELECT state, cleanup_token, cleanup_completed_at FROM "
                                     "evaluation_upload_intents WHERE namespace = :ns", ns=child) == [
            ("cleanup_owned", None, None)]
        assert await repo.cleanup_candidates(clock(), 10) == (child,)
        claim = await repo.claim_orphan(child, clock())
        assert claim is not None and await repo.complete_cleanup(claim)
        assert await repo.cleanup_candidates(clock(), 10) == ()
    asyncio.run(scenario())


def test_cleanup_candidates_paginate_by_keyset_without_mutation(repo, pg_engine, clock):
    async def scenario():
        namespaces = sorted(namespace_for(uuid4()) for _ in range(5))
        for namespace in namespaces:
            await repo.begin_upload(uuid4(), namespace, lease_seconds=60)
        clock.advance(seconds=60, hours=24)
        before = await rows(pg_engine, ALL_INTENT_COLUMNS)
        first = await repo.cleanup_candidates(clock(), 2)
        second = await repo.cleanup_candidates(clock(), 2, after=first[-1])
        third = await repo.cleanup_candidates(clock(), 2, after=second[-1])
        assert first + second + third == tuple(namespaces) and len(third) == 1
        assert await rows(pg_engine, ALL_INTENT_COLUMNS) == before
        with pytest.raises(ValueError):
            await repo.cleanup_candidates(clock(), 0)
    asyncio.run(scenario())


def test_count_expired_runs_is_bounded_read_only_and_exact_at_expiry(repo, pg_engine, envelope, clock):
    async def scenario():
        for _ in range(3):
            assert (await repo.enqueue(make_envelope(envelope), intent=None)).status == "committed"
        expiry = envelope.safe_run.completed_at + timedelta(days=90)
        assert await repo.count_expired_runs(expiry - timedelta(microseconds=1), 10) == 0
        assert await repo.count_expired_runs(expiry, 10) == 3
        assert await repo.count_expired_runs(expiry, 2) == 2
        assert await count(pg_engine, "evaluation_runs") == 3
        assert await repo.expire_runs(expiry, 10) == 3
        with pytest.raises(ValueError):
            await repo.count_expired_runs(expiry, 0)
    asyncio.run(scenario())


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
