"""Delivery worker contracts with finite in-memory repository/broker/clock fakes and Moto SQS.

The fake repository mirrors OutboxRepository's lease/attempt/backoff semantics so the worker's
own behavior is isolated; real-PostgreSQL equivalents run under RUN_EVAL_POSTGRES_TESTS=1 below
and in the Task 10 acceptance suite.
"""
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from services.api.app.evaluation.bounded_io import BoundedIO
from services.api.app.evaluation.delivery_models import (
    DeliveryReason, EventIdentity, ExportEnvelope, ManifestEntry, OutboxClaim, PayloadKind, PayloadRef, PayloadSlot,
    PublishReceipt,
)
from services.api.app.evaluation.outbox import EvidenceUnavailable, retry_delay
from services.api.app.evaluation.publisher import PublishError, RunEvidencePublisher
from services.evaluation_delivery.worker import DeliveryWorker
import consumer_fixture  # test-only module beside this file (pytest prepend import mode)

pytestmark = pytest.mark.filterwarnings('ignore:datetime.datetime.utcnow:DeprecationWarning:botocore')

REPO_ROOT = Path(__file__).parents[2]
SENTINEL = 'SENTINEL-WORKER-SECRET-7777'
START = datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc)


class StepClock:
    def __init__(self, now=START):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += timedelta(seconds=seconds)
        return self.now


@dataclass
class Row:
    identity: EventIdentity
    export_hash: str
    envelope: bytes
    expires_at: datetime
    state: str = 'pending'
    attempts: int = 0
    next_attempt_at: datetime = START
    lease_token: UUID | None = None
    lease_until: datetime | None = None
    acknowledgement_id: str | None = None
    error_class: str | None = None


class FakeRepo:
    """In-memory OutboxRepository double: same lease-token checks, attempt cap, and backoff.

    Unlike the real claim SQL it does not filter expired metadata, so the worker's own expiry
    guard (which also covers expiry between claim and send) is exercised directly.
    """

    def __init__(self, clock, *, max_attempts=8, base=1.0, cap=60.0):
        self.clock = clock
        self.max_attempts, self.base, self.cap = max_attempts, base, cap
        self.rows: list[Row] = []
        self.fail_next_ack = False  # Fake repository injection, not a production flag.
        self.fail_claims = 0
        self.steal_before_ack = False
        self.on_claim = None
        self.claim_calls = self.ack_calls = self.retry_calls = 0

    def add(self, envelope: ExportEnvelope, *, expires_at=None, export_hash=None, identity=None):
        self.rows.append(Row(
            identity=identity or EventIdentity(run_id=envelope.run_id),
            export_hash=export_hash or envelope.export_hash, envelope=envelope.canonical_bytes(),
            expires_at=expires_at or self.clock() + timedelta(days=90), next_attempt_at=self.clock()))
        return self.rows[-1]

    def _owned(self, claim):
        now = self.clock()
        for row in self.rows:
            if (row.identity == claim.identity and row.export_hash == claim.export_hash and row.state == 'leased'
                    and row.lease_token == claim.lease_token and row.lease_until > now):
                return row
        return None

    async def claim_batch(self, limit, lease_seconds):
        self.claim_calls += 1
        await asyncio.sleep(0)
        if self.fail_claims:
            self.fail_claims -= 1
            raise EvidenceUnavailable()
        now = self.clock()
        due = [r for r in self.rows if r.state in ('pending', 'leased') and r.next_attempt_at <= now
               and (r.lease_until is None or r.lease_until <= now)][:limit]
        claims = []
        for row in due:
            if row.attempts >= self.max_attempts:
                row.state, row.error_class, row.lease_token, row.lease_until = 'quarantined', 'retry_exhausted', None, None
                continue
            row.state, row.lease_token = 'leased', uuid4()
            row.lease_until, row.attempts = now + timedelta(seconds=lease_seconds), row.attempts + 1
            claims.append(OutboxClaim(identity=row.identity, export_hash=row.export_hash, envelope_bytes=row.envelope,
                                      expires_at=row.expires_at, lease_token=row.lease_token,
                                      lease_expires_at=row.lease_until, attempts=row.attempts))
        if self.on_claim:
            self.on_claim()
        return tuple(claims)

    async def mark_delivered(self, claim, receipt):
        self.ack_calls += 1
        await asyncio.sleep(0)
        if self.fail_next_ack:
            self.fail_next_ack = False
            raise EvidenceUnavailable()
        if self.steal_before_ack:
            for row in self.rows:
                row.lease_token = uuid4()
        row = self._owned(claim)
        if row is None:
            return False
        row.state, row.acknowledgement_id, row.lease_token, row.lease_until = 'delivered', receipt.acknowledgement_id, None, None
        return True

    async def schedule_retry(self, claim, error_class, *, permanent):
        self.retry_calls += 1
        await asyncio.sleep(0)
        row = self._owned(claim)
        if row is None:
            return False
        now = self.clock()
        row.lease_token = row.lease_until = None
        if permanent or row.attempts >= self.max_attempts:
            row.state = 'quarantined'
            row.error_class = error_class if permanent else 'retry_exhausted'
        else:
            row.state, row.error_class = 'pending', error_class
            row.next_attempt_at = now + timedelta(seconds=retry_delay(row.attempts, base=self.base, cap=self.cap,
                                                                      jitter=lambda: 1.0))
        return True


class FakeBroker:
    """Publisher double that records bodies and fails as scripted."""

    def __init__(self):
        self.bodies: list[bytes] = []
        self.failures: list[BaseException] = []
        self.hang: asyncio.Event | None = None
        self.closed = False

    async def publish(self, envelope_bytes, *, deadline):
        if self.hang is not None:
            await self.hang.wait()
        if self.failures:
            raise self.failures.pop(0)
        self.bodies.append(envelope_bytes)
        return PublishReceipt(acknowledgement_id=f'ack-{len(self.bodies)}')

    async def close(self, deadline):
        self.closed = True
        return 0


class MotoBroker:
    """A real RunEvidencePublisher on a Moto queue; `bodies` drains everything the queue received."""

    def __init__(self, client, url):
        self.client, self.url = client, url
        self._bodies: list[bytes] = []

    def publisher(self):
        return RunEvidencePublisher(self.client, queue_url=self.url, io=BoundedIO(2))

    @property
    def bodies(self):
        while True:
            messages = self.client.receive_message(QueueUrl=self.url, MaxNumberOfMessages=10,
                                                   VisibilityTimeout=300).get('Messages', [])
            if not messages:
                return self._bodies
            self._bodies.extend(m['Body'].encode('utf-8') for m in messages)


@pytest.fixture
def clock():
    return StepClock()


@pytest.fixture
def repo(clock):
    return FakeRepo(clock)


@pytest.fixture
def sqs_queue(aws_env):
    import boto3
    from moto import mock_aws
    with mock_aws():
        client = boto3.client('sqs', region_name='us-east-1')
        url = client.create_queue(QueueName='synthetic-eval-runs')['QueueUrl']
        try:
            yield client, url
        finally:
            client.close()


@pytest.fixture
def broker(sqs_queue):
    return MotoBroker(*sqs_queue)


@pytest.fixture
def worker(repo, broker, clock, envelope):
    repo.add(envelope)
    return DeliveryWorker(repo, broker.publisher(), clock)


def fake_worker(repo, clock, **kwargs):
    broker = FakeBroker()
    return DeliveryWorker(repo, broker, clock, **kwargs), broker


def other_envelope(envelope, **safe_run_updates):
    run_id = uuid4()
    safe_run = envelope.safe_run.model_copy(update={'run_id': run_id, **safe_run_updates})
    return ExportEnvelope(run_id=run_id, safe_run=safe_run, redaction_policy_version=envelope.redaction_policy_version,
                          completeness=envelope.completeness)


# --- acknowledgement-before-mark crash ------------------------------------------------

async def acknowledgement_loss_contract(worker, repo, broker, clock):
    repo.fail_next_ack = True  # Fake repository injection, not a production flag.
    await worker.run_once()
    clock.advance(61)
    await worker.run_once()
    assert broker.bodies[0] == broker.bodies[1]


def test_acknowledgement_loss_redelivers_identical_bytes(worker, repo, broker, clock):
    asyncio.run(acknowledgement_loss_contract(worker, repo, broker, clock))
    [row] = repo.rows
    assert broker.bodies == [row.envelope, row.envelope]
    assert row.state == 'delivered' and row.attempts == 2 and repo.retry_calls == 0


def test_acknowledgement_loss_is_absorbed_by_a_transactional_consumer(worker, repo, broker, clock, tmp_path):
    asyncio.run(acknowledgement_loss_contract(worker, repo, broker, clock))
    engine = consumer_engine(tmp_path)
    try:
        outcomes = [asyncio.run(consumer_fixture.accept(engine, body, now=clock())) for body in broker.bodies]
    finally:
        asyncio.run(engine.dispose())
    assert outcomes == ['accepted', 'duplicate']


def test_lost_acknowledgement_is_not_retried_before_the_lease_lapses(worker, repo, broker, clock):
    async def scenario():
        repo.fail_next_ack = True
        assert await worker.run_once() == 1
        clock.advance(59)
        assert await worker.run_once() == 0
    asyncio.run(scenario())
    assert len(broker.bodies) == 1 and repo.rows[0].state == 'leased'


# --- happy path and exact bytes -------------------------------------------------------

def test_delivers_exact_stored_bytes_and_records_the_acknowledgement(worker, repo, broker):
    assert asyncio.run(worker.run_once()) == 1
    [row] = repo.rows
    [body] = broker.bodies
    assert body == row.envelope
    assert row.state == 'delivered' and row.acknowledgement_id and row.error_class is None


def test_expired_payload_references_do_not_mutate_the_envelope(repo, clock, envelope):
    created = clock() - timedelta(days=45)
    slot = PayloadSlot(kind=PayloadKind.input, ordinal=1)
    ref = PayloadRef(bucket='synthetic-evidence-payloads',
                     key=f'evidence/test/synthetic-tenant/{envelope.run_id}/2026-01-15/input/1', version_id='v1',
                     tenant_partition='synthetic-tenant', slot=slot, byte_size=10, sha256='a' * 64,
                     policy_version=envelope.redaction_policy_version, created_at=created,
                     expires_at=created + timedelta(days=30))
    stored = ExportEnvelope(run_id=envelope.run_id, safe_run=envelope.safe_run,
                            redaction_policy_version=envelope.redaction_policy_version,
                            manifest=(ManifestEntry(slot=slot, state='stored', reference=ref),), completeness='complete')
    assert ref.expires_at < clock()
    row = repo.add(stored)
    original = bytes(row.envelope)
    worker, broker = fake_worker(repo, clock)
    asyncio.run(worker.run_once())
    assert broker.bodies == [original] and row.envelope == original and row.state == 'delivered'


# --- validation before send ---------------------------------------------------------------

@pytest.mark.parametrize('mismatch', ['hash', 'identity'])
def test_claim_that_disagrees_with_its_envelope_is_quarantined_unsent(repo, clock, envelope, mismatch):
    if mismatch == 'hash':
        row = repo.add(envelope, export_hash='0' * 64)
    else:
        row = repo.add(envelope, identity=EventIdentity(run_id=uuid4()))
    worker, broker = fake_worker(repo, clock)
    asyncio.run(worker.run_once())
    assert broker.bodies == []
    assert row.state == 'quarantined' and row.error_class == DeliveryReason.metadata_invalid.value


def test_corrupt_stored_bytes_are_quarantined_unsent(repo, clock, envelope):
    row = repo.add(envelope)
    row.envelope = row.envelope.replace(b'"success"', b'"error"', 1)
    worker, broker = fake_worker(repo, clock)
    asyncio.run(worker.run_once())
    assert broker.bodies == [] and row.state == 'quarantined' and row.error_class == 'metadata_invalid'


def test_oversized_envelope_is_quarantined_by_the_real_publisher(repo, clock, envelope, sqs_queue):
    client, url = sqs_queue
    row = repo.add(envelope)
    publisher = RunEvidencePublisher(client, queue_url=url, io=BoundedIO(1), max_body_bytes=len(row.envelope) - 1)
    asyncio.run(DeliveryWorker(repo, publisher, clock).run_once())
    assert MotoBroker(client, url).bodies == []
    assert row.state == 'quarantined' and row.error_class == DeliveryReason.metadata_too_large.value


def test_metadata_expiry_prevents_publish_even_after_a_prior_failure(repo, clock, envelope):
    row = repo.add(envelope, expires_at=clock() + timedelta(seconds=30))
    worker, broker = fake_worker(repo, clock)
    broker.failures.append(PublishError(DeliveryReason.broker_transient, permanent=False))

    async def scenario():
        await worker.run_once()
        assert row.state == 'pending' and row.attempts == 1
        clock.advance(31)
        await worker.run_once()
    asyncio.run(scenario())
    assert broker.bodies == [] and row.attempts == 2
    assert row.state == 'quarantined' and row.error_class == DeliveryReason.expired.value
    assert row.envelope == envelope.canonical_bytes()


def test_expiry_during_the_claim_window_prevents_publish(repo, clock, envelope):
    row = repo.add(envelope, expires_at=clock() + timedelta(seconds=5))
    repo.on_claim = lambda: clock.advance(10)
    worker, broker = fake_worker(repo, clock)
    asyncio.run(worker.run_once())
    assert broker.bodies == [] and row.state == 'quarantined' and row.error_class == 'expired'


def test_lease_too_short_to_send_and_acknowledge_is_left_to_lapse(repo, clock, envelope):
    row = repo.add(envelope)
    repo.on_claim = lambda: clock.advance(58)
    worker, broker = fake_worker(repo, clock)
    asyncio.run(worker.run_once())
    assert broker.bodies == [] and repo.ack_calls == repo.retry_calls == 0 and row.state == 'leased'


# --- retry, backoff, quarantine ------------------------------------------------------------

def test_transient_failure_backs_off_and_resends_identical_bytes(repo, clock, envelope):
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)
    broker.failures.append(PublishError(DeliveryReason.broker_transient, permanent=False))

    async def scenario():
        await worker.run_once()
        assert row.state == 'pending' and row.error_class == 'broker_transient'
        assert row.next_attempt_at == clock() + timedelta(seconds=1)
        assert await worker.run_once() == 0  # not due yet
        clock.advance(1)
        assert await worker.run_once() == 1
    asyncio.run(scenario())
    assert broker.bodies == [row.envelope] and row.state == 'delivered' and row.attempts == 2


def test_transient_failures_exhaust_after_eight_attempts(repo, clock, envelope):
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)
    publishes = []

    async def always_fail(envelope_bytes, *, deadline):
        publishes.append(envelope_bytes)
        raise PublishError(DeliveryReason.broker_transient, permanent=False)
    broker.publish = always_fail

    async def scenario():
        for _ in range(12):
            await worker.run_once()
            clock.advance(61)
    asyncio.run(scenario())
    assert len(publishes) == 8 and set(publishes) == {row.envelope}
    assert row.state == 'quarantined' and row.error_class == DeliveryReason.retry_exhausted.value


def test_backoff_delays_are_capped_exponential(repo, clock, envelope):
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)
    broker.failures.extend(PublishError(DeliveryReason.broker_transient, permanent=False) for _ in range(7))
    delays = []

    async def scenario():
        for _ in range(7):
            await worker.run_once()
            delays.append((row.next_attempt_at - clock()).total_seconds())
            clock.now = row.next_attempt_at
    asyncio.run(scenario())
    assert delays == [1, 2, 4, 8, 16, 32, 60]


def test_denied_broker_quarantines_immediately(repo, clock, envelope):
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)
    broker.failures.append(PublishError(DeliveryReason.broker_denied, permanent=True))
    asyncio.run(worker.run_once())
    assert row.state == 'quarantined' and row.error_class == 'broker_denied' and row.attempts == 1


def test_denied_real_broker_quarantines(repo, clock, envelope, sqs_queue):
    client, url = sqs_queue
    row = repo.add(envelope)
    publisher = RunEvidencePublisher(client, queue_url=url.rsplit('/', 1)[0] + '/missing', io=BoundedIO(1))
    asyncio.run(DeliveryWorker(repo, publisher, clock).run_once())
    assert row.state == 'quarantined' and row.error_class == 'broker_denied'


def test_unexpected_publisher_errors_are_transient_and_content_free(repo, clock, envelope, caplog):
    caplog.set_level(logging.DEBUG, logger='services')
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)
    broker.failures.append(RuntimeError(SENTINEL))
    asyncio.run(worker.run_once())
    assert row.state == 'pending' and row.error_class == 'broker_transient'
    assert SENTINEL not in caplog.text and str(envelope.run_id) not in caplog.text
    assert envelope.canonical_bytes().decode() not in caplog.text


def test_stale_lease_on_acknowledgement_authorizes_no_second_write(repo, clock, envelope):
    repo.add(envelope)
    repo.steal_before_ack = True
    worker, broker = fake_worker(repo, clock)
    asyncio.run(worker.run_once())
    assert len(broker.bodies) == 1 and repo.ack_calls == 1 and repo.retry_calls == 0


def test_one_run_once_sends_each_claim_at_most_once(repo, clock, envelope):
    repo.add(envelope)
    worker, broker = fake_worker(repo, clock)
    calls = []

    async def failing(envelope_bytes, *, deadline):
        calls.append(deadline)
        raise PublishError(DeliveryReason.broker_transient, permanent=False)
    broker.publish = failing
    asyncio.run(worker.run_once())
    assert len(calls) == 1 and repo.claim_calls == 1


def test_publish_deadline_stays_inside_the_lease(repo, clock, envelope):
    repo.add(envelope)
    deadlines = []
    worker, broker = fake_worker(repo, clock, monotonic=lambda: 100.0, send_timeout_seconds=500,
                                 lease_seconds=20, ack_reserve_seconds=5)

    async def record(envelope_bytes, *, deadline):
        deadlines.append(deadline)
        return PublishReceipt(acknowledgement_id='ack')
    broker.publish = record
    asyncio.run(worker.run_once())
    assert deadlines == [115.0]


# --- concurrency -----------------------------------------------------------------------

def test_two_workers_never_double_publish(repo, clock, envelope, broker):
    envelopes = [other_envelope(envelope) for _ in range(7)]
    for item in envelopes:
        repo.add(item)
    first = DeliveryWorker(repo, broker.publisher(), clock, batch=3)
    second = DeliveryWorker(repo, broker.publisher(), clock, batch=3)

    async def scenario():
        total = 0
        for _ in range(4):
            total += sum(await asyncio.gather(first.run_once(), second.run_once()))
        return total
    assert asyncio.run(scenario()) == 7
    assert sorted(broker.bodies) == sorted(e.canonical_bytes() for e in envelopes)
    assert all(row.state == 'delivered' and row.attempts == 1 for row in repo.rows)


def test_send_concurrency_is_bounded(repo, clock, envelope):
    for _ in range(6):
        repo.add(other_envelope(envelope))
    worker, broker = fake_worker(repo, clock, send_workers=2, batch=6)
    active, peak = 0, 0

    async def slow(envelope_bytes, *, deadline):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return PublishReceipt(acknowledgement_id='ack')
    broker.publish = slow
    assert asyncio.run(worker.run_once()) == 6
    assert peak == 2


# --- run loop, shutdown ---------------------------------------------------------------------

def test_run_survives_database_outages_and_stops_on_request(repo, clock, envelope):
    repo.add(envelope)
    repo.fail_claims = 2
    worker, broker = fake_worker(repo, clock, poll_seconds=0.01)

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        for _ in range(200):
            if broker.bodies:
                break
            await asyncio.sleep(0.01)
        stop.set()
        await asyncio.wait_for(task, 1)
    asyncio.run(scenario())
    assert len(broker.bodies) == 1 and repo.claim_calls >= 3


def test_close_stops_claims_and_bounds_draining(repo, clock, envelope):
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)

    async def scenario():
        broker.hang = asyncio.Event()  # never set: an in-flight send that does not finish
        stop = asyncio.Event()
        task = asyncio.create_task(worker.run(stop))
        while repo.claim_calls == 0:
            await asyncio.sleep(0.001)
        await asyncio.sleep(0.01)
        started = time.monotonic()
        residue = await worker.close(time.monotonic() + 0.1)
        assert time.monotonic() - started < 1
        await asyncio.wait_for(task, 1)
        calls = repo.claim_calls
        assert await worker.run_once() == 0 and repo.claim_calls == calls
        return residue
    assert asyncio.run(scenario()) == 0
    assert broker.closed and broker.bodies == []
    assert row.state == 'leased' and repo.ack_calls == repo.retry_calls == 0  # lease lapses; redelivered later


def test_close_waits_for_in_flight_sends_that_finish_in_time(repo, clock, envelope):
    row = repo.add(envelope)
    worker, broker = fake_worker(repo, clock)

    async def scenario():
        broker.hang = asyncio.Event()
        once = asyncio.create_task(worker.run_once())
        while repo.claim_calls == 0:
            await asyncio.sleep(0.001)
        asyncio.get_running_loop().call_later(0.05, broker.hang.set)
        await worker.close(time.monotonic() + 2)
        return await once
    assert asyncio.run(scenario()) == 1
    assert row.state == 'delivered'


# --- CLI ------------------------------------------------------------------------------------

def cli_env(tmp_path, **overrides):
    env = {k: v for k, v in os.environ.items() if not k.startswith(('EVAL_', 'AWS_'))}
    env.update({
        'PYTHONPATH': str(REPO_ROOT), 'DATABASE_URL': f'postgresql+asyncpg://u:{SENTINEL}@127.0.0.1:1/none',
        'REDIS_URL': 'redis://127.0.0.1:1/0', 'NEO4J_PASSWORD': 'synthetic', 'S3_BUCKET_NAME': 'synthetic',
        'JWT_SECRET_KEY': 'synthetic-jwt', 'AWS_ACCESS_KEY_ID': 'synthetic', 'AWS_SECRET_ACCESS_KEY': 'synthetic',
        'AWS_EC2_METADATA_DISABLED': 'true', 'EVAL_EVIDENCE_ENABLED': 'true', 'EVAL_IDENTITY_HASH_KEY': 'synthetic-id',
        'EVAL_QUEUE_URL': 'http://127.0.0.1:1/123456789012/eval-runs', 'EVAL_SHUTDOWN_DRAIN_SECONDS': '2',
    })
    env.update(overrides)
    return env


def test_cli_refuses_to_start_when_delivery_is_disabled(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'services.evaluation_delivery.worker'], cwd=tmp_path,
                            env=cli_env(tmp_path, EVAL_EVIDENCE_ENABLED='false'), capture_output=True, text=True,
                            timeout=60)
    assert result.returncode == 2
    assert 'disabled' in result.stderr and SENTINEL not in result.stderr + result.stdout


def test_cli_rejects_invalid_configuration_without_echoing_it(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'services.evaluation_delivery.worker'], cwd=tmp_path,
                            env=cli_env(tmp_path, EVAL_IDENTITY_HASH_KEY='', EVAL_QUEUE_URL=SENTINEL),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 2 and SENTINEL not in result.stderr + result.stdout


@pytest.mark.parametrize('signum', [signal.SIGTERM, signal.SIGINT])
def test_cli_stops_claiming_and_exits_cleanly_on_signal(tmp_path, signum):
    process = subprocess.Popen([sys.executable, '-m', 'services.evaluation_delivery.worker'], cwd=tmp_path,
                               env=cli_env(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 30
        line = ''
        while 'started' not in line and time.monotonic() < deadline:
            line = process.stderr.readline()
        assert 'started' in line
        time.sleep(0.3)  # let it hit the unreachable database at least once
        process.send_signal(signum)
        out, err = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
    assert process.returncode == 0
    assert 'stopped' in err and SENTINEL not in out + err + line


# --- consumer fixture ---------------------------------------------------------------------

def consumer_engine(tmp_path):
    from sqlalchemy.ext.asyncio import create_async_engine
    return create_async_engine(f'sqlite+aiosqlite:///{tmp_path / "consumer.db"}')


def conflicting(envelope):
    safe_run = envelope.safe_run.model_copy(update={'error_class': 'OtherError'})
    return ExportEnvelope(run_id=envelope.run_id, safe_run=safe_run,
                          redaction_policy_version=envelope.redaction_policy_version,
                          completeness=envelope.completeness)


def consume(engine, *bodies, now):
    async def scenario():
        return [await consumer_fixture.accept(engine, body, now=now) for body in bodies]
    return asyncio.run(scenario())


def test_consumer_accepts_identical_redelivery_once_and_rejects_conflicts(tmp_path, envelope):
    engine = consumer_engine(tmp_path)
    now = envelope.safe_run.completed_at + timedelta(days=1)
    data = envelope.canonical_bytes()
    try:
        assert consume(engine, data, data, conflicting(envelope).canonical_bytes(), data, now=now) == [
            'accepted', 'duplicate', 'conflict', 'duplicate']
        assert asyncio.run(consumer_fixture.receipts(engine)) == [
            (str(envelope.run_id), envelope.export_hash)]
    finally:
        asyncio.run(engine.dispose())


def test_consumer_rejects_expired_metadata_without_recording_it(tmp_path, envelope):
    engine = consumer_engine(tmp_path)
    now = envelope.safe_run.completed_at + timedelta(days=90)
    try:
        assert consume(engine, envelope.canonical_bytes(), now=now) == ['expired']
        assert asyncio.run(consumer_fixture.receipts(engine)) == []
    finally:
        asyncio.run(engine.dispose())


def test_consumer_rejects_malformed_envelopes(tmp_path, envelope):
    engine = consumer_engine(tmp_path)
    try:
        with pytest.raises(ValueError):
            consume(engine, envelope.canonical_bytes(exclude_hash=True), now=START)
    finally:
        asyncio.run(engine.dispose())


def test_consumer_dedup_state_is_independent_of_the_producer(tmp_path, envelope):
    # A fresh consumer database has no knowledge of producer delivery status.
    now = envelope.safe_run.completed_at + timedelta(days=1)
    (tmp_path / 'a').mkdir()
    (tmp_path / 'b').mkdir()
    first = consumer_engine(tmp_path / 'a')
    second = consumer_engine(tmp_path / 'b')
    try:
        assert consume(first, envelope.canonical_bytes(), now=now) == ['accepted']
        assert consume(second, envelope.canonical_bytes(), now=now) == ['accepted']
    finally:
        asyncio.run(first.dispose())
        asyncio.run(second.dispose())


# --- opt-in real PostgreSQL ---------------------------------------------------------------

def test_live_consumer_is_transactional_under_concurrent_redelivery(pg_engine, envelope):
    now = envelope.safe_run.completed_at + timedelta(days=1)
    data = envelope.canonical_bytes()

    async def scenario():
        assert await consumer_fixture.receipts(pg_engine) == []  # create the table before racing
        return await asyncio.gather(*(consumer_fixture.accept(pg_engine, data, now=now) for _ in range(5)),
                                    consumer_fixture.accept(pg_engine, conflicting(envelope).canonical_bytes(), now=now))
    *identical, rival = asyncio.run(scenario())
    # Exactly one hash wins the identity, whichever transaction commits first.
    if rival == 'accepted':
        assert identical == ['conflict'] * 5
    else:
        assert rival == 'conflict' and sorted(identical) == ['accepted'] + ['duplicate'] * 4


def test_live_repository_acknowledgement_loss_redelivers_identical_bytes(repo_factory, clock, envelope, broker):
    clock.now = envelope.safe_run.completed_at + timedelta(minutes=1)
    real = repo_factory()

    class LosesFirstAck:
        def __init__(self, inner):
            self.inner, self.lost = inner, False

        def __getattr__(self, name):
            return getattr(self.inner, name)

        async def mark_delivered(self, claim, receipt):
            if not self.lost:
                self.lost = True
                raise EvidenceUnavailable()
            return await self.inner.mark_delivered(claim, receipt)
    assert asyncio.run(real.enqueue(envelope, None)).status == 'committed'
    worker = DeliveryWorker(LosesFirstAck(real), broker.publisher(), clock)

    async def scenario():
        await worker.run_once()
        clock.advance(61)
        await worker.run_once()
        return await real.claim_batch(10, 60)
    assert asyncio.run(scenario()) == ()
    assert broker.bodies == [envelope.canonical_bytes()] * 2
