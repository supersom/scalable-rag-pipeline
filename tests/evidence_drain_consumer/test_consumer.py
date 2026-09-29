"""Drain consumer: a temporary placeholder until Phase 3's evaluation worker exists.

It archives committed evidence envelopes read off the queue to S3, keyed by run_id, so nothing
is silently lost while nothing else consumes eval-runs. It is not a scorer and stores no state
of its own beyond the archive objects and a conflict counter.
"""
import boto3
import pytest
from moto import mock_aws
from prometheus_client import CollectorRegistry

from services.evidence_drain_consumer.consumer import DrainConsumer


@pytest.fixture
def envelope_bytes():
    from datetime import datetime, timezone
    from uuid import uuid4
    from services.api.app.evaluation.delivery_models import SafeConfiguration, SafeRun
    from services.api.app.evaluation.metadata_export import seal_export
    from services.api.app.evaluation.delivery_models import ExportEnvelope
    safe_run = SafeRun(run_id=uuid4(), started_at=datetime.now(timezone.utc),
                       completed_at=datetime.now(timezone.utc), status="success",
                       configuration=SafeConfiguration(generation_model="fake-chat",
                                                               embedding_model="fake-embed"))
    envelope = ExportEnvelope(run_id=safe_run.run_id, safe_run=safe_run,
                              redaction_policy_version="restricted-synthetic-v1", completeness="metadata_only")
    return envelope.canonical_bytes(), envelope.run_id


@pytest.fixture
def consumer():
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="evidence-archive-test")
        client.put_bucket_versioning(Bucket="evidence-archive-test",
                                     VersioningConfiguration={"Status": "Enabled"})
        registry = CollectorRegistry()
        yield DrainConsumer(client, bucket="evidence-archive-test", registry=registry)


def test_archives_a_valid_envelope_by_run_id(consumer, envelope_bytes):
    body, run_id = envelope_bytes
    outcome = pytest_run(consumer.process_message(body))
    assert outcome == "archived"
    obj = consumer._client.get_object(Bucket="evidence-archive-test", Key=f"queue-archive/{run_id}.json")
    assert obj["Body"].read() == body


def test_identical_redelivery_is_a_no_op_not_a_conflict(consumer, envelope_bytes):
    body, run_id = envelope_bytes
    pytest_run(consumer.process_message(body))
    outcome = pytest_run(consumer.process_message(body))
    assert outcome == "archived"
    listing = consumer._client.list_objects_v2(Bucket="evidence-archive-test", Prefix="queue-archive-conflict/")
    assert listing.get("KeyCount", 0) == 0


def test_conflicting_hash_for_the_same_run_id_never_overwrites_the_first(consumer, envelope_bytes):
    from datetime import datetime, timezone
    from services.api.app.evaluation.delivery_models import SafeConfiguration, ExportEnvelope, SafeRun
    body, run_id = envelope_bytes
    pytest_run(consumer.process_message(body))
    other_safe_run = SafeRun(run_id=run_id, started_at=datetime.now(timezone.utc),
                             completed_at=datetime.now(timezone.utc), status="error",
                             configuration=SafeConfiguration(generation_model="fake-chat",
                                                                     embedding_model="fake-embed"))
    conflicting = ExportEnvelope(run_id=run_id, safe_run=other_safe_run,
                                 redaction_policy_version="restricted-synthetic-v1",
                                 completeness="metadata_only").canonical_bytes()
    outcome = pytest_run(consumer.process_message(conflicting))
    assert outcome == "conflict"
    original = consumer._client.get_object(Bucket="evidence-archive-test", Key=f"queue-archive/{run_id}.json")
    assert original["Body"].read() == body
    listing = consumer._client.list_objects_v2(Bucket="evidence-archive-test", Prefix="queue-archive-conflict/")
    assert listing["KeyCount"] == 1


def test_malformed_body_is_invalid_not_a_crash(consumer):
    outcome = pytest_run(consumer.process_message(b"not json"))
    assert outcome == "invalid"


def pytest_run(coro):
    import asyncio
    return asyncio.run(coro)
