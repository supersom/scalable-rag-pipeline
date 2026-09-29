"""Delivery metrics: closed label sets, custom registries, and optional injection into producers/workers."""
import asyncio
import logging
import time

import pytest
from prometheus_client import CollectorRegistry

from test_delivery_worker import FakeBroker, FakeRepo, StepClock  # sibling test module (prepend import mode)
from test_runtime import (LITERAL, RepoSpy, StoreSpy, capture_run, content_config, make_runtime,
                          metadata_config)


def all_label_values(registry):
    for family in registry.collect():
        for s in family.samples:
            yield from (v for k, v in s.labels.items() if k != 'le')  # bucket bounds are fixed numbers


def test_counter_accepts_only_closed_labels():
    from services.api.app.evaluation.delivery_metrics import COMPONENTS, OUTCOMES, REASONS, DeliveryMetrics
    from services.api.app.evaluation.delivery_models import DeliveryReason
    registry = CollectorRegistry()
    metrics = DeliveryMetrics(registry)
    metrics.inc('runtime', 'failed', DeliveryReason.database_unavailable)
    metrics.inc('admission', 'admitted')
    assert registry.get_sample_value('evidence_delivery_events_total', {
        'component': 'runtime', 'outcome': 'failed', 'reason': 'database_unavailable'}) == 1
    assert registry.get_sample_value('evidence_delivery_events_total', {
        'component': 'admission', 'outcome': 'admitted', 'reason': 'none'}) == 1
    run_id = '8d1f0f84-6c5e-4c1c-9d0e-3f7a6ddc0b1e'
    for bad in [(run_id, 'failed', None), ('runtime', 'synthetic-tenant', None),
                ('runtime', 'failed', '/etc/passwd'), ('runtime', 'failed', 'token=fixture-secret'),
                ('runtime', 'failed', 'RuntimeError: private detail')]:
        with pytest.raises(ValueError):
            metrics.inc(*bad)
    assert set(all_label_values(registry)) <= COMPONENTS | OUTCOMES | REASONS


def test_gauges_and_histograms_are_unlabelled_or_component_labelled():
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    registry = CollectorRegistry()
    metrics = DeliveryMetrics(registry)
    metrics.pending(3, 2048, 1.5)
    metrics.quarantine(2, 30.0)
    metrics.observe('upload', 0.25, 512)
    assert registry.get_sample_value('evidence_pending_runs') == 3
    assert registry.get_sample_value('evidence_pending_bytes') == 2048
    assert registry.get_sample_value('evidence_pending_oldest_age_seconds') == 1.5
    assert registry.get_sample_value('evidence_quarantine_records') == 2
    assert registry.get_sample_value('evidence_quarantine_oldest_age_seconds') == 30.0
    assert registry.get_sample_value('evidence_processing_seconds_count', {'component': 'upload'}) == 1
    assert registry.get_sample_value('evidence_payload_bytes_sum', {'component': 'upload'}) == 512
    for bad in [(-1, 0, 0), (0, float('nan'), 0), (0, 0, float('inf'))]:
        with pytest.raises(ValueError):
            metrics.pending(*bad)
    with pytest.raises(ValueError):
        metrics.observe('/tmp/x', 1.0, 1)


def test_independent_registries_do_not_collide():
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics, default_metrics
    DeliveryMetrics(CollectorRegistry())
    DeliveryMetrics(CollectorRegistry())
    assert default_metrics() is default_metrics()


def test_runtime_flow_never_labels_identity_tenant_path_or_secret(policy, terminal_event, caplog):
    from services.api.app.evaluation.delivery_metrics import COMPONENTS, OUTCOMES, REASONS
    from services.api.app.evaluation.outbox import EvidenceUnavailable
    registry = CollectorRegistry()
    repo, store = RepoSpy(), StoreSpy()
    runtime = make_runtime(content_config(), policy, repo, store, registry)
    caplog.set_level(logging.DEBUG)

    async def run():
        await runtime.start()
        runtime.submit(capture_run(runtime, terminal_event))
        await asyncio.sleep(0.05)
        repo.intent_error = EvidenceUnavailable()
        runtime.submit(capture_run(runtime, terminal_event))
        runtime.open_capture(terminal_event.run_id, 'unlisted-tenant')
        await runtime.close(time.monotonic() + 1)
    asyncio.run(run())
    values = set(all_label_values(registry))
    assert values <= COMPONENTS | OUTCOMES | REASONS
    forbidden = (str(terminal_event.run_id), 'synthetic-tenant', 'unlisted-tenant', 'fixture-secret', 'evidence/',
                 'hmac-sha256', 'alice')
    assert not any(f in v for v in values for f in forbidden)
    assert not any(f in caplog.text for f in forbidden)
    assert LITERAL not in caplog.text
    assert registry.get_sample_value('evidence_delivery_events_total', {
        'component': 'runtime', 'outcome': 'committed', 'reason': 'none'}) == 2
    assert registry.get_sample_value('evidence_payload_bytes_count', {'component': 'upload'}) == 2
    assert registry.get_sample_value('evidence_pending_runs') == 0


def test_pending_gauges_track_admission(policy, terminal_event):
    registry = CollectorRegistry()
    repo, store = RepoSpy(), StoreSpy()
    repo.hold = asyncio.Event()
    runtime = make_runtime(metadata_config(), policy, repo, store, registry)

    async def run():
        await runtime.start()
        runtime.submit(runtime.metadata_run(terminal_event))
        assert registry.get_sample_value('evidence_pending_runs') == 1
        assert registry.get_sample_value('evidence_pending_oldest_age_seconds') >= 0
        repo.hold.set()
        await runtime.close(time.monotonic() + 1)
    asyncio.run(run())
    assert registry.get_sample_value('evidence_pending_runs') == 0
    assert registry.get_sample_value('evidence_pending_oldest_age_seconds') == 0


def worker_with_metrics(repo, clock, registry):
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    from services.evaluation_delivery.worker import DeliveryWorker
    broker = FakeBroker()
    return DeliveryWorker(repo, broker, clock, metrics=DeliveryMetrics(registry)), broker


def worker_count(registry, outcome, reason='none'):
    return registry.get_sample_value('evidence_delivery_events_total', {
        'component': 'worker', 'outcome': outcome, 'reason': reason}) or 0


def test_worker_counts_delivered_retry_and_quarantine(envelope):
    from services.api.app.evaluation.delivery_models import DeliveryReason
    from services.api.app.evaluation.publisher import PublishError
    registry = CollectorRegistry()
    clock = StepClock()
    repo = FakeRepo(clock)
    worker, broker = worker_with_metrics(repo, clock, registry)
    repo.add(envelope)
    broker.failures = [PublishError(DeliveryReason.broker_transient, permanent=False)]
    asyncio.run(worker.run_once())
    assert worker_count(registry, 'retried', 'broker_transient') == 1
    clock.advance(120)
    asyncio.run(worker.run_once())
    assert worker_count(registry, 'delivered') == 1
    repo.rows.clear()
    repo.add(envelope)
    broker.failures = [PublishError(DeliveryReason.broker_denied, permanent=True)]
    asyncio.run(worker.run_once())
    assert worker_count(registry, 'quarantined', 'broker_denied') == 1


def test_worker_and_publisher_without_metrics_keep_existing_constructors(envelope):
    from services.api.app.evaluation.bounded_io import BoundedIO
    from services.api.app.evaluation.publisher import RunEvidencePublisher
    from services.evaluation_delivery.worker import DeliveryWorker
    clock = StepClock()
    repo = FakeRepo(clock)
    repo.add(envelope)
    worker = DeliveryWorker(repo, FakeBroker(), clock)
    assert asyncio.run(worker.run_once()) == 1
    RunEvidencePublisher(object(), queue_url='http://localhost/q', io=BoundedIO(1), max_body_bytes=10)


def test_publisher_observes_latency_and_bytes(envelope):
    from services.api.app.evaluation.bounded_io import BoundedIO
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    from services.api.app.evaluation.publisher import PublishError, RunEvidencePublisher
    registry = CollectorRegistry()

    class Client:
        def __init__(self):
            self.fail = False

        def send_message(self, **kwargs):
            if self.fail:
                raise RuntimeError('private broker detail')
            return {'MessageId': 'm-1'}
    client = Client()
    publisher = RunEvidencePublisher(client, queue_url='http://localhost/q', io=BoundedIO(1),
                                     max_body_bytes=131072, metrics=DeliveryMetrics(registry))
    data = envelope.canonical_bytes()

    async def run():
        await publisher.publish(data, deadline=time.monotonic() + 2)
        client.fail = True
        with pytest.raises(PublishError):
            await publisher.publish(data, deadline=time.monotonic() + 2)
        await publisher.close(time.monotonic() + 1)
    asyncio.run(run())
    assert registry.get_sample_value('evidence_payload_bytes_sum', {'component': 'publisher'}) == len(data)
    assert registry.get_sample_value('evidence_delivery_events_total', {
        'component': 'publisher', 'outcome': 'failed', 'reason': 'broker_transient'}) == 1
    assert registry.get_sample_value('evidence_delivery_events_total', {
        'component': 'publisher', 'outcome': 'attempted', 'reason': 'none'}) == 2


def test_start_metrics_server_is_idempotent_per_port():
    """worker.serve()/drain_consumer.main() may run more than once in the same process (as the
    test suite itself does); a second start on the same port must be a no-op, not crash with
    'Address already in use'."""
    import socket
    import urllib.request

    from services.api.app.evaluation.delivery_metrics import start_metrics_server

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]

    start_metrics_server(port)
    start_metrics_server(port)  # must not raise OSError: Address already in use

    with urllib.request.urlopen(f'http://127.0.0.1:{port}/metrics', timeout=5) as response:
        body = response.read().decode()
    assert response.status == 200
    assert 'evidence_delivery_events_total' in body
