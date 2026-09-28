"""Transactional evidence outbox, upload intents, and orphan-cleanup coordination.

Every operation is one short PostgreSQL transaction; no lock is held across S3/SQS IO.
Lease and expiry comparisons use the injected application clock (bound ``:now``), not the database
clock, so retention tests can control time; hosts sharing this database need NTP-level agreement. Database
failures surface only as fixed-message ``EvidencePersistenceError`` subclasses so statement text
and bound parameters (which include envelope bytes) never reach logs or callers.
"""
from __future__ import annotations

import hashlib
import random
import re
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import bindparam, insert, text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from .delivery_models import (
    CleanupClaim, DeliveryReason, DeliveryReceipt, EventIdentity, ExportEnvelope, OutboxClaim, PublishReceipt,
    UploadIntent,
)
from .storage_models import EvidencePayloadRef, EvidenceRun, EvidenceUploadIntent

try:  # asyncpg may raise its own errors (e.g. on connect) without SQLAlchemy wrapping them
    import asyncpg
    _DB_ERRORS: tuple[type[BaseException], ...] = (
        SQLAlchemyError, OSError, TimeoutError, asyncpg.PostgresError, asyncpg.InterfaceError)
except ImportError:  # pragma: no cover - asyncpg is a pinned API dependency
    _DB_ERRORS = (SQLAlchemyError, OSError, TimeoutError)

_SAFE_ERROR_CLASS = re.compile(r"^[A-Za-z][A-Za-z0-9_.]{0,63}$")


class EvidencePersistenceError(RuntimeError):
    """Base for safe persistence failures: fixed text, no chained driver exception."""
    message = "evidence persistence failed"

    def __init__(self) -> None:
        super().__init__(self.message)


class EvidenceUnavailable(EvidencePersistenceError):
    message = "evidence database unavailable"


class EvidenceInvalid(EvidencePersistenceError):
    message = "evidence rejected by database constraints"


class EvidenceConflict(EvidencePersistenceError):
    message = "evidence namespace or identity conflict"


class _Abort(Exception):
    def __init__(self, reason: DeliveryReason) -> None:
        self.reason = reason


def safe_error_class(value: str) -> str:
    return value if isinstance(value, str) and _SAFE_ERROR_CLASS.fullmatch(value) else "unclassified"


def retry_delay(attempts: int, *, base: float, cap: float, jitter: Callable[[], float] = random.random) -> float:
    """Capped exponential delay with equal jitter: within [d/2, d] for d = min(cap, base * 2**(n-1))."""
    delay = min(cap, base * 2 ** max(attempts - 1, 0))
    return delay / 2 + delay / 2 * jitter()


_CLAIM_SQL = text("""
SELECT o.id, o.attempts FROM evaluation_outbox AS o
JOIN evaluation_runs AS r ON r.id = o.run_pk
WHERE o.state IN ('pending', 'leased') AND o.next_attempt_at <= :now
  AND (o.lease_until IS NULL OR o.lease_until <= :now)
  AND r.expires_at > :now
ORDER BY o.next_attempt_at, o.id
FOR UPDATE OF o SKIP LOCKED
LIMIT :limit
""")

_LEASE_SQL = text("""
UPDATE evaluation_outbox AS o
SET state = 'leased', lease_token = gen_random_uuid(), lease_until = :lease_until,
    attempts = o.attempts + 1, updated_at = :now
FROM evaluation_runs AS r
WHERE r.id = o.run_pk AND o.id IN :ids
RETURNING r.export_version, r.run_schema_version, r.run_id, r.export_hash, r.envelope, r.expires_at,
          o.lease_token, o.lease_until, o.attempts
""").bindparams(bindparam("ids", expanding=True))

# A lapsed lease with no recorded outcome (e.g. the dispatcher was killed mid-send) still spent an
# attempt; enforcing the cap here keeps the attempt bound even when schedule_retry never runs.
_EXHAUST_SQL = text("""
UPDATE evaluation_outbox
SET state = 'quarantined', error_class = 'retry_exhausted', lease_token = NULL, lease_until = NULL,
    updated_at = :now
WHERE id IN :ids
""").bindparams(bindparam("ids", expanding=True))

# Token AND unexpired lease: a stale or lapsed claimant can never overwrite a newer claim.
_OWNED_CLAIM = """
  o.run_pk = r.id AND r.export_version = :export_version AND r.run_schema_version = :run_schema_version
  AND r.run_id = :run_id AND r.export_hash = :export_hash
  AND o.state = 'leased' AND o.lease_token = :token AND o.lease_until > :now
"""

_ACK_SQL = text(f"""
UPDATE evaluation_outbox AS o
SET state = 'delivered', acknowledgement_id = :ack, delivered_at = :now, lease_token = NULL,
    lease_until = NULL, error_class = NULL, updated_at = :now
FROM evaluation_runs AS r
WHERE {_OWNED_CLAIM}
""")

_LOCK_CLAIM_SQL = text(f"""
SELECT o.id, o.attempts FROM evaluation_outbox AS o, evaluation_runs AS r
WHERE {_OWNED_CLAIM}
FOR UPDATE OF o
""")

_RETRY_SQL = text("""
UPDATE evaluation_outbox
SET state = :state, next_attempt_at = :next_attempt_at, error_class = :error_class,
    lease_token = NULL, lease_until = NULL, updated_at = :now
WHERE id = :id
""")

_REQUEUE_SQL = text("""
UPDATE evaluation_outbox AS o
SET state = 'pending', attempts = 0, next_attempt_at = :now, error_class = NULL, updated_at = :now
FROM evaluation_runs AS r
WHERE o.run_pk = r.id AND r.export_version = :export_version AND r.run_schema_version = :run_schema_version
  AND r.run_id = :run_id AND o.state = 'quarantined' AND r.expires_at > :now
""")

_CLOSE_INTENT_SQL = text("""
UPDATE evaluation_upload_intents
SET state = 'closed', closed_at = :now
WHERE namespace = :namespace AND run_id = :run_id AND lease_token = :token
  AND state = 'open' AND expires_at > :now
""")

_EXISTING_HASH_SQL = text("""
SELECT export_hash FROM evaluation_runs
WHERE export_version = :export_version AND run_schema_version = :run_schema_version AND run_id = :run_id
""")

# Namespaces are hierarchical key prefixes ending in "/". Coordination treats every ancestor and
# descendant namespace as overlapping: a claim on "a/" must not cover live work under "a/b/", and a
# claim on "a/b/" must not delete in-flight keys of an open intent for "a/". Upload/cleanup row
# creation is serialized by one advisory lock because overlapping namespaces are different rows.
# This relies on READ COMMITTED (the PostgreSQL default): each statement after the lock sees rows
# committed by the previous lock holder. One global key is coarse; revisit if load tests show contention.
_COORDINATION_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"scalable-rag:evaluation-namespace-coordination").digest()[:8], "big", signed=True)
_COORDINATION_LOCK_SQL = text("SELECT pg_advisory_xact_lock(:key)")

_OVERLAP_WHERE = "(namespace IN :ancestors OR namespace LIKE :descendants)"
_OVERLAP_INTENTS = ("SELECT namespace, state, expires_at, cleanup_token, cleanup_lease_until "
                    f"FROM evaluation_upload_intents WHERE {_OVERLAP_WHERE}")
_OVERLAP_INTENTS_SQL = text(_OVERLAP_INTENTS).bindparams(bindparam("ancestors", expanding=True))

# New uploads conflict with the same or any descendant namespace in any state, and with ancestors
# unless the ancestor is a cleanup marker whose deletion has completed. A lapsed-but-incomplete
# cleanup still blocks: its cleaner may still be deleting under the prefix.
_UPLOAD_CONFLICT_SQL = text("""
SELECT 1 FROM evaluation_upload_intents
WHERE namespace LIKE :descendants
   OR (namespace IN :ancestors AND NOT (state = 'cleanup_owned' AND cleanup_token IS NULL
                                        AND cleanup_completed_at IS NOT NULL))
LIMIT 1
""").bindparams(bindparam("ancestors", expanding=True))
_OVERLAP_INTENTS_FOR_UPDATE_SQL = text(_OVERLAP_INTENTS + " ORDER BY namespace FOR UPDATE").bindparams(
    bindparam("ancestors", expanding=True))

# Any unexpired reference to an object at or below the namespace, whichever intent recorded it.
_LIVE_REF_SQL = text("""
SELECT 1 FROM evaluation_payload_refs WHERE object_key LIKE :descendants AND expires_at > :now LIMIT 1
""")

_EXCLUDE_OVERLAPS_SQL = text(f"""
UPDATE evaluation_upload_intents SET state = 'cleanup_owned'
WHERE {_OVERLAP_WHERE} AND namespace <> :namespace AND state <> 'cleanup_owned'
""").bindparams(bindparam("ancestors", expanding=True))

_TAKE_CLEANUP_SQL = text("""
UPDATE evaluation_upload_intents
SET state = 'cleanup_owned', cleanup_token = :token, cleanup_lease_until = :lease_until,
    cleanup_completed_at = NULL
WHERE namespace = :namespace
""")

_COMPLETE_CLEANUP_SQL = text("""
UPDATE evaluation_upload_intents
SET cleanup_token = NULL, cleanup_lease_until = NULL, cleanup_completed_at = :now
WHERE namespace = :namespace AND state = 'cleanup_owned' AND cleanup_token = :token
  AND cleanup_lease_until > :now
""")

# Discovery only: claim_orphan re-checks every condition under the coordination lock. Rows swept to
# cleanup_owned by a parent claim carry no token or completion stamp, so they stay candidates until a
# claim of their own records completion. Namespaces with a live reference of their own are skipped
# here so they cannot occupy every page until their content expires.
_CLEANUP_CANDIDATES_SQL = text("""
SELECT i.namespace FROM evaluation_upload_intents AS i
WHERE i.cleanup_completed_at IS NULL AND i.namespace > :after
  AND ((i.state IN ('open', 'closed') AND i.expires_at <= :grace_cutoff)
       OR (i.state = 'cleanup_owned' AND (i.cleanup_token IS NULL OR i.cleanup_lease_until <= :now)))
  AND NOT EXISTS (SELECT 1 FROM evaluation_payload_refs AS p
                  WHERE p.namespace = i.namespace AND p.expires_at > :now)
ORDER BY i.namespace
LIMIT :limit
""")

_COUNT_EXPIRED_SQL = text("""
SELECT count(*) FROM (SELECT 1 FROM evaluation_runs WHERE expires_at <= :now LIMIT :limit) AS due
""")

_EXPIRE_SQL = text("""
DELETE FROM evaluation_runs WHERE id IN (
  SELECT id FROM evaluation_runs WHERE expires_at <= :now
  ORDER BY expires_at, id
  FOR UPDATE SKIP LOCKED
  LIMIT :limit)
""")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("evidence clock must return UTC-aware timestamps")
    return value


def _namespace_params(namespace: str) -> dict:
    """Validate a namespace and build its ancestor list and escaped descendant LIKE pattern."""
    if (not isinstance(namespace, str) or not namespace.endswith("/") or namespace.startswith("/")
            or "//" in namespace or len(namespace) > 1024):
        raise ValueError("namespace must be a relative key prefix ending in '/'")
    parts = namespace.split("/")[:-1]
    escaped = namespace.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return {"namespace": namespace, "ancestors": ["/".join(parts[:i]) + "/" for i in range(1, len(parts) + 1)],
            "descendants": escaped + "%"}


def _identity_params(identity: EventIdentity) -> dict:
    return {"export_version": identity.export_version, "run_schema_version": identity.run_schema_version,
            "run_id": identity.run_id}


class OutboxRepository:
    def __init__(self, session_factory: Callable, clock: Callable[[], datetime], *,
                 max_attempts: int = 8, backoff_base_seconds: float = 1.0, backoff_cap_seconds: float = 60.0,
                 metadata_retention_days: int = 90, content_retention_days: int = 30,
                 orphan_grace_seconds: float = 86400,
                 cleanup_lease_seconds: float = 300, jitter: Callable[[], float] = random.random) -> None:
        if min(max_attempts, backoff_base_seconds, backoff_cap_seconds, metadata_retention_days,
               content_retention_days, orphan_grace_seconds, cleanup_lease_seconds) <= 0:
            raise ValueError("outbox limits must be positive")
        self._sessions = session_factory
        self._clock = clock
        self._max_attempts = max_attempts
        self._backoff_base = backoff_base_seconds
        self._backoff_cap = backoff_cap_seconds
        self._retention = timedelta(days=metadata_retention_days)
        self._content_retention = timedelta(days=content_retention_days)
        self._grace = timedelta(seconds=orphan_grace_seconds)
        self._cleanup_lease = timedelta(seconds=cleanup_lease_seconds)
        self._jitter = jitter

    def _now(self) -> datetime:
        return _utc(self._clock())

    @asynccontextmanager
    async def _transaction(self):
        # Translate outside the except block so no driver exception (with its bound
        # parameters) stays reachable through __context__ either.
        failure: type[EvidencePersistenceError] | None = None
        try:
            async with self._sessions() as session, session.begin():
                yield session
        except IntegrityError:
            failure = EvidenceInvalid
        except _DB_ERRORS:
            failure = EvidenceUnavailable
        if failure is not None:
            raise failure()

    # --- upload intents -----------------------------------------------------------

    async def begin_upload(self, run_id: UUID, namespace: str, lease_seconds: float) -> UploadIntent:
        params = _namespace_params(namespace)
        if lease_seconds <= 0:
            raise ValueError("upload intent requires a positive lease")
        now = self._now()
        intent = UploadIntent(run_id=run_id, namespace=namespace, lease_token=uuid4(),
                              expires_at=now + timedelta(seconds=lease_seconds))
        statement = pg_insert(EvidenceUploadIntent).values(
            namespace=namespace, run_id=intent.run_id, state="open", lease_token=intent.lease_token,
            expires_at=intent.expires_at, created_at=now,
        ).on_conflict_do_nothing(index_elements=["namespace"]).returning(EvidenceUploadIntent.namespace)
        async with self._transaction() as session:
            await session.execute(_COORDINATION_LOCK_SQL, {"key": _COORDINATION_LOCK_KEY})
            overlapping = (await session.execute(_UPLOAD_CONFLICT_SQL, params)).first()
            inserted = None if overlapping else (await session.execute(statement)).first()
        if inserted is None:
            raise EvidenceConflict()
        return intent

    # --- commit ---------------------------------------------------------------------

    async def enqueue(self, envelope: ExportEnvelope, intent: UploadIntent | None) -> DeliveryReceipt:
        now = self._now()
        identity = EventIdentity(export_version=envelope.export_version,
                                 run_schema_version=envelope.run_schema_version, run_id=envelope.run_id)

        def failed(reason: DeliveryReason) -> DeliveryReceipt:
            return DeliveryReceipt(status="failed", reason=reason, identity=identity,
                                   export_hash=envelope.export_hash)

        expires_at = envelope.safe_run.completed_at + self._retention
        if expires_at <= now:
            return failed(DeliveryReason.expired)
        refs = [entry.reference for entry in envelope.manifest if entry.state == "stored"]
        if intent is not None:
            if intent.run_id != envelope.run_id or intent.state != "open" or intent.expires_at <= now:
                return failed(DeliveryReason.intent_failed)
            if any(not ref.key.startswith(intent.namespace) for ref in refs):
                return failed(DeliveryReason.metadata_invalid)
            if any(ref.expires_at > ref.created_at + self._content_retention for ref in refs):
                return failed(DeliveryReason.metadata_invalid)
        elif refs:
            return failed(DeliveryReason.intent_failed)

        run_insert = pg_insert(EvidenceRun).values(
            export_version=identity.export_version, run_schema_version=identity.run_schema_version,
            run_id=identity.run_id, export_hash=envelope.export_hash, envelope=envelope.canonical_bytes(),
            redaction_policy_version=envelope.redaction_policy_version, completeness=envelope.completeness,
            completed_at=envelope.safe_run.completed_at, created_at=now, expires_at=expires_at,
        ).on_conflict_do_nothing(constraint="evaluation_runs_identity_key").returning(EvidenceRun.id)
        try:
            async with self._transaction() as session:
                run_pk = (await session.execute(run_insert)).scalar()
                if run_pk is None:
                    # Duplicate: the first commit wins; expiry and bytes are never rewritten. A fresh
                    # intent passed with a duplicate stays open on purpose: its objects are unreferenced
                    # and orphan cleanup removes them once the intent expires plus the grace period.
                    existing = (await session.execute(_EXISTING_HASH_SQL, _identity_params(identity))).scalar()
                    if existing != envelope.export_hash:
                        raise _Abort(DeliveryReason.identity_conflict)
                else:
                    if intent is not None:
                        closed = await session.execute(_CLOSE_INTENT_SQL, {
                            "namespace": intent.namespace, "run_id": intent.run_id,
                            "token": intent.lease_token, "now": now})
                        if closed.rowcount != 1:
                            raise _Abort(DeliveryReason.intent_failed)
                    if refs:
                        await session.execute(insert(EvidencePayloadRef), [{
                            "run_pk": run_pk, "namespace": intent.namespace, "kind": ref.slot.kind.value,
                            "ordinal": ref.slot.ordinal, "item_ordinal": ref.slot.item_ordinal,
                            "bucket": ref.bucket, "object_key": ref.key, "version_id": ref.version_id,
                            "tenant_partition": ref.tenant_partition, "byte_size": ref.byte_size,
                            "sha256": ref.sha256, "policy_version": ref.policy_version,
                            "created_at": ref.created_at, "expires_at": ref.expires_at,
                        } for ref in refs])
                    await session.execute(text(
                        "INSERT INTO evaluation_outbox (run_pk, state, attempts, next_attempt_at, created_at, "
                        "updated_at) VALUES (:run_pk, 'pending', 0, :now, :now, :now)"),
                        {"run_pk": run_pk, "now": now})
        except _Abort as abort:
            return failed(abort.reason)
        except EvidenceInvalid:
            return failed(DeliveryReason.metadata_invalid)
        except EvidenceUnavailable:
            return failed(DeliveryReason.database_unavailable)
        return DeliveryReceipt(status="committed", identity=identity, export_hash=envelope.export_hash)

    # --- delivery leases --------------------------------------------------------------

    async def claim_batch(self, limit: int, lease_seconds: float) -> tuple[OutboxClaim, ...]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("claim limit and lease must be positive")
        now = self._now()
        async with self._transaction() as session:
            selected = (await session.execute(_CLAIM_SQL, {"now": now, "limit": limit})).all()
            exhausted = [row.id for row in selected if row.attempts >= self._max_attempts]
            ids = [row.id for row in selected if row.attempts < self._max_attempts]
            if exhausted:
                await session.execute(_EXHAUST_SQL, {"ids": exhausted, "now": now})
            if not ids:
                return ()
            leased = (await session.execute(_LEASE_SQL, {
                "ids": ids, "now": now, "lease_until": now + timedelta(seconds=lease_seconds)})).all()
        return tuple(OutboxClaim(
            identity=EventIdentity(export_version=row.export_version, run_schema_version=row.run_schema_version,
                                   run_id=row.run_id),
            export_hash=row.export_hash, envelope_bytes=bytes(row.envelope), expires_at=row.expires_at,
            lease_token=row.lease_token, lease_expires_at=row.lease_until, attempts=row.attempts,
        ) for row in leased)

    def _claim_params(self, claim: OutboxClaim, now: datetime) -> dict:
        return {**_identity_params(claim.identity), "export_hash": claim.export_hash,
                "token": claim.lease_token, "now": now}

    async def mark_delivered(self, claim: OutboxClaim, receipt: PublishReceipt) -> bool:
        now = self._now()
        async with self._transaction() as session:
            result = await session.execute(_ACK_SQL, {**self._claim_params(claim, now),
                                                      "ack": receipt.acknowledgement_id})
        return result.rowcount == 1

    async def schedule_retry(self, claim: OutboxClaim, error_class: str, *, permanent: bool) -> bool:
        now = self._now()
        error_class = safe_error_class(error_class)
        async with self._transaction() as session:
            row = (await session.execute(_LOCK_CLAIM_SQL, self._claim_params(claim, now))).first()
            if row is None:
                return False
            if permanent or row.attempts >= self._max_attempts:
                values = {"state": "quarantined", "next_attempt_at": now,
                          "error_class": error_class if permanent else DeliveryReason.retry_exhausted.value}
            else:
                delay = retry_delay(row.attempts, base=self._backoff_base, cap=self._backoff_cap,
                                    jitter=self._jitter)
                values = {"state": "pending", "next_attempt_at": now + timedelta(seconds=delay),
                          "error_class": error_class}
            await session.execute(_RETRY_SQL, {**values, "id": row.id, "now": now})
        return True

    async def retry_quarantined(self, identity: EventIdentity) -> bool:
        now = self._now()
        async with self._transaction() as session:
            result = await session.execute(_REQUEUE_SQL, {**_identity_params(identity), "now": now})
        return result.rowcount == 1

    # --- orphan cleanup coordination ------------------------------------------------

    def _cleanup_allowed(self, row, now: datetime) -> bool:
        if row.state == "cleanup_owned":  # resumable once no other cleaner holds a live lease
            return row.cleanup_token is None or row.cleanup_lease_until <= now
        # open/closed: every upload under this intent happened before its expiry; add the grace.
        return row.expires_at + self._grace <= now

    async def inspect_cleanup(self, namespace: str, now: datetime) -> CleanupClaim | None:
        """Read-only eligibility preview; its token is never persisted and grants nothing."""
        now = _utc(now)
        params = _namespace_params(namespace)
        async with self._transaction() as session:
            if (await session.execute(_LIVE_REF_SQL, {**params, "now": now})).first():
                return None
            rows = (await session.execute(_OVERLAP_INTENTS_SQL, params)).all()
        if not all(self._cleanup_allowed(row, now) for row in rows):
            return None
        return CleanupClaim(namespace=namespace, lease_token=uuid4(), expires_at=now)

    async def claim_orphan(self, namespace: str, now: datetime) -> CleanupClaim | None:
        """Commit cleanup ownership (an exclusion marker) before any remote deletion.

        Every overlapping intent must be eligible, and no unexpired reference may exist at or below
        the namespace. Overlapping intents are marked cleanup-owned too, so none of them can commit.
        Database failures raise ``EvidenceUnavailable``: no claim, no cleanup permission.
        """
        now = _utc(now)
        params = _namespace_params(namespace)
        claim = CleanupClaim(namespace=namespace, lease_token=uuid4(), expires_at=now + self._cleanup_lease)
        async with self._transaction() as session:
            await session.execute(_COORDINATION_LOCK_SQL, {"key": _COORDINATION_LOCK_KEY})
            # Row locks make a concurrent commit (which holds its intent row) either fully visible to
            # the reference check below or unable to close its intent afterwards.
            rows = (await session.execute(_OVERLAP_INTENTS_FOR_UPDATE_SQL, params)).all()
            if (await session.execute(_LIVE_REF_SQL, {**params, "now": now})).first():
                return None
            if not all(self._cleanup_allowed(row, now) for row in rows):
                return None
            if any(row.namespace == namespace for row in rows):
                await session.execute(_TAKE_CLEANUP_SQL, {"namespace": namespace, "token": claim.lease_token,
                                                          "lease_until": claim.expires_at})
            else:
                await session.execute(pg_insert(EvidenceUploadIntent).values(
                    namespace=namespace, run_id=None, state="cleanup_owned", created_at=now,
                    cleanup_token=claim.lease_token, cleanup_lease_until=claim.expires_at))
            await session.execute(_EXCLUDE_OVERLAPS_SQL, params)
        return claim

    async def cleanup_candidates(self, now: datetime, limit: int, *, after: str | None = None) -> tuple[str, ...]:
        """Read-only, keyset-paginated namespaces whose cleanup may be claimable; grants nothing."""
        if limit <= 0:
            raise ValueError("candidate limit must be positive")
        now = _utc(now)
        async with self._transaction() as session:
            result = await session.execute(_CLEANUP_CANDIDATES_SQL, {
                "now": now, "grace_cutoff": now - self._grace, "after": after or "", "limit": limit})
            return tuple(row.namespace for row in result.all())

    async def complete_cleanup(self, claim: CleanupClaim) -> bool:
        now = self._now()
        async with self._transaction() as session:
            result = await session.execute(_COMPLETE_CLEANUP_SQL, {
                "namespace": claim.namespace, "token": claim.lease_token, "now": now})
        return result.rowcount == 1

    # --- retention --------------------------------------------------------------------

    async def count_expired_runs(self, now: datetime, limit: int) -> int:
        """Read-only dry-run counterpart of ``expire_runs``: how many it would delete, at most ``limit``."""
        if limit <= 0:
            raise ValueError("expiry limit must be positive")
        async with self._transaction() as session:
            return (await session.execute(_COUNT_EXPIRED_SQL, {"now": _utc(now), "limit": limit})).scalar()

    async def expire_runs(self, now: datetime, limit: int) -> int:
        """Delete runs past metadata expiry; outbox rows and references cascade, even if quarantined."""
        if limit <= 0:
            raise ValueError("expiry limit must be positive")
        async with self._transaction() as session:
            result = await session.execute(_EXPIRE_SQL, {"now": _utc(now), "limit": limit})
        return result.rowcount
