"""Publish committed metadata envelopes to SQS: exact stored bytes, finite IO, content-free failures.

Wire contract: the message body is the canonical envelope exactly as committed. Four String message
attributes (``export_version``, ``run_schema_version``, ``run_id``, ``export_hash``) repeat the
body's identity and hash for routing/inspection only; consumers must deduplicate from the validated
body, never from the attributes. Every failure becomes a ``PublishError`` carrying a fixed
``DeliveryReason`` and a transient/permanent flag, never an SDK message, queue body, or cause chain.

Classification: explicit server-side authorization, missing-queue, KMS-key, and invalid-content
rejections plus local region/parameter misconfiguration are permanent (quarantine for operator
action). Throttling, 5xx, connection errors, expired tokens/requests, unresolvable credentials (an
IMDS/role-provider hiccup looks identical to missing credentials), deadlines, unknown error codes,
and malformed acknowledgements are transient; the outbox's attempt cap bounds them.
"""
from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .bounded_io import BoundedIO, BoundedIOClosed, BoundedIOTimeout
from .delivery_config import DeliveryConfig
from .delivery_models import DeliveryReason, ExportEnvelope, PublishReceipt

# SQS accepts only these characters in a message body (#x9 | #xA | #xD | #x20-#xD7FF | #xE000-#xFFFD | astral).
_SQS_TEXT = re.compile(r"[\t\n\r\x20-\uD7FF\uE000-\uFFFD\U00010000-\U0010FFFF]*")

_DENIED_CODES = frozenset({
    "AccessDenied", "AccessDeniedException", "AuthorizationError", "InvalidClientTokenId",
    "UnrecognizedClientException", "SignatureDoesNotMatch", "IncompleteSignature", "MissingAuthenticationToken",
    "InvalidSecurity", "AWS.SimpleQueueService.NonExistentQueue", "QueueDoesNotExist", "InvalidAddress",
    "UnsupportedOperation", "InvalidMessageContents", "InvalidParameterValue", "MessageTooLong",
    "KmsAccessDenied", "KmsDisabled", "KmsNotFound", "KmsInvalidState", "KmsInvalidKeyUsage", "KmsOptInRequired",
})


class PublishError(RuntimeError):
    def __init__(self, reason: DeliveryReason, *, permanent: bool) -> None:
        self.reason = reason
        self.permanent = permanent
        super().__init__(f"evidence publish failed: {reason.value}")


def _invalid() -> PublishError:
    return PublishError(DeliveryReason.metadata_invalid, permanent=True)


def parse_envelope(envelope_bytes: bytes) -> ExportEnvelope:
    """Revalidate stored bytes: UTF-8, canonical JSON, recomputed hash, SQS-safe characters."""
    envelope = None
    if isinstance(envelope_bytes, bytes) and envelope_bytes:
        # Parse errors can quote content; keep them out of any raised exception's context.
        try:
            text = envelope_bytes.decode("utf-8")
            raw = json.loads(text)
            if isinstance(raw, dict) and raw.get("export_hash") is not None:
                candidate = ExportEnvelope.model_validate(raw)
                if candidate.canonical_bytes() == envelope_bytes and _SQS_TEXT.fullmatch(text):
                    envelope = candidate
        except Exception:
            envelope = None
    if envelope is None:
        raise _invalid()
    return envelope


def _classify(error: BaseException) -> PublishError:
    from botocore.exceptions import ClientError, NoRegionError, ParamValidationError
    if isinstance(error, ClientError):
        code = error.response.get("Error", {}).get("Code")
        permanent = code in _DENIED_CODES
    else:
        # Credential-resolution errors stay transient: a per-row quarantine would sweep a healthy backlog.
        permanent = isinstance(error, (NoRegionError, ParamValidationError))
    return PublishError(DeliveryReason.broker_denied if permanent else DeliveryReason.broker_transient,
                        permanent=permanent)


class RunEvidencePublisher:
    def __init__(self, client: Any, *, queue_url: str, io: BoundedIO, max_body_bytes: int,
                 owns_client: bool = False) -> None:
        if not queue_url or max_body_bytes <= 0:
            raise ValueError("publisher requires a queue URL and a positive body cap")
        self._client = client
        self.queue_url = queue_url
        self._io = io
        self.max_body_bytes = max_body_bytes
        self._owns_client = owns_client

    @classmethod
    def from_config(cls, config: DeliveryConfig, *,
                    monotonic: Callable[[], float] = time.monotonic) -> "RunEvidencePublisher":
        parts = urlsplit(config.queue_url or "")
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise ValueError("publisher requires an http(s) queue URL")
        import boto3
        from botocore.config import Config
        # The queue URL's own origin is the endpoint, for AWS and local emulators alike.
        client = boto3.client(
            "sqs", region_name=config.aws_region, endpoint_url=f"{parts.scheme}://{parts.netloc}",
            config=Config(connect_timeout=config.sdk_connect_timeout_seconds,
                          read_timeout=config.sdk_read_timeout_seconds,
                          retries={"total_max_attempts": config.sdk_attempts, "mode": "standard"},
                          max_pool_connections=config.send_workers))
        return cls(client, queue_url=config.queue_url,
                   io=BoundedIO(config.send_workers, clock=monotonic, thread_name_prefix="evidence-sqs"),
                   max_body_bytes=config.queue_body_bytes, owns_client=True)

    async def publish(self, envelope_bytes: bytes, *, deadline: float) -> PublishReceipt:
        if isinstance(envelope_bytes, bytes) and len(envelope_bytes) > self.max_body_bytes:
            raise PublishError(DeliveryReason.metadata_too_large, permanent=True)
        envelope = parse_envelope(envelope_bytes)
        body = envelope_bytes.decode("utf-8")
        attributes = {name: {"DataType": "String", "StringValue": value} for name, value in (
            ("export_version", envelope.export_version), ("run_schema_version", envelope.run_schema_version),
            ("run_id", str(envelope.run_id)), ("export_hash", envelope.export_hash))}

        def send() -> dict:
            return self._client.send_message(QueueUrl=self.queue_url, MessageBody=body,
                                             MessageAttributes=attributes)
        failure: PublishError | None = None
        try:
            response = await self._io.call(send, deadline=deadline)
        except BoundedIOTimeout:
            failure = PublishError(DeliveryReason.deadline_expired, permanent=False)
        except BoundedIOClosed:
            failure = PublishError(DeliveryReason.runtime_stopped, permanent=False)
        except Exception as error:
            failure = _classify(error)
        # Raised outside the handlers so no SDK exception stays reachable through __context__.
        if failure is not None:
            raise failure
        message_id = response.get("MessageId") if isinstance(response, dict) else None
        if not isinstance(message_id, str) or not message_id:
            raise PublishError(DeliveryReason.broker_transient, permanent=False)
        return PublishReceipt(acknowledgement_id=message_id)

    async def close(self, deadline: float) -> int:
        remaining = await self._io.close(deadline)
        if remaining == 0 and self._owns_client:
            self._client.close()
        return remaining
