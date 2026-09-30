"""Version 1 metadata-only wire contract. Never include raw payload fields."""
import hashlib
import json
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Outcome = Literal["success", "error", "timeout", "cancelled", "partial", "disabled", "invalid", "skipped"]


class EvidenceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False,
                              protected_namespaces=("model_validate", "model_dump"))


class ConfigurationFingerprint(EvidenceModel):
    agent_version: str | None = None
    agent_build_sha: str | None = None
    generation_model: str | None = None
    embedding_model: str | None = None
    embedding_dimensions: int | None = Field(default=None, gt=0)
    graph_version: str | None = None
    prompt_hashes: dict[str, str] = Field(default_factory=dict)
    tool_registry_hash: str | None = None
    tool_schema_version: str | None = None
    chunking_version: str | None = None
    corpus_version: str | None = None
    index_snapshot: str | None = None
    reranker_version: str | None = None
    policy_version: str | None = None
    acl_version: str | None = None
    fingerprint: str = ""

    @model_validator(mode="after")
    def fingerprint_configuration(self):
        canonical = json.dumps(self.model_dump(exclude={"fingerprint"}), sort_keys=True, separators=(",", ":"))
        computed = "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
        if self.fingerprint and self.fingerprint != computed:
            raise ValueError("Configuration fingerprint mismatch")
        object.__setattr__(self, "fingerprint", computed)
        return self


class RetrievedEvidence(EvidenceModel):
    retrieval_source: Literal["qdrant", "neo4j"]
    document_id: str | None = None
    document_version: str | None = None
    chunk_id: str | None = None
    rank: int = Field(gt=0)
    retrieval_rank: int | None = Field(default=None, gt=0)
    score: float | None = None
    rerank_score: float | None = None
    content_hash: str
    content_ref: str | None = None


class RetrievalEvidence(EvidenceModel):
    query_hash: str
    collection: str | None = None
    index_snapshot: str | None = None
    corpus_version: str | None = None
    acl_filter_version: str | None = None
    items: list[RetrievedEvidence] = Field(default_factory=list)
    vector_status: Outcome = "success"
    graph_status: Outcome = "success"
    graph_error_type: str | None = None


class ModelCall(EvidenceModel):
    purpose: str = "unspecified"
    request_hash: str
    model: str | None = None
    requested_model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    completion_id: str | None = None
    status: Outcome = "success"
    error_type: str | None = None
    retry_count: int = Field(default=0, ge=0)
    latency_ms: float | None = Field(default=None, ge=0)


class ToolStep(EvidenceModel):
    step: int = Field(gt=0)
    tool_name: str
    registered: bool | None = None
    tool_schema_version: str | None = None
    arguments_hash: str | None = None
    arguments_ref: str | None = None
    result_hash: str | None = None
    result_ref: str | None = None
    authorization: str | None = None
    approval: str | None = None
    side_effect_key: str | None = None
    started_at: datetime
    latency_ms: float = Field(ge=0)
    status: Outcome
    retry_count: int = Field(default=0, ge=0)
    error_type: str | None = None


class DependencyObservation(EvidenceModel):
    sequence: int = Field(gt=0)
    name: str
    status: Outcome
    latency_ms: float = Field(ge=0)
    error_type: str | None = None
    attributes: dict[str, str | int | float | bool] = Field(default_factory=dict)


class ContentSlotRef(EvidenceModel):
    ordinal: int = Field(gt=0)
    item_ordinal: int = Field(gt=0)


class OperationEvidence(EvidenceModel):
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    parent_operation_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")
    name: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    schema_version: int = Field(gt=0)
    phase: str = Field(pattern=r"^[a-z][a-z0-9_.-]{0,63}$")
    sequence: int = Field(gt=0)
    timestamp: datetime
    status: Outcome | None = None
    latency_ms: float | None = Field(default=None, ge=0)
    facts: dict[str, str | int | float | bool] = Field(default_factory=dict)
    content_slots: dict[str, ContentSlotRef] = Field(default_factory=dict)

    @model_validator(mode="after")
    def valid_slots(self):
        if self.timestamp.tzinfo is None or len(self.facts) > 16 or len(self.content_slots) > 16:
            raise ValueError("invalid operation event")
        if len(set(self.content_slots.values())) != len(self.content_slots):
            raise ValueError("duplicate operation content slot")
        return self


class OperationalEvidence(EvidenceModel):
    latency_ms: float = Field(ge=0)
    time_to_first_output_ms: float | None = Field(default=None, ge=0)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    usage_complete: bool = False
    estimated_cost: float | None = None
    price_table_version: str | None = None
    agent_steps: int = Field(default=0, ge=0)
    retry_count: int = Field(default=0, ge=0)
    cache_hit: bool = False
    provider_throttled: bool = False


class RunEvidence(EvidenceModel):
    schema_version: Literal["1.0", "1.1"] = "1.0"
    run_id: str
    session_id: str
    trace_id: str | None = None
    started_at: datetime
    completed_at: datetime
    status: Literal["success", "error", "timeout", "cancelled"]
    error_type: str | None = None
    tenant_id: str | None = None
    user_id_hash: str | None = None
    user_role: str | None = None
    use_case: str | None = None
    input_hash: str
    output_hash: str | None = None
    input_ref: str | None = None
    output_ref: str | None = None
    configuration: ConfigurationFingerprint
    retrieval: list[RetrievalEvidence] = Field(default_factory=list)
    model_calls: list[ModelCall] = Field(default_factory=list)
    trajectory: list[ToolStep] = Field(default_factory=list)
    dependencies: list[DependencyObservation] = Field(default_factory=list)
    operation_events: list[OperationEvidence] = Field(default_factory=list)
    operations: OperationalEvidence
    unavailable: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def ordered_timestamps(self):
        if self.started_at.tzinfo is None or self.completed_at.tzinfo is None:
            raise ValueError("Evidence timestamps must be timezone-aware")
        if self.completed_at < self.started_at:
            raise ValueError("Completion precedes start")
        if self.operation_events and self.schema_version != "1.1":
            raise ValueError("operation events require schema 1.1")
        return self
