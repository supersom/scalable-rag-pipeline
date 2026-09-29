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


def test_put_includes_the_bucket_kms_key_when_configured(envelope_bytes):
    body, run_id = envelope_bytes
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="evidence-archive-test")
        registry = CollectorRegistry()
        consumer = DrainConsumer(client, bucket="evidence-archive-test",
                                 kms_key_id="arn:aws:kms:us-east-1:111122223333:key/fake-key-id",
                                 registry=registry)
        outcome = pytest_run(consumer.process_message(body))
        assert outcome == "archived"
        obj = client.get_object(Bucket="evidence-archive-test", Key=f"queue-archive/{run_id}.json")
        assert obj["ServerSideEncryption"] == "aws:kms"
        assert obj["SSEKMSKeyId"].endswith("fake-key-id")


def test_put_omits_sse_kms_key_id_when_not_configured():
    class SpyS3:
        def __init__(self):
            self.calls = []
            self.exceptions = type("Exceptions", (), {"NoSuchKey": type("NoSuchKey", (Exception,), {})})()

        def get_object(self, **_kwargs):
            raise self.exceptions.NoSuchKey()

        def put_object(self, **kwargs):
            self.calls.append(kwargs)

    spy = SpyS3()
    registry = CollectorRegistry()
    consumer = DrainConsumer(spy, bucket="evidence-archive-test", registry=registry)
    consumer._put("queue-archive/some-run.json", b"payload")
    assert len(spy.calls) == 1
    assert "SSEKMSKeyId" not in spy.calls[0]
    assert spy.calls[0]["ServerSideEncryption"] == "aws:kms"


def test_poll_loop_survives_a_malformed_message_and_still_processes_the_next(consumer, envelope_bytes):
    from services.evidence_drain_consumer.consumer import DrainConsumer
    body, run_id = envelope_bytes

    class FakeSQS:
        def __init__(self, messages):
            self.messages = list(messages)
            self.deleted = []

        def receive_message(self, **_kwargs):
            if not self.messages:
                return {}
            return {"Messages": [self.messages.pop(0)]}

        def delete_message(self, *, QueueUrl, ReceiptHandle):
            self.deleted.append(ReceiptHandle)

    sqs = FakeSQS([
        {"Body": "not json", "ReceiptHandle": "r1"},
        {"Body": body.decode(), "ReceiptHandle": "r2"},
    ])
    outcomes = pytest_run(_drain_two(consumer, sqs))
    assert outcomes == ["invalid", "archived"]
    assert sqs.deleted == ["r2"]  # the malformed message is left for the queue's own redrive, not deleted


async def _drain_two(consumer, sqs):
    from services.evidence_drain_consumer.main import poll_once
    outcomes = []
    for _ in range(2):
        outcome = await poll_once(consumer, sqs, queue_url="http://queue")
        outcomes.append(outcome)
    return outcomes


def test_missing_configuration_is_a_content_free_exit_2(monkeypatch, capsys):
    from services.evidence_drain_consumer import main
    monkeypatch.delenv("EVIDENCE_QUEUE_URL", raising=False)
    monkeypatch.delenv("EVIDENCE_ARCHIVE_BUCKET", raising=False)
    monkeypatch.delenv("EVIDENCE_ARCHIVE_KMS_KEY_ID", raising=False)
    code = main.main()
    out, err = capsys.readouterr()
    assert code == main.EXIT_CONFIGURATION
    assert "EVIDENCE_QUEUE_URL" not in err  # fixed message only, no echoed env var names/values


def test_missing_kms_key_id_alone_is_also_exit_2(monkeypatch, capsys):
    from services.evidence_drain_consumer import main
    monkeypatch.setenv("EVIDENCE_QUEUE_URL", "http://queue.example/000000000000/evidence")
    monkeypatch.setenv("EVIDENCE_ARCHIVE_BUCKET", "evidence-archive")
    monkeypatch.delenv("EVIDENCE_ARCHIVE_KMS_KEY_ID", raising=False)
    code = main.main()
    out, err = capsys.readouterr()
    assert code == main.EXIT_CONFIGURATION
    assert "EVIDENCE_ARCHIVE_KMS_KEY_ID" not in err  # fixed message only, no echoed env var names/values


def pytest_run(coro):
    import asyncio
    return asyncio.run(coro)
