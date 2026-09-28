"""SQLAlchemy mappings for evidence tables created by ``migrations/*.sql``.

Independent metadata: never shared with chat history and never used with ``create_all``; the
SQL migrations are the schema source of truth and a live test checks these columns match.
"""
from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import BigInteger, CHAR, DateTime, ForeignKey, Integer, LargeBinary, MetaData, Text, Uuid
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

EVIDENCE_METADATA = MetaData()


class EvidenceBase(DeclarativeBase):
    metadata = EVIDENCE_METADATA


def _ts(nullable: bool = False) -> Mapped:
    return mapped_column(DateTime(timezone=True), nullable=nullable)


class EvidenceRun(EvidenceBase):
    __tablename__ = "evaluation_runs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    export_version: Mapped[str] = mapped_column(Text)
    run_schema_version: Mapped[str] = mapped_column(Text)
    run_id: Mapped[UUID] = mapped_column(Uuid)
    export_hash: Mapped[str] = mapped_column(CHAR(64))
    envelope: Mapped[bytes] = mapped_column(LargeBinary)
    redaction_policy_version: Mapped[str] = mapped_column(Text)
    completeness: Mapped[str] = mapped_column(Text)
    completed_at: Mapped[datetime] = _ts()
    created_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = _ts()


class EvidenceOutbox(EvidenceBase):
    __tablename__ = "evaluation_outbox"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_pk: Mapped[int] = mapped_column(BigInteger, ForeignKey("evaluation_runs.id", ondelete="CASCADE"))
    state: Mapped[str] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = _ts()
    lease_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    lease_until: Mapped[datetime | None] = _ts(nullable=True)
    acknowledgement_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _ts()
    updated_at: Mapped[datetime] = _ts()
    delivered_at: Mapped[datetime | None] = _ts(nullable=True)


class EvidencePayloadRef(EvidenceBase):
    __tablename__ = "evaluation_payload_refs"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    run_pk: Mapped[int] = mapped_column(BigInteger, ForeignKey("evaluation_runs.id", ondelete="CASCADE"))
    namespace: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text)
    ordinal: Mapped[int] = mapped_column(Integer)
    item_ordinal: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bucket: Mapped[str] = mapped_column(Text)
    object_key: Mapped[str] = mapped_column(Text)
    version_id: Mapped[str] = mapped_column(Text)
    tenant_partition: Mapped[str] = mapped_column(Text)
    byte_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(CHAR(64))
    policy_version: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = _ts()
    expires_at: Mapped[datetime] = _ts()


class EvidenceUploadIntent(EvidenceBase):
    __tablename__ = "evaluation_upload_intents"

    namespace: Mapped[str] = mapped_column(Text, primary_key=True)
    run_id: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    state: Mapped[str] = mapped_column(Text)
    lease_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    expires_at: Mapped[datetime | None] = _ts(nullable=True)
    created_at: Mapped[datetime] = _ts()
    closed_at: Mapped[datetime | None] = _ts(nullable=True)
    cleanup_token: Mapped[UUID | None] = mapped_column(Uuid, nullable=True)
    cleanup_lease_until: Mapped[datetime | None] = _ts(nullable=True)
    cleanup_completed_at: Mapped[datetime | None] = _ts(nullable=True)
