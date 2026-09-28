"""Offline outbox contracts: validation before IO, fail-closed errors, retry arithmetic."""
import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from services.api.app.evaluation.delivery_models import (
    DeliveryReason, EventIdentity, ExportEnvelope, ManifestEntry, OutboxClaim, PayloadKind, PayloadRef, PayloadSlot, UploadIntent,
)
from services.api.app.evaluation.outbox import (
    EvidenceConflict, EvidencePersistenceError, EvidenceUnavailable, OutboxRepository, retry_delay, safe_error_class,
)

SENTINEL = b"SENTINEL-PAYLOAD-BYTES"
NAMESPACE = "evidence/test/synthetic-tenant/{run_id}/"


def untouched_sessions():
    def factory():
        raise AssertionError("validation must reject before any database IO")
    return factory


def leaky_sessions():
    def factory():
        raise OperationalError("INSERT INTO evaluation_runs", {"envelope": SENTINEL}, Exception("boom " + SENTINEL.decode()))
    return factory


def unreachable_repo(clock):
    # A real driver connection attempt to a closed loopback port: no mocks.
    engine = create_async_engine("postgresql+asyncpg://u:p@127.0.0.1:1/none", poolclass=NullPool,
                                 connect_args={"timeout": 2})
    return OutboxRepository(async_sessionmaker(engine), clock)


def stored_envelope(envelope, namespace, *, created):
    slot = PayloadSlot(kind=PayloadKind.input, ordinal=1)
    ref = PayloadRef(bucket="synthetic-evidence-payloads", key=f"{namespace}2026-01-01/input/1", version_id="v1",
                     tenant_partition="synthetic-tenant", slot=slot, byte_size=10, sha256="a" * 64,
                     policy_version=envelope.redaction_policy_version, created_at=created,
                     expires_at=created + timedelta(days=30))
    return ExportEnvelope(
        run_id=envelope.run_id, safe_run=envelope.safe_run, redaction_policy_version=envelope.redaction_policy_version,
        manifest=(ManifestEntry(slot=slot, state="stored", reference=ref),), completeness="complete")


def intent_for(envelope, clock, **overrides):
    values = dict(run_id=envelope.run_id, namespace=NAMESPACE.format(run_id=envelope.run_id),
                  lease_token=uuid4(), expires_at=clock() + timedelta(seconds=60))
    values.update(overrides)
    return UploadIntent(**values)


def test_stored_references_require_an_upload_intent(envelope, clock):
    repo = OutboxRepository(untouched_sessions(), clock)
    stored = stored_envelope(envelope, NAMESPACE.format(run_id=envelope.run_id), created=clock())
    receipt = asyncio.run(repo.enqueue(stored, intent=None))
    assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)


@pytest.mark.parametrize("problem", ["other_run", "closed", "cleanup_owned", "expired"])
def test_unusable_intents_are_rejected_before_io(envelope, clock, problem):
    repo = OutboxRepository(untouched_sessions(), clock)
    overrides = {
        "other_run": {"run_id": uuid4()},
        "closed": {"state": "closed"},
        "cleanup_owned": {"state": "cleanup_owned"},
        "expired": {"expires_at": clock()},
    }[problem]
    intent = intent_for(envelope, clock, **overrides)
    receipt = asyncio.run(repo.enqueue(stored_envelope(envelope, intent.namespace, created=clock()), intent=intent))
    assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.intent_failed)


def test_references_outside_the_intent_namespace_are_rejected(envelope, clock):
    repo = OutboxRepository(untouched_sessions(), clock)
    intent = intent_for(envelope, clock)
    foreign = stored_envelope(envelope, "evidence/test/other-tenant/other-run/", created=clock())
    receipt = asyncio.run(repo.enqueue(foreign, intent=intent))
    assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.metadata_invalid)


def test_already_expired_metadata_is_never_committed(envelope, clock):
    clock.now = envelope.safe_run.completed_at + timedelta(days=90)
    repo = OutboxRepository(untouched_sessions(), clock)
    receipt = asyncio.run(repo.enqueue(envelope, intent=None))
    assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.expired)


def test_naive_clock_is_rejected(envelope):
    repo = OutboxRepository(untouched_sessions(), lambda: datetime(2026, 1, 1))
    with pytest.raises(ValueError):
        asyncio.run(repo.enqueue(envelope, intent=None))


def test_persistence_errors_never_carry_statement_parameters(envelope, clock):
    repo = OutboxRepository(leaky_sessions(), clock)
    receipt = asyncio.run(repo.enqueue(envelope, intent=None))
    assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.database_unavailable)
    with pytest.raises(EvidenceUnavailable) as caught:
        asyncio.run(repo.claim_batch(limit=10, lease_seconds=60))
    error = caught.value
    assert isinstance(error, EvidencePersistenceError)
    assert SENTINEL.decode() not in str(error) and SENTINEL.decode() not in repr(error)
    assert error.__cause__ is None and error.__context__ is None


def test_unreachable_database_grants_no_cleanup_permission(envelope, clock):
    repo = unreachable_repo(clock)
    namespace = NAMESPACE.format(run_id=envelope.run_id)
    for operation in (lambda: repo.claim_orphan(namespace, clock()), lambda: repo.inspect_cleanup(namespace, clock()),
                      lambda: repo.begin_upload(envelope.run_id, namespace, lease_seconds=60),
                      lambda: repo.claim_batch(limit=1, lease_seconds=60)):
        with pytest.raises(EvidenceUnavailable):
            asyncio.run(operation())
    receipt = asyncio.run(repo.enqueue(envelope, intent=None))
    assert (receipt.status, receipt.reason) == ("failed", DeliveryReason.database_unavailable)


def test_unreachable_database_rejects_acknowledgement(envelope, clock):
    repo = unreachable_repo(clock)
    claim = OutboxClaim(identity=EventIdentity(run_id=envelope.run_id), export_hash=envelope.export_hash,
                        envelope_bytes=envelope.canonical_bytes(), expires_at=clock() + timedelta(days=90),
                        lease_token=uuid4(), lease_expires_at=clock() + timedelta(seconds=60), attempts=1)
    with pytest.raises(EvidenceUnavailable):
        asyncio.run(repo.schedule_retry(claim, "broker_transient", permanent=False))


def test_conflict_is_a_persistence_error():
    assert issubclass(EvidenceConflict, EvidencePersistenceError)
    assert issubclass(EvidenceUnavailable, EvidencePersistenceError)


def test_retry_delay_is_capped_jittered_exponential():
    assert retry_delay(1, base=1, cap=60, jitter=lambda: 1.0) == 1
    assert retry_delay(3, base=1, cap=60, jitter=lambda: 1.0) == 4
    assert retry_delay(20, base=1, cap=60, jitter=lambda: 1.0) == 60
    assert retry_delay(3, base=1, cap=60, jitter=lambda: 0.0) == 2
    for attempts in range(1, 12):
        assert 0 < retry_delay(attempts, base=1, cap=60) <= 60


@pytest.mark.parametrize("value, expected", [
    ("broker_transient", "broker_transient"), ("ClientError", "ClientError"),
    ("Error: user@example.com leaked", "unclassified"), ("x" * 200, "unclassified"), ("", "unclassified"),
])
def test_error_classes_are_restricted_to_safe_identifiers(value, expected):
    assert safe_error_class(value) == expected
