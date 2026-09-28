"""Terminal admission runtime: synchronous non-durable submit, bounded background upload/commit."""
import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from prometheus_client import CollectorRegistry

from services.api.app.evaluation.delivery_config import DeliveryConfig
from services.api.app.evaluation.delivery_models import DeliveryReceipt, PayloadRef, PayloadSlot
from services.api.app.evaluation.outbox import EvidenceUnavailable
from services.api.app.evaluation.payloads import PayloadStorageError

LITERAL = 'Contact alice@example.test; token=fixture-secret'


def content_config(**overrides):
    values = dict(enabled=True, capture_content=True, environment='local',
                  database_url='postgresql://local/test', queue_url='http://localhost/queue',
                  identity_key='dedicated-synthetic-evidence-key', content_policy='restricted-synthetic-v1',
                  fixture_manifest='local-fixture.json', fixture_case='literal', bucket='local-bucket',
                  kms_key_id='local-key', s3_endpoint_url='http://localhost:5000')
    values.update(overrides)
    return DeliveryConfig(**values)


def metadata_config(**overrides):
    values = dict(enabled=True, database_url='postgresql://local/test', queue_url='http://localhost/queue',
                  identity_key='dedicated-synthetic-evidence-key')
    values.update(overrides)
    return DeliveryConfig(**values)


class RepoSpy:
    """Records calls; each call can be held open by an asyncio.Event to prove non-blocking admission."""

    def __init__(self):
        self.calls = []
        self.hold = None
        self.intent_error = None
        self.enqueue_receipt = None
        self.enqueued = []

    async def begin_upload(self, run_id, namespace, lease_seconds):
        from services.api.app.evaluation.delivery_models import UploadIntent
        self.calls.append(('begin_upload', namespace, lease_seconds))
        if self.hold is not None:
            await self.hold.wait()
        if self.intent_error is not None:
            raise self.intent_error
        return UploadIntent(run_id=run_id, namespace=namespace, lease_token=uuid4(),
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds))

    async def enqueue(self, envelope, intent):
        self.calls.append(('enqueue', intent))
        if self.hold is not None:
            await self.hold.wait()
        self.enqueued.append((envelope, intent))
        if self.enqueue_receipt is not None:
            return self.enqueue_receipt
        return DeliveryReceipt(status='committed', export_hash=envelope.export_hash)


class StoreSpy:
    def __init__(self):
        self.calls = []
        self.hold = None
        self.fail_slots = set()
        self.closed = []

    def namespace(self, tenant_partition, run_id):
        return f'evidence/local/{tenant_partition}/{run_id}/'

    async def put(self, run_id, tenant_partition, payload, *, deadline):
        self.calls.append(('put', payload.slot))
        if self.hold is not None:
            await self.hold.wait()
        if payload.slot in self.fail_slots:
            raise PayloadStorageError('failed')
        created = datetime.now(timezone.utc)
        import hashlib
        return PayloadRef(bucket='local-bucket',
                          key=f'{self.namespace(tenant_partition, run_id)}{created.date()}/{payload.slot.kind.value}/1',
                          version_id='v1', tenant_partition=tenant_partition, slot=payload.slot,
                          byte_size=len(payload.data), sha256=hashlib.sha256(payload.data).hexdigest(),
                          policy_version=payload.policy_version, created_at=created,
                          expires_at=created + timedelta(days=30))

    async def close(self, deadline):
        self.closed.append(deadline)
        return 0


@pytest.fixture
def repo_spy():
    return RepoSpy()


@pytest.fixture
def store_spy():
    return StoreSpy()


@pytest.fixture
def registry():
    return CollectorRegistry()


def make_runtime(config, policy, repo, store, registry=None):
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    from services.api.app.evaluation.runtime import EvidenceRuntime
    metrics = registry if isinstance(registry, DeliveryMetrics) else \
        DeliveryMetrics(registry) if registry is not None else None
    return EvidenceRuntime(config, policy, repo, store, metrics=metrics)


def capture_run(runtime, terminal_event, *, output=True):
    """A CapturedRun built through the real Task 6 session detach."""
    session = runtime.open_capture(terminal_event.run_id, 'synthetic-tenant')
    assert session is not None
    session.record(PayloadSlot(kind='input', ordinal=1), {'text': LITERAL}, session.source(LITERAL))
    if output:
        session.record(PayloadSlot(kind='output', ordinal=1), {'text': LITERAL}, session.source(LITERAL))
    return session.detach(terminal_event)


def sample(registry, outcome, component='runtime', reason='none'):
    return registry.get_sample_value('evidence_delivery_events_total',
                                     {'component': component, 'outcome': outcome, 'reason': reason}) or 0


@pytest.fixture
def runtime(policy, repo_spy, store_spy, registry):
    return make_runtime(content_config(), policy, repo_spy, store_spy, registry)


@pytest.fixture
def captured_run(runtime, terminal_event):
    return capture_run(runtime, terminal_event)


def test_submit_has_no_network_wait(runtime, captured_run, repo_spy, store_spy):
    async def run():
        await runtime.start()
        receipt = runtime.submit(captured_run)
        assert receipt.status == 'admitted'
        assert repo_spy.calls == store_spy.calls == []
        await runtime.close(time.monotonic() + 1)
    asyncio.run(run())


def test_admitted_is_not_committed_until_the_transaction_returns(runtime, captured_run, repo_spy, store_spy):
    async def run():
        repo_spy.hold = asyncio.Event()
        await runtime.start()
        assert runtime.submit(captured_run).status == 'admitted'
        assert runtime.pending_count == 1 and runtime.pending_bytes > 0
        for _ in range(20):
            await asyncio.sleep(0)
        # Blocked in the intent transaction: nothing uploaded, nothing committed, still pending.
        assert [c[0] for c in repo_spy.calls] == ['begin_upload'] and store_spy.calls == []
        assert runtime.pending_count == 1
        repo_spy.hold.set()
        await runtime.close(time.monotonic() + 2)
        assert [c[0] for c in repo_spy.calls] == ['begin_upload', 'enqueue']
        assert runtime.pending_count == runtime.pending_bytes == 0
    asyncio.run(run())


def test_process_one_commits_stored_references_by_slot(runtime, captured_run, repo_spy, store_spy):
    receipt = asyncio.run(runtime.process_one(captured_run))
    assert receipt.status == 'committed'
    envelope, intent = repo_spy.enqueued[0]
    partition = captured_run.prepared.safe_run.tenant_partition
    assert partition.startswith('hmac-sha256:') and 'synthetic-tenant' not in intent.namespace
    assert intent.namespace == store_spy.namespace(partition, captured_run.prepared.safe_run.run_id)
    assert repo_spy.calls[0][2] == runtime.config.intent_lease_seconds
    stored = {e.slot.kind.value: e for e in envelope.manifest if e.state == 'stored'}
    assert set(stored) == {'input', 'output'}
    assert all(e.reference.slot == e.slot and e.reference.key.startswith(intent.namespace) for e in stored.values())
    assert envelope.completeness == 'complete'


def test_partial_upload_keeps_successful_references(runtime, captured_run, repo_spy, store_spy):
    store_spy.fail_slots = {PayloadSlot(kind='output', ordinal=1)}
    receipt = asyncio.run(runtime.process_one(captured_run))
    assert receipt.status == 'committed'
    envelope, _ = repo_spy.enqueued[0]
    states = {e.slot.kind.value: (e.state, e.reason) for e in envelope.manifest}
    assert states == {'input': ('stored', None), 'output': ('unavailable', 'upload_failed')}
    assert envelope.completeness == 'partial'


def test_failed_intent_skips_uploads_and_commits_metadata_only(runtime, captured_run, repo_spy, store_spy, registry):
    repo_spy.intent_error = EvidenceUnavailable()
    receipt = asyncio.run(runtime.process_one(captured_run))
    assert receipt.status == 'committed'
    assert store_spy.calls == []
    envelope, intent = repo_spy.enqueued[0]
    assert intent is None
    assert {e.state for e in envelope.manifest} == {'unavailable'}
    assert {e.reason for e in envelope.manifest} == {'upload_failed'}
    assert sample(registry, 'failed', component='upload', reason='intent_failed') == 1


def test_hung_uploads_fall_back_to_metadata_within_the_reserved_deadline(policy, repo_spy, store_spy, terminal_event):
    config = content_config(deadline_seconds=0.6, commit_reserve_seconds=0.3)
    runtime = make_runtime(config, policy, repo_spy, store_spy)
    captured = capture_run(runtime, terminal_event)
    store_spy.hold = asyncio.Event()  # never released: every upload hangs

    async def run():
        started = time.monotonic()
        receipt = await runtime.process_one(captured)
        return receipt, time.monotonic() - started
    receipt, elapsed = asyncio.run(run())
    assert receipt.status == 'committed'
    assert elapsed < config.deadline_seconds
    envelope, intent = repo_spy.enqueued[0]
    assert intent is not None  # the open intent is closed by the metadata commit
    assert {e.reason for e in envelope.manifest} == {'upload_timeout'}
    assert len(store_spy.calls) == 1  # later payloads are not attempted once the content deadline passed


def test_expired_admission_is_denied_before_any_commit(runtime, captured_run, repo_spy, store_spy):
    async def run():
        return await runtime.process_one(captured_run, admitted_at=time.monotonic() - 60)
    receipt = asyncio.run(run())
    assert (receipt.status, receipt.reason) == ('failed', 'deadline_expired')
    assert repo_spy.enqueued == [] and store_spy.calls == []


def test_hung_commit_is_bounded_by_the_deadline(policy, repo_spy, store_spy, terminal_event):
    config = metadata_config(deadline_seconds=0.4, commit_reserve_seconds=0.2)
    runtime = make_runtime(config, policy, repo_spy, store_spy)
    repo_spy.hold = asyncio.Event()
    captured = runtime.metadata_run(terminal_event)
    receipt = asyncio.run(runtime.process_one(captured))
    assert (receipt.status, receipt.reason) == ('failed', 'deadline_expired')


def test_commit_failure_leaves_orphans_for_cleanup_and_reports_failed(runtime, captured_run, repo_spy, store_spy,
                                                                      registry):
    repo_spy.enqueue_receipt = DeliveryReceipt(status='failed', reason='database_unavailable')
    receipt = asyncio.run(runtime.process_one(captured_run))
    assert (receipt.status, receipt.reason) == ('failed', 'database_unavailable')
    assert len(store_spy.calls) == 2  # uploaded objects stay for intent-expiry orphan cleanup, never deleted inline
    assert sample(registry, 'failed', reason='database_unavailable') == 1


def test_repository_exceptions_become_fixed_reasons(runtime, captured_run, repo_spy):
    async def boom(envelope, intent):
        raise RuntimeError('private secret detail')
    repo_spy.enqueue = boom
    receipt = asyncio.run(runtime.process_one(captured_run))
    assert (receipt.status, receipt.reason) == ('failed', 'database_unavailable')
    assert 'private' not in receipt.model_dump_json()


def test_oversized_metadata_envelope_is_a_measured_loss(policy, repo_spy, store_spy, terminal_event, registry):
    runtime = make_runtime(metadata_config(queue_body_bytes=64), policy, repo_spy, store_spy, registry)
    receipt = asyncio.run(runtime.process_one(runtime.metadata_run(terminal_event)))
    assert (receipt.status, receipt.reason) == ('failed', 'metadata_too_large')
    assert repo_spy.calls == []
    assert sample(registry, 'failed', reason='metadata_too_large') == 1


def test_pending_count_overflow_is_dropped_with_fixed_reason(policy, repo_spy, store_spy, terminal_event, registry):
    runtime = make_runtime(metadata_config(pending_runs=2), policy, repo_spy, store_spy, registry)

    async def run():
        await runtime.start()
        receipts = [runtime.submit(runtime.metadata_run(terminal_event)) for _ in range(3)]
        assert [r.status for r in receipts] == ['admitted', 'admitted', 'dropped']
        assert receipts[2].reason == 'pending_capacity'
        assert runtime.pending_count == 2
        await runtime.close(time.monotonic() + 2)
    asyncio.run(run())
    assert sample(registry, 'dropped', component='admission', reason='pending_capacity') == 1
    assert sample(registry, 'admitted', component='admission') == 2


def test_pending_bytes_overflow_releases_nothing_it_did_not_reserve(policy, repo_spy, store_spy, terminal_event):
    probe = capture_run(make_runtime(content_config(), policy, repo_spy, store_spy), terminal_event)
    size = sum(len(p.data) for p in probe.prepared.payloads)
    limit = size + size // 2  # room for one run, not two
    runtime = make_runtime(content_config(payload_bytes=limit, run_bytes=limit, pending_bytes=limit),
                           policy, repo_spy, store_spy)
    repo_spy.hold = asyncio.Event()

    async def run():
        await runtime.start()
        a = runtime.submit(capture_run(runtime, terminal_event))
        b = runtime.submit(capture_run(runtime, terminal_event.model_copy(update={'run_id': str(uuid4())})))
        assert (a.status, b.status, b.reason) == ('admitted', 'dropped', 'pending_capacity')
        assert runtime.pending_bytes == size and runtime.pending_count == 1
        repo_spy.hold.set()
        await runtime.close(time.monotonic() + 2)
        assert runtime.pending_bytes == runtime.pending_count == 0
    asyncio.run(run())


def test_buffers_are_released_after_every_outcome(runtime, repo_spy, store_spy, terminal_event):
    repo_spy.intent_error = EvidenceUnavailable()
    repo_spy.enqueue_receipt = DeliveryReceipt(status='failed', reason='database_unavailable')

    async def run():
        await runtime.start()
        runtime.submit(capture_run(runtime, terminal_event))
        await runtime.close(time.monotonic() + 2)
    asyncio.run(run())
    assert runtime.pending_count == runtime.pending_bytes == 0
    assert runtime._pool.active_runs == runtime._pool.resident_bytes == 0


def test_submit_after_close_is_dropped_as_stopped(runtime, captured_run, registry):
    async def run():
        await runtime.start()
        await runtime.close(time.monotonic() + 1)
        return runtime.submit(captured_run)
    receipt = asyncio.run(run())
    assert (receipt.status, receipt.reason) == ('dropped', 'runtime_stopped')
    assert runtime.open_capture(uuid4(), 'synthetic-tenant') is None


def test_shutdown_with_hung_io_is_bounded_and_counts_dropped_work(policy, repo_spy, store_spy, terminal_event,
                                                                  registry):
    runtime = make_runtime(metadata_config(), policy, repo_spy, store_spy, registry)
    repo_spy.hold = asyncio.Event()

    async def run():
        await runtime.start()
        for _ in range(5):
            assert runtime.submit(runtime.metadata_run(terminal_event)).status == 'admitted'
        await asyncio.sleep(0.01)
        started = time.monotonic()
        await runtime.close(time.monotonic() + 0.2)
        assert time.monotonic() - started < 1
        assert all(task.done() for task in runtime._workers)
    asyncio.run(run())
    assert runtime.pending_count == runtime.pending_bytes == 0
    assert sample(registry, 'dropped', reason='runtime_stopped') == 5
    assert store_spy.closed  # the payload store's own bounded shutdown ran


def test_fixed_task_and_sdk_thread_counts_under_hung_uploads(policy, repo_spy, terminal_event):
    from services.api.app.evaluation.bounded_io import BoundedIO
    from services.api.app.evaluation.payloads import S3PayloadStore
    release = threading.Event()
    entered = []

    class HungClient:
        def put_object(self, **kwargs):
            entered.append(threading.current_thread().name)
            release.wait(5)
            return {'VersionId': 'v1', 'ServerSideEncryption': 'aws:kms'}

    config = content_config(deadline_seconds=0.5, commit_reserve_seconds=0.25)
    store = S3PayloadStore(HungClient(), bucket='local-bucket', kms_key_id='local-key', environment='local',
                           io=BoundedIO(config.s3_workers, thread_name_prefix='evidence-s3-test'))
    runtime = make_runtime(config, policy, repo_spy, store)

    async def run():
        before = len(asyncio.all_tasks())
        await runtime.start()
        assert len(asyncio.all_tasks()) - before == config.runtime_workers == 2
        for _ in range(6):
            event = terminal_event.model_copy(update={'run_id': str(uuid4())})
            runtime.submit(capture_run(runtime, event))
        await asyncio.sleep(0.3)
        assert len(asyncio.all_tasks()) - before == 2
        s3_threads = [t for t in threading.enumerate() if t.name.startswith('evidence-s3-test')]
        assert len(s3_threads) <= config.s3_workers and store._io.in_flight <= config.s3_workers
        await runtime.close(time.monotonic() + 0.3)
    try:
        asyncio.run(run())
    finally:
        release.set()
    assert len(set(entered)) <= config.s3_workers


def test_disabled_runtime_allocates_nothing(policy, repo_spy, store_spy, terminal_event):
    runtime = make_runtime(DeliveryConfig(), policy, repo_spy, store_spy)

    async def run():
        before = len(asyncio.all_tasks())
        await runtime.start()
        assert len(asyncio.all_tasks()) == before
        assert runtime.open_capture(terminal_event.run_id, 'synthetic-tenant') is None
        receipt = runtime.admit(None, terminal_event)
        await runtime.close(time.monotonic() + 1)
        return receipt
    receipt = asyncio.run(run())
    assert receipt.status == 'disabled'
    assert runtime._pool is None and runtime._queue is None and runtime._workers == ()
    assert repo_spy.calls == store_spy.calls == [] and store_spy.closed == []


def test_start_evidence_runtime_returns_none_without_building_clients(monkeypatch):
    from services.api.app.evaluation import runtime as runtime_module

    def forbidden(*args, **kwargs):
        raise AssertionError('disabled mode must not build clients')
    monkeypatch.setattr(runtime_module, 'create_async_engine', forbidden)
    monkeypatch.setattr(runtime_module.S3PayloadStore, 'from_config', forbidden)
    assert asyncio.run(runtime_module.start_evidence_runtime(DeliveryConfig(), settings=None)) is None


def test_start_failure_cleans_up_partially_built_resources(monkeypatch, terminal_event):
    from services.api.app.evaluation import runtime as runtime_module
    disposed, closed = [], []

    class Engine:
        async def dispose(self):
            disposed.append(True)

    class Store:
        async def close(self, deadline):
            closed.append(deadline)
            return 0
    monkeypatch.setattr(runtime_module, 'create_async_engine', lambda *a, **k: Engine())
    monkeypatch.setattr(runtime_module.S3PayloadStore, 'from_config', classmethod(lambda cls, config: Store()))

    async def fail_start(self):
        raise RuntimeError('start failed')
    monkeypatch.setattr(runtime_module.EvidenceRuntime, 'start', fail_start)
    manifest = __import__('pathlib').Path(__file__).parents[2] / 'deploy/evaluation/restricted-synthetic-v1.json'
    settings = type('S', (), dict(LLM_MODEL_ID=None, LLM_MODEL_NAME='fake-chat', EMBED_MODEL_NAME='fake-embed',
                                  QDRANT_COLLECTION='synthetic-documents'))()
    with pytest.raises(RuntimeError, match='start failed'):
        asyncio.run(runtime_module.start_evidence_runtime(content_config(fixture_manifest=str(manifest)), settings))
    assert disposed == [True] and len(closed) == 1


def test_fixture_case_is_validated_against_the_trusted_manifest(policy, repo_spy, store_spy, registry):
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    metrics = DeliveryMetrics(registry)
    mismatched = make_runtime(content_config(fixture_case='not-in-manifest'), policy, repo_spy, store_spy, metrics)
    other_tenant = make_runtime(content_config(), policy, repo_spy, store_spy, metrics)

    async def run():
        await mismatched.start()
        await other_tenant.start()
        try:
            assert mismatched.open_capture(uuid4(), 'synthetic-tenant') is None
            assert other_tenant.open_capture(uuid4(), 'unlisted-tenant') is None
            assert other_tenant.open_capture(uuid4(), None) is None
            session = other_tenant.open_capture(uuid4(), 'synthetic-tenant')
            assert session is not None and session.fixture_case == 'literal'
            session.close()
        finally:
            await mismatched.close(time.monotonic() + 1)
            await other_tenant.close(time.monotonic() + 1)
    asyncio.run(run())
    assert sample(registry, 'omitted', component='capture', reason='unapproved_source') == 2
    assert sample(registry, 'omitted', component='capture', reason='missing_tenant') == 1


def test_admit_detaches_once_and_never_raises(runtime, terminal_event):
    async def run():
        await runtime.start()
        session = runtime.open_capture(terminal_event.run_id, 'synthetic-tenant')
        first = runtime.admit(session, terminal_event)
        broken = runtime.admit(None, object())  # an unexpected value is contained, not raised
        await runtime.close(time.monotonic() + 1)
        return first, broken
    first, broken = asyncio.run(run())
    assert first.status == 'admitted'
    assert (broken.status, broken.reason) == ('failed', 'metadata_invalid')
    assert runtime._pool.active_runs == 0


def test_metadata_only_admission_without_capture(policy, repo_spy, store_spy, terminal_event):
    runtime = make_runtime(metadata_config(), policy, repo_spy, store_spy)
    assert runtime.open_capture(terminal_event.run_id, 'synthetic-tenant') is None
    receipt = asyncio.run(runtime.process_one(runtime.metadata_run(terminal_event)))
    assert receipt.status == 'committed'
    envelope, intent = repo_spy.enqueued[0]
    assert intent is None and envelope.manifest == () and envelope.completeness == 'metadata_only'
    assert store_spy.calls == []
