"""Archives committed evidence envelopes read off the queue to S3, keyed by run_id.

Temporary: exists only because nothing yet consumes eval-runs (Phase 3's evaluation worker
does that). Using run_id as the object key makes identical redelivery an idempotent overwrite;
a genuinely conflicting hash for the same run_id is preserved separately, never silently merged
over the first copy. This is not a scorer and stores no state beyond archive objects and a
conflict counter.
"""
from __future__ import annotations

import logging
from typing import Any

from prometheus_client import REGISTRY, Counter, CollectorRegistry

from services.api.app.evaluation.publisher import PublishError, parse_envelope

logger = logging.getLogger(__name__)

ARCHIVED = "archived"
CONFLICT = "conflict"
INVALID = "invalid"


class DrainConsumer:
    def __init__(self, client: Any, *, bucket: str, kms_key_id: str | None = None,
                registry: CollectorRegistry = REGISTRY) -> None:
        self._client = client
        self._bucket = bucket
        self._kms_key_id = kms_key_id
        self._conflicts = Counter("evidence_archive_conflicts_total",
                                  "Envelopes whose run_id already had different archived content",
                                  registry=registry)

    async def process_message(self, body: bytes) -> str:
        try:
            envelope = parse_envelope(body)
        except PublishError:
            logger.warning("evidence archive skipped reason=invalid_envelope")
            return INVALID
        key = f"queue-archive/{envelope.run_id}.json"
        existing = self._get(key)
        if existing is not None:
            if existing == body:
                return ARCHIVED  # identical redelivery; nothing to do
            conflict_key = f"queue-archive-conflict/{envelope.run_id}-{_short_token()}.json"
            self._put(conflict_key, body)
            self._conflicts.inc()
            logger.warning("evidence archive conflict run_id=%s", envelope.run_id)
            return CONFLICT
        self._put(key, body)
        return ARCHIVED

    def _get(self, key: str) -> bytes | None:
        try:
            return self._client.get_object(Bucket=self._bucket, Key=key)["Body"].read()
        except self._client.exceptions.NoSuchKey:
            return None
        except Exception as error:
            if getattr(error, "response", {}).get("Error", {}).get("Code") == "NoSuchKey":
                return None
            raise

    def _put(self, key: str, body: bytes) -> None:
        kwargs: dict[str, Any] = {"Bucket": self._bucket, "Key": key, "Body": body,
                                  "ServerSideEncryption": "aws:kms", "ContentType": "application/json"}
        if self._kms_key_id:
            kwargs["SSEKMSKeyId"] = self._kms_key_id
        self._client.put_object(**kwargs)


def _short_token() -> str:
    from uuid import uuid4
    return uuid4().hex[:12]
