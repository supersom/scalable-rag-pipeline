"""Immutable, content-free contracts shared by evidence capture and delivery."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from enum import StrEnum
from types import MappingProxyType
from collections.abc import Mapping
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_serializer, model_validator

from .models import RunEvidence


def canonical_bytes(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("timestamp must be UTC-aware")
    return value


UtcTime = Annotated[datetime, AfterValidator(_utc)]
PositiveInt = Annotated[int, Field(strict=True, gt=0)]
NonnegativeInt = Annotated[int, Field(strict=True, ge=0)]
FiniteNonnegative = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Hash256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Outcome = Literal["success", "error", "timeout", "cancelled", "partial", "disabled", "invalid", "skipped"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False,
                              protected_namespaces=("model_validate", "model_dump"))


class PayloadKind(StrEnum):
    input = "input"
    output = "output"
    retrieval_query = "retrieval_query"
    retrieval_chunk = "retrieval_chunk"
    model_request = "model_request"
    model_response = "model_response"
    tool_arguments = "tool_arguments"
    tool_result = "tool_result"
    operation_content = "operation_content"


class UnavailableReason(StrEnum):
    capture_disabled = "capture_disabled"
    missing_tenant = "missing_tenant"
    unapproved_source = "unapproved_source"
    unapproved_tool = "unapproved_tool"
    unsupported_shape = "unsupported_shape"
    redaction_failed = "redaction_failed"
    payload_too_large = "payload_too_large"
    run_budget_exceeded = "run_budget_exceeded"
    payload_count_exceeded = "payload_count_exceeded"
    upload_failed = "upload_failed"
    upload_timeout = "upload_timeout"
    not_observed = "not_observed"


class DeliveryReason(StrEnum):
    active_capacity = "active_capacity"
    pending_capacity = "pending_capacity"
    runtime_stopped = "runtime_stopped"
    metadata_invalid = "metadata_invalid"
    metadata_too_large = "metadata_too_large"
    deadline_expired = "deadline_expired"
    database_unavailable = "database_unavailable"
    intent_failed = "intent_failed"
    broker_transient = "broker_transient"
    broker_denied = "broker_denied"
    retry_exhausted = "retry_exhausted"
    expired = "expired"
    identity_conflict = "identity_conflict"
    cleanup_failed = "cleanup_failed"


class EventIdentity(Contract):
    export_version: Literal["1.0", "1.1"] = "1.0"
    run_schema_version: Literal["1.0", "1.1"] = "1.0"
    run_id: UUID


class PayloadSlot(Contract):
    kind: PayloadKind
    ordinal: PositiveInt
    item_ordinal: PositiveInt | None = None

    @model_validator(mode="after")
    def input_output_ordinal(self):
        if self.kind in {PayloadKind.input, PayloadKind.output} and (self.ordinal != 1 or self.item_ordinal is not None):
            raise ValueError("input/output use ordinal 1 without item ordinal")
        if self.kind == PayloadKind.operation_content and self.item_ordinal is None:
            raise ValueError("operation content requires item ordinal")
        return self


class RedactedPayload(Contract):
    slot: PayloadSlot
    data: bytes = Field(min_length=1)
    policy_version: str = Field(min_length=1)


class PayloadRef(Contract):
    bucket: str = Field(min_length=1)
    key: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    tenant_partition: str = Field(min_length=1)
    slot: PayloadSlot
    byte_size: PositiveInt
    sha256: Hash256
    policy_version: str = Field(min_length=1)
    created_at: UtcTime
    expires_at: UtcTime

    @model_validator(mode="after")
    def ordered_expiry(self):
        if self.expires_at <= self.created_at:
            raise ValueError("payload expiry must follow creation")
        return self


class ObjectVersion(Contract):
    key: str = Field(min_length=1)
    version_id: str = Field(min_length=1)
    is_delete_marker: bool = False


class ManifestEntry(Contract):
    slot: PayloadSlot
    state: Literal["stored", "unavailable"]
    reference: PayloadRef | None = None
    reason: UnavailableReason | None = None

    @model_validator(mode="after")
    def consistent_state(self):
        if self.state == "stored":
            if self.reference is None or self.reason is not None or self.reference.slot != self.slot:
                raise ValueError("stored entry requires matching reference only")
        elif self.reason is None or self.reference is not None:
            raise ValueError("unavailable entry requires reason only")
        return self


class SafeConfiguration(Contract):
    generation_model: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: PositiveInt | None = None
    agent_build_sha: str | None = None
    fingerprint: str | None = None
    corpus_version: str | None = None
    index_snapshot: str | None = None
    acl_version: str | None = None


class SafeRetrievalItem(Contract):
    rank: PositiveInt
    retrieval_rank: PositiveInt | None = None
    score: float | None = Field(default=None, allow_inf_nan=False)
    rerank_score: float | None = Field(default=None, allow_inf_nan=False)
    document_pseudonym: str | None = None
    chunk_pseudonym: str | None = None
    content_digest_pseudonym: str | None = None


class SafeRetrieval(Contract):
    collection: str | None = None
    query_digest_pseudonym: str | None = None
    items: tuple[SafeRetrievalItem, ...] = ()
    vector_status: Outcome | None = None
    graph_status: Outcome | None = None


class SafeModelCall(Contract):
    ordinal: PositiveInt
    purpose: str | None = None
    model: str | None = None
    request_digest_pseudonym: str | None = None
    input_tokens: NonnegativeInt | None = None
    output_tokens: NonnegativeInt | None = None
    cached_tokens: NonnegativeInt | None = None
    status: Outcome
    latency_ms: FiniteNonnegative | None = None
    provider_request_pseudonym: str | None = None
    completion_pseudonym: str | None = None
    error_class: str | None = None
    retry_count: NonnegativeInt = 0


class SafeToolStep(Contract):
    step: PositiveInt
    tool_name: str | None = None
    status: Outcome
    latency_ms: FiniteNonnegative
    arguments_digest_pseudonym: str | None = None
    result_digest_pseudonym: str | None = None
    error_class: str | None = None
    retry_count: NonnegativeInt = 0


class SafeDependency(Contract):
    sequence: PositiveInt
    name: str | None = None
    status: Outcome
    latency_ms: FiniteNonnegative
    error_class: str | None = None
    attributes: Mapping[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def freeze_attributes(self):
        object.__setattr__(self, "attributes", MappingProxyType(dict(self.attributes)))
        return self

    @field_serializer("attributes")
    def serialize_attributes(self, value: Mapping[str, str | int | float | bool]) -> dict:
        return dict(value)

    def __deepcopy__(self, memo):
        # This model contains only immutable scalars and a copied read-only map.
        # Returning itself keeps Pydantic's deep model_copy usable for envelopes.
        return self


class SafeOperationEvent(Contract):
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_operation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    name: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    schema_version: PositiveInt
    phase: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    sequence: PositiveInt
    timestamp: UtcTime
    status: Outcome | None = None
    latency_ms: FiniteNonnegative | None = None
    facts: Mapping[str, str | int | float | bool] = Field(default_factory=dict)
    content_slots: Mapping[str, PayloadSlot] = Field(default_factory=dict)
    detail_state: Literal["recorded", "schema_unavailable"] = "recorded"

    @model_validator(mode="after")
    def freeze_maps(self):
        object.__setattr__(self, "facts", MappingProxyType(dict(self.facts)))
        object.__setattr__(self, "content_slots", MappingProxyType(dict(self.content_slots)))
        return self

    @field_serializer("facts", "content_slots")
    def serialize_maps(self, value: Mapping) -> dict:
        return dict(value)

    def __deepcopy__(self, memo):
        return self


class SafeOperations(Contract):
    latency_ms: FiniteNonnegative = 0.0
    time_to_first_output_ms: FiniteNonnegative | None = None
    input_tokens: NonnegativeInt | None = None
    output_tokens: NonnegativeInt | None = None
    cached_tokens: NonnegativeInt | None = None
    usage_complete: bool = False
    agent_steps: NonnegativeInt = 0
    retry_count: NonnegativeInt = 0
    cache_hit: bool = False
    provider_throttled: bool = False


class SafeRun(Contract):
    run_id: UUID
    started_at: UtcTime
    completed_at: UtcTime
    status: Literal["success", "error", "timeout", "cancelled"]
    trace_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    tenant_partition: str | None = None
    session_pseudonym: str | None = None
    user_pseudonym: str | None = None
    input_digest_pseudonym: str | None = None
    output_digest_pseudonym: str | None = None
    error_class: str | None = None
    configuration: SafeConfiguration = Field(default_factory=SafeConfiguration)
    retrieval: tuple[SafeRetrieval, ...] = ()
    model_calls: tuple[SafeModelCall, ...] = ()
    trajectory: tuple[SafeToolStep, ...] = ()
    dependencies: tuple[SafeDependency, ...] = ()
    operation_events: tuple[SafeOperationEvent, ...] = ()
    operations: SafeOperations = Field(default_factory=SafeOperations)
    unavailable: tuple[str, ...] = ()

    @model_validator(mode="after")
    def ordered_timestamps(self):
        if self.completed_at < self.started_at:
            raise ValueError("completion precedes start")
        return self


class FixtureRecord(Contract):
    tenant_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    source_kind: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    source_hash: str = Field(min_length=1)
    template_id: str | None = None
    collection: str | None = None
    document_id: str | None = None


class ExportPolicy(Contract):
    identity_key: str = Field(default="", repr=False, exclude=True)
    environment: str = "local"
    redaction_policy_version: str = "restricted-synthetic-v1"
    allowed_models: tuple[str, ...] = ()
    allowed_collections: tuple[str, ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allowed_dependencies: tuple[str, ...] = ()
    synthetic_sources: tuple[FixtureRecord, ...] = ()
    payload_bytes: PositiveInt = 65536
    run_bytes: PositiveInt = 1048576
    payload_slots: PositiveInt = 64


class PreparedExport(Contract):
    safe_run: SafeRun
    redaction_policy_version: str = Field(min_length=1)
    payloads: tuple[RedactedPayload, ...] = ()
    unavailable: tuple[ManifestEntry, ...] = ()


class ExportEnvelope(Contract):
    export_version: Literal["1.0", "1.1"] = "1.0"
    run_schema_version: Literal["1.0", "1.1"] = "1.0"
    run_id: UUID
    export_hash: Hash256 | None = None
    redaction_policy_version: str = Field(min_length=1)
    safe_run: SafeRun
    manifest: tuple[ManifestEntry, ...] = ()
    completeness: Literal["complete", "partial", "metadata_only"]

    @model_validator(mode="after")
    def validate_hash_and_identity(self):
        if self.run_id != self.safe_run.run_id:
            raise ValueError("run identities disagree")
        if self.export_version != self.run_schema_version:
            raise ValueError("export and run versions disagree")
        if self.run_schema_version == "1.0" and self.safe_run.operation_events:
            raise ValueError("operation events require schema 1.1")
        digest = hashlib.sha256(self.canonical_bytes(exclude_hash=True)).hexdigest()
        if self.export_hash is not None and self.export_hash != digest:
            raise ValueError("export hash mismatch")
        object.__setattr__(self, "export_hash", digest)
        return self

    def canonical_bytes(self, *, exclude_hash: bool = False) -> bytes:
        data = self.model_dump(mode="json", exclude={"export_hash"} if exclude_hash else None)
        if self.run_schema_version == "1.0":
            data["safe_run"].pop("operation_events", None)
        return canonical_bytes(data)


class DeliveryReceipt(Contract):
    status: Literal["disabled", "admitted", "dropped", "failed", "committed"]
    reason: DeliveryReason | None = None
    identity: EventIdentity | None = None
    export_hash: Hash256 | None = None


class PublishReceipt(Contract):
    acknowledgement_id: str = Field(min_length=1)


class UploadIntent(Contract):
    run_id: UUID
    namespace: str = Field(min_length=1)
    lease_token: UUID
    expires_at: UtcTime
    state: Literal["open", "closed", "cleanup_owned"] = "open"


class OutboxClaim(Contract):
    identity: EventIdentity
    export_hash: Hash256
    envelope_bytes: bytes = Field(min_length=1)
    expires_at: UtcTime
    lease_token: UUID
    lease_expires_at: UtcTime
    attempts: NonnegativeInt = 0


class CapturedRun(Contract):
    event: RunEvidence
    prepared: PreparedExport


class CleanupClaim(Contract):
    namespace: str = Field(min_length=1)
    lease_token: UUID
    expires_at: UtcTime
    state: Literal["cleanup_owned"] = "cleanup_owned"


class CleanupReport(Contract):
    versions_examined: NonnegativeInt = 0
    versions_deleted: NonnegativeInt = 0
    failures: NonnegativeInt = 0
    runs_expired: NonnegativeInt = 0
    namespaces_examined: NonnegativeInt = 0
    namespaces_eligible: NonnegativeInt = 0
    namespaces_cleaned: NonnegativeInt = 0

    @property
    def deleted_versions(self) -> int:
        return self.versions_deleted
