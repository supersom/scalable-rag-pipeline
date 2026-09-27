"""Opt-in evidence delivery settings; validation does not create clients."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DeliveryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    enabled: bool = False
    capture_content: bool = False
    environment: str = "prod"
    database_url: str | None = Field(default=None, repr=False)
    queue_url: str | None = None
    identity_key: str | None = Field(default=None, repr=False, exclude=True)
    jwt_secret: str | None = Field(default=None, repr=False, exclude=True)
    content_policy: str | None = None
    fixture_manifest: str | None = None
    bucket: str | None = None
    kms_key_id: str | None = None
    s3_endpoint_url: str | None = None
    aws_region: str = "us-east-1"
    payload_bytes: int = Field(default=65536, gt=0)
    run_bytes: int = Field(default=1048576, gt=0)
    payload_slots: int = Field(default=64, gt=0)
    active_runs: int = Field(default=32, gt=0)
    active_bytes: int = Field(default=16777216, gt=0)
    pending_runs: int = Field(default=32, gt=0)
    pending_bytes: int = Field(default=16777216, gt=0)
    runtime_workers: int = Field(default=2, gt=0)
    s3_workers: int = Field(default=2, gt=0)
    deadline_seconds: float = Field(default=20, gt=0)
    commit_reserve_seconds: float = Field(default=3, gt=0)
    sdk_connect_timeout_seconds: float = Field(default=1, gt=0)
    sdk_read_timeout_seconds: float = Field(default=2, gt=0)
    sdk_attempts: int = Field(default=2, gt=0)
    queue_body_bytes: int = Field(default=131072, gt=0)
    delivery_batch: int = Field(default=10, gt=0)
    send_workers: int = Field(default=2, gt=0)
    claim_lease_seconds: float = Field(default=60, gt=0)
    producer_attempts: int = Field(default=8, gt=0)
    backoff_base_seconds: float = Field(default=1, gt=0)
    backoff_cap_seconds: float = Field(default=60, gt=0)
    shutdown_drain_seconds: float = Field(default=10, gt=0)
    intent_lease_seconds: float = Field(default=60, gt=0)
    orphan_grace_seconds: float = Field(default=86400, gt=0)
    content_retention_days: int = Field(default=30, gt=0)
    metadata_retention_days: int = Field(default=90, gt=0)

    @model_validator(mode="after")
    def conditional_configuration(self):
        if self.content_policy not in (None, "restricted-synthetic-v1"):
            raise ValueError("unsupported content policy")
        if self.commit_reserve_seconds >= self.deadline_seconds:
            raise ValueError("commit reserve must be shorter than deadline")
        if self.intent_lease_seconds < self.deadline_seconds:
            raise ValueError("intent lease must cover upload and commit deadline")
        if self.claim_lease_seconds < self.sdk_attempts * (self.sdk_connect_timeout_seconds + self.sdk_read_timeout_seconds):
            raise ValueError("claim lease must cover broker processing")
        if self.backoff_cap_seconds < self.backoff_base_seconds:
            raise ValueError("backoff cap must be at least base")
        if self.payload_bytes > self.run_bytes or self.run_bytes > self.active_bytes or self.run_bytes > self.pending_bytes:
            raise ValueError("byte budgets are inconsistent")
        if self.capture_content and not self.enabled:
            raise ValueError("content capture requires evidence delivery")
        if self.enabled:
            if not all((self.database_url, self.queue_url, self.identity_key)):
                raise ValueError("metadata delivery requires database, queue and identity key")
            if self.jwt_secret and self.identity_key == self.jwt_secret:
                raise ValueError("evidence identity key must differ from JWT secret")
        if self.capture_content:
            if self.environment not in ("local", "test"):
                raise ValueError("content capture is restricted to local synthetic mode")
            if self.content_policy != "restricted-synthetic-v1":
                raise ValueError("content capture requires synthetic policy")
            if not all((self.fixture_manifest, self.bucket, self.kms_key_id, self.s3_endpoint_url)):
                raise ValueError("content capture requires manifest, bucket, KMS key and endpoint")
        return self

    @classmethod
    def from_settings(cls, settings: object) -> "DeliveryConfig":
        return cls(
            enabled=settings.EVAL_EVIDENCE_ENABLED,
            capture_content=settings.EVAL_CAPTURE_CONTENT,
            environment=settings.ENV,
            database_url=settings.DATABASE_URL,
            queue_url=settings.EVAL_QUEUE_URL,
            identity_key=settings.EVAL_IDENTITY_HASH_KEY,
            jwt_secret=settings.JWT_SECRET_KEY,
            content_policy=settings.EVAL_CONTENT_POLICY,
            fixture_manifest=settings.EVAL_FIXTURE_MANIFEST,
            bucket=settings.EVAL_S3_BUCKET,
            kms_key_id=settings.EVAL_KMS_KEY_ID,
            s3_endpoint_url=settings.EVAL_S3_ENDPOINT_URL,
            aws_region=settings.AWS_REGION,
            payload_bytes=settings.EVAL_PAYLOAD_BYTES,
            run_bytes=settings.EVAL_RUN_BYTES,
            payload_slots=settings.EVAL_PAYLOAD_SLOTS,
            active_runs=settings.EVAL_ACTIVE_RUNS,
            active_bytes=settings.EVAL_ACTIVE_BYTES,
            pending_runs=settings.EVAL_PENDING_RUNS,
            pending_bytes=settings.EVAL_PENDING_BYTES,
            runtime_workers=settings.EVAL_RUNTIME_WORKERS,
            s3_workers=settings.EVAL_S3_WORKERS,
            deadline_seconds=settings.EVAL_DEADLINE_SECONDS,
            commit_reserve_seconds=settings.EVAL_COMMIT_RESERVE_SECONDS,
            sdk_connect_timeout_seconds=settings.EVAL_SDK_CONNECT_TIMEOUT_SECONDS,
            sdk_read_timeout_seconds=settings.EVAL_SDK_READ_TIMEOUT_SECONDS,
            sdk_attempts=settings.EVAL_SDK_ATTEMPTS,
            queue_body_bytes=settings.EVAL_QUEUE_BODY_BYTES,
            delivery_batch=settings.EVAL_DELIVERY_BATCH,
            send_workers=settings.EVAL_SEND_WORKERS,
            claim_lease_seconds=settings.EVAL_CLAIM_LEASE_SECONDS,
            producer_attempts=settings.EVAL_PRODUCER_ATTEMPTS,
            backoff_base_seconds=settings.EVAL_BACKOFF_BASE_SECONDS,
            backoff_cap_seconds=settings.EVAL_BACKOFF_CAP_SECONDS,
            shutdown_drain_seconds=settings.EVAL_SHUTDOWN_DRAIN_SECONDS,
            intent_lease_seconds=settings.EVAL_INTENT_LEASE_SECONDS,
            orphan_grace_seconds=settings.EVAL_ORPHAN_GRACE_SECONDS,
            content_retention_days=settings.EVAL_CONTENT_RETENTION_DAYS,
            metadata_retention_days=settings.EVAL_METADATA_RETENTION_DAYS,
        )
