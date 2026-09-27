"""Versioned, KMS-encrypted storage of redacted payloads with authorized exact-version reads.

Object keys: ``evidence/{environment}/{tenant_partition}/{run_id}/{YYYY-MM-DD}/{kind}/{ordinal}[/{item_ordinal}]``.
``namespace()`` (everything up to and including the run UUID) is the unit that upload intents
and orphan cleanup coordinate on; the UTC date sits below it so one run never spans namespaces.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal, Protocol
from uuid import UUID

from .bounded_io import BoundedIO, BoundedIOClosed, BoundedIOTimeout
from .delivery_config import DeliveryConfig
from .delivery_models import ObjectVersion, PayloadRef, PayloadSlot, RedactedPayload

KEY_ROOT = "evidence"
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_-]{0,127}$")
_DELETE_BATCH = 1000

StorageReason = Literal["failed", "timeout", "closed", "integrity", "too_large", "disabled", "limit"]
DenialReason = Literal["tenant", "reference", "namespace", "policy", "expired", "disabled"]


class PayloadAccessDenied(PermissionError):
    """Authorization or reference validation failed before any object IO."""

    def __init__(self, reason: DenialReason) -> None:
        self.reason = reason
        super().__init__(f"payload access denied: {reason}")


class PayloadStorageError(RuntimeError):
    """A content-free storage failure; never carries SDK messages, keys, or payload text."""

    def __init__(self, reason: StorageReason) -> None:
        self.reason = reason
        super().__init__(f"payload storage failed: {reason}")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _segment(value: object) -> str:
    if not isinstance(value, str) or not _SEGMENT.fullmatch(value):
        raise PayloadAccessDenied("namespace")
    return value


def _slot_path(slot: PayloadSlot) -> str:
    path = f"{slot.kind.value}/{slot.ordinal}"
    return path if slot.item_ordinal is None else f"{path}/{slot.item_ordinal}"


class PayloadStore(Protocol):
    async def put(self, run_id: UUID, tenant_partition: str, payload: RedactedPayload, *,
                  deadline: float) -> PayloadRef: ...

    async def get(self, reference: PayloadRef, authorized_tenant: str, *, deadline: float) -> bytes: ...

    async def delete_versions(self, versions: tuple[ObjectVersion, ...], *, deadline: float) -> int: ...

    async def list_versions(self, prefix: str, *, deadline: float) -> tuple[ObjectVersion, ...]: ...

    async def close(self, deadline: float) -> int: ...


class DisabledPayloadStore:
    """Content capture disabled: no client, no executor, nothing stored or resolvable."""

    async def put(self, run_id: UUID, tenant_partition: str, payload: RedactedPayload, *,
                  deadline: float) -> PayloadRef:
        raise PayloadStorageError("disabled")

    async def get(self, reference: PayloadRef, authorized_tenant: str, *, deadline: float) -> bytes:
        raise PayloadAccessDenied("disabled")

    async def delete_versions(self, versions: tuple[ObjectVersion, ...], *, deadline: float) -> int:
        if versions:
            raise PayloadStorageError("disabled")
        return 0

    async def list_versions(self, prefix: str, *, deadline: float) -> tuple[ObjectVersion, ...]:
        return ()

    async def close(self, deadline: float) -> int:
        return 0


class S3PayloadStore:
    def __init__(self, client: Any, *, bucket: str, kms_key_id: str, environment: str, io: BoundedIO,
                 clock: Callable[[], datetime] = _utc_now, monotonic: Callable[[], float] = time.monotonic,
                 policy_versions: tuple[str, ...] = ("restricted-synthetic-v1",), max_bytes: int = 65536,
                 retention_days: int = 30, list_page_size: int = 1000, max_listed_versions: int = 10000,
                 owns_client: bool = False) -> None:
        if not bucket or not kms_key_id:
            raise ValueError("payload storage requires a bucket and dedicated KMS key")
        self._client = client
        self._bucket = bucket
        self._kms_key_id = kms_key_id
        self._root = f"{KEY_ROOT}/{_segment(environment)}/"
        self._io = io
        self._clock = clock
        self._monotonic = monotonic
        self._policies = frozenset(policy_versions)
        self._max_bytes = max_bytes
        self._retention = timedelta(days=retention_days)
        self._page_size = list_page_size
        self._max_listed = max_listed_versions
        self._owns_client = owns_client

    @classmethod
    def from_config(cls, config: DeliveryConfig, *, clock: Callable[[], datetime] = _utc_now,
                    monotonic: Callable[[], float] = time.monotonic) -> "S3PayloadStore":
        if not config.bucket or not config.kms_key_id:
            raise ValueError("payload storage requires a bucket and dedicated KMS key")
        import boto3
        from botocore.config import Config
        client = boto3.client(
            "s3", region_name=config.aws_region, endpoint_url=config.s3_endpoint_url,
            config=Config(connect_timeout=config.sdk_connect_timeout_seconds,
                          read_timeout=config.sdk_read_timeout_seconds,
                          retries={"total_max_attempts": config.sdk_attempts, "mode": "standard"},
                          max_pool_connections=config.s3_workers))
        return cls(client, bucket=config.bucket, kms_key_id=config.kms_key_id, environment=config.environment,
                   io=BoundedIO(config.s3_workers, clock=monotonic, thread_name_prefix="evidence-s3"),
                   clock=clock, monotonic=monotonic, max_bytes=config.payload_bytes,
                   retention_days=config.content_retention_days, owns_client=True)

    def namespace(self, tenant_partition: str, run_id: UUID) -> str:
        return f"{self._root}{_segment(tenant_partition)}/{UUID(str(run_id))}/"

    def _key(self, tenant_partition: str, run_id: UUID, slot: PayloadSlot, created: datetime) -> str:
        return f"{self.namespace(tenant_partition, run_id)}{created.date().isoformat()}/{_slot_path(slot)}"

    async def _call(self, fn: Callable[[], Any], deadline: float) -> Any:
        try:
            return await self._io.call(fn, deadline=deadline)
        except BoundedIOTimeout:
            raise PayloadStorageError("timeout") from None
        except BoundedIOClosed:
            raise PayloadStorageError("closed") from None
        except (PayloadStorageError, PayloadAccessDenied):
            raise
        except Exception:
            # SDK errors can echo keys or request details; keep only a fixed reason.
            raise PayloadStorageError("failed") from None

    async def put(self, run_id: UUID, tenant_partition: str, payload: RedactedPayload, *,
                  deadline: float) -> PayloadRef:
        if not isinstance(payload, RedactedPayload):
            raise PayloadAccessDenied("reference")
        if payload.policy_version not in self._policies:
            raise PayloadAccessDenied("policy")
        data = payload.data
        if len(data) > self._max_bytes:
            raise PayloadStorageError("too_large")
        created = self._clock()
        if created.tzinfo is None or created.utcoffset() != timedelta(0):
            raise ValueError("storage clock must return UTC-aware time")
        key = self._key(tenant_partition, run_id, payload.slot, created)

        def upload() -> dict:
            return self._client.put_object(Bucket=self._bucket, Key=key, Body=data, ContentType="application/json",
                                           ServerSideEncryption="aws:kms", SSEKMSKeyId=self._kms_key_id)
        response = await self._call(upload, deadline)
        version_id = response.get("VersionId")
        if not isinstance(version_id, str) or not version_id or version_id == "null" \
                or response.get("ServerSideEncryption") != "aws:kms":
            raise PayloadStorageError("failed")
        return PayloadRef(bucket=self._bucket, key=key, version_id=version_id, tenant_partition=tenant_partition,
                          slot=payload.slot, byte_size=len(data), sha256=hashlib.sha256(data).hexdigest(),
                          policy_version=payload.policy_version, created_at=created,
                          expires_at=created + self._retention)

    def _authorize(self, reference: PayloadRef, authorized_tenant: str | None) -> None:
        if not isinstance(reference, PayloadRef):
            raise PayloadAccessDenied("reference")
        if not authorized_tenant or authorized_tenant != reference.tenant_partition:
            raise PayloadAccessDenied("tenant")
        if reference.bucket != self._bucket or reference.byte_size > self._max_bytes:
            raise PayloadAccessDenied("reference")
        tenant_prefix = f"{self._root}{_segment(reference.tenant_partition)}/"
        if not reference.key.startswith(tenant_prefix):
            raise PayloadAccessDenied("namespace")
        run_segment, _, rest = reference.key[len(tenant_prefix):].partition("/")
        try:
            run_id = UUID(run_segment)
        except ValueError:
            raise PayloadAccessDenied("namespace") from None
        if reference.key != self._key(reference.tenant_partition, run_id, reference.slot, reference.created_at):
            raise PayloadAccessDenied("namespace")
        if reference.policy_version not in self._policies:
            raise PayloadAccessDenied("policy")
        if reference.expires_at != reference.created_at + self._retention:
            raise PayloadAccessDenied("reference")
        if self._clock() >= reference.expires_at:
            raise PayloadAccessDenied("expired")

    async def get(self, reference: PayloadRef, authorized_tenant: str, *, deadline: float) -> bytes:
        self._authorize(reference, authorized_tenant)
        limit = self._max_bytes

        def download() -> tuple[str | None, bytes]:
            response = self._client.get_object(Bucket=reference.bucket, Key=reference.key,
                                               VersionId=reference.version_id)
            body = response["Body"]
            try:
                return response.get("VersionId"), body.read(limit + 1)
            finally:
                body.close()
        version_id, data = await self._call(download, deadline)
        if version_id != reference.version_id or len(data) != reference.byte_size \
                or hashlib.sha256(data).hexdigest() != reference.sha256:
            raise PayloadStorageError("integrity")
        return data

    def _check_prefix(self, prefix: str) -> None:
        if not isinstance(prefix, str) or not prefix.startswith(self._root) or not prefix.endswith("/"):
            raise PayloadAccessDenied("namespace")
        segments = prefix[len(self._root):-1].split("/")
        # A tenant or narrower namespace only; never the whole environment root.
        if not segments or any(not _SEGMENT.fullmatch(segment) for segment in segments):
            raise PayloadAccessDenied("namespace")

    async def list_versions(self, prefix: str, *, deadline: float) -> tuple[ObjectVersion, ...]:
        self._check_prefix(prefix)
        found: list[ObjectVersion] = []
        marker: dict[str, str] = {}
        while True:
            request = {"Bucket": self._bucket, "Prefix": prefix, "MaxKeys": self._page_size, **marker}
            page = await self._call(lambda: self._client.list_object_versions(**request), deadline)
            for entries, is_marker in ((page.get("Versions", ()), False), (page.get("DeleteMarkers", ()), True)):
                for entry in entries:
                    if entry["Key"].startswith(prefix):
                        found.append(ObjectVersion(key=entry["Key"], version_id=entry["VersionId"],
                                                   is_delete_marker=is_marker))
            if len(found) > self._max_listed:
                raise PayloadStorageError("limit")
            if not page.get("IsTruncated"):
                return tuple(found)
            marker = {"KeyMarker": page["NextKeyMarker"], "VersionIdMarker": page["NextVersionIdMarker"]}

    async def delete_versions(self, versions: tuple[ObjectVersion, ...], *, deadline: float) -> int:
        for version in versions:
            if not isinstance(version, ObjectVersion):
                raise PayloadAccessDenied("reference")
            self._check_prefix(version.key.rpartition("/")[0] + "/")
        deleted = 0
        for start in range(0, len(versions), _DELETE_BATCH):
            objects = [{"Key": v.key, "VersionId": v.version_id} for v in versions[start:start + _DELETE_BATCH]]
            request = {"Objects": objects, "Quiet": True}
            response = await self._call(
                lambda: self._client.delete_objects(Bucket=self._bucket, Delete=request), deadline)
            deleted += len(objects) - len(response.get("Errors", ()))
        return deleted

    async def close(self, deadline: float) -> int:
        remaining = await self._io.close(deadline)
        if remaining == 0 and self._owns_client:
            self._client.close()
        return remaining
