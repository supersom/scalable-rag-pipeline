"""Test-only transactional deduplicating consumer; not a grading or scoring consumer.

State lives in its own table on whatever engine/schema the test supplies, never in producer tables,
so deduplication is proven by consumer transactions rather than by producer delivery status.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import Column, MetaData, PrimaryKeyConstraint, String, Table, insert, select
from sqlalchemy.exc import IntegrityError

from services.api.app.evaluation.publisher import PublishError, parse_envelope

RETENTION = timedelta(days=90)

_metadata = MetaData()
_receipts = Table(
    "evaluation_consumer_fixture_receipts", _metadata,
    Column("export_version", String(8), nullable=False),
    Column("run_schema_version", String(8), nullable=False),
    Column("run_id", String(36), nullable=False),
    Column("export_hash", String(64), nullable=False),
    PrimaryKeyConstraint("export_version", "run_schema_version", "run_id"),
)


async def _ensure(engine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_metadata.create_all)


async def accept(engine, envelope_bytes: bytes, *, now: datetime | None = None) -> str:
    """Return ``accepted``, ``duplicate``, ``conflict``, or ``expired``; raise ValueError if malformed."""
    try:
        envelope = parse_envelope(envelope_bytes)
    except PublishError:
        raise ValueError("malformed evidence envelope") from None
    now = now or datetime.now(timezone.utc)
    if envelope.safe_run.completed_at + RETENTION <= now:
        return "expired"
    key = {"export_version": envelope.export_version, "run_schema_version": envelope.run_schema_version,
           "run_id": str(envelope.run_id)}
    await _ensure(engine)
    try:
        async with engine.begin() as conn:
            await conn.execute(insert(_receipts).values(**key, export_hash=envelope.export_hash))
        return "accepted"
    except IntegrityError:
        pass
    async with engine.begin() as conn:
        existing = (await conn.execute(select(_receipts.c.export_hash).filter_by(**key))).scalar_one()
    return "duplicate" if existing == envelope.export_hash else "conflict"


async def receipts(engine) -> list[tuple[str, str]]:
    await _ensure(engine)
    async with engine.begin() as conn:
        rows = (await conn.execute(select(_receipts.c.run_id, _receipts.c.export_hash))).all()
    return [tuple(row) for row in rows]
