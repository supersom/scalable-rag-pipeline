"""Opt-in acceptance over loopback HTTP, isolated PostgreSQL, and real Moto S3/SQS.

The API is test-owned: only cache/history/graph routing are replaced. Authentication,
chat streaming, responder/model capture, persistence and publication are production code.
"""
import asyncio
import hashlib
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import pytest

LITERAL = 'Contact alice@example.test; token=fixture-secret'
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('collision', ['bucket', 'queue'])
def test_fixture_rejects_existing_resource_without_cleanup(collision):
    from botocore.exceptions import ClientError
    from acceptance_fixture import LiveDelivery

    harness = object.__new__(LiveDelivery)
    harness.name = 'eval_test_0123456789abcdef'
    harness.bucket = 'eval-test-0123456789abcdef'
    harness.created_schema = harness.created_bucket = False
    harness.queue = None
    harness.engine = Mock(dispose=AsyncMock())
    harness.admin = Mock(dispose=AsyncMock())
    harness.s3 = Mock()
    harness.sqs = Mock()
    if collision == 'queue':
        harness.s3.head_bucket.side_effect = ClientError(
            {'Error': {'Code': '404', 'Message': 'missing'}}, 'HeadBucket')
    with pytest.raises(AssertionError, match=f'{collision} name already exists'):
        harness.setup()
    harness.cleanup()
    harness.s3.create_bucket.assert_not_called()
    harness.s3.delete_bucket.assert_not_called()
    harness.sqs.create_queue.assert_not_called()
    harness.sqs.delete_queue.assert_not_called()


def test_manifest_hashes_match_the_live_acceptance_sources():
    from services.api.app.evaluation.provenance import SyntheticManifest
    # Read the literal template without importing the API's network-bound globals.
    import ast
    tree = ast.parse((ROOT / 'services/api/app/agents/nodes/responder.py').read_text())
    template = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == 'DIRECT_TEMPLATE' for t in n.targets))
    manifest = SyntheticManifest.load(ROOT / 'deploy/evaluation/restricted-synthetic-v1.json')
    for tenant in ('synthetic-tenant', 'synthetic-tenant-2'):
        for kind, content in [('input', LITERAL), ('template', template)]:
            records = [r for r in manifest.sources if r.case_id == 'literal'
                       and r.tenant_id == tenant and r.source_kind == kind]
            assert len(records) == 1, 'acceptance source missing from trusted manifest'
            assert records[0].source_hash == 'sha256:' + hashlib.sha256(content.encode()).hexdigest()


def test_live_delivery_links_stored_versions(live_delivery, caplog):
    async def scenario():
        async with live_delivery.api() as api:
            from services.api.app.evaluation import runtime as runtime_module
            runtime_module._active = None
            try:
                baseline = await api.post()
            finally:
                runtime_module._active = api.runtime
            response = await api.post()
            assert response.status_code == 200
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[-1]['type'] == 'answer'
            assert events[-1]['content'] == 'This is a deterministic local response.'
            normalize = lambda data: [{k: v for k, v in json.loads(line).items()
                                       if k not in ('run_id', 'session_id')} for line in data.splitlines()]
            assert normalize(response.text) == normalize(baseline.text)
            run_id = UUID(response.headers['X-Run-ID'])
            envelope, stored = await live_delivery.wait_run(run_id)
            assert envelope.run_id == run_id
            assert envelope.completeness == 'complete'
            assert {e.slot.kind.value for e in envelope.manifest} == {
                'input', 'model_request', 'model_response', 'output'}
            db_refs = await live_delivery.sql('SELECT p.object_key, p.version_id, p.sha256 FROM '
                'evaluation_payload_refs p JOIN evaluation_runs r ON r.id=p.run_pk')
            assert sorted(db_refs) == sorted((e.reference.key, e.reference.version_id, e.reference.sha256)
                                            for e in envelope.manifest)
            assert (await live_delivery.sql('SELECT r.run_id, o.state FROM evaluation_runs r '
                'JOIN evaluation_outbox o ON r.id=o.run_pk')) == [(run_id, 'pending')]
            await live_delivery.publish()
            message = live_delivery.receive()
            assert message['Body'].encode() == stored == envelope.canonical_bytes()
            assert message['MessageAttributes']['run_id']['StringValue'] == str(run_id)
            assert message['MessageAttributes']['export_hash']['StringValue'] == envelope.export_hash
            from services.api.app.evaluation.payloads import PayloadAccessDenied
            tenant_a = await api.authorized_partition('synthetic-tenant')
            tenant_b = await api.authorized_partition('synthetic-tenant-2')
            from services.api.app.agents.nodes.responder import DIRECT_TEMPLATE
            from services.api.app.evaluation.delivery_models import canonical_bytes
            redacted_input = 'Contact [REDACTED_EMAIL]; token=[REDACTED_SECRET]'
            answer = 'This is a deterministic local response.'
            expected_payloads = {
                'input': canonical_bytes({'text': redacted_input}),
                'model_request': canonical_bytes({
                    'messages': [{'role': 'user', 'content': DIRECT_TEMPLATE.format(query=redacted_input)}],
                    'model': 'fake-chat', 'temperature': 0.3, 'max_tokens': 1024,
                }),
                'model_response': canonical_bytes({'text': answer}),
                'output': canonical_bytes({'text': answer}),
            }
            for entry in envelope.manifest:
                ref = entry.reference
                assert ref.version_id
                data = await live_delivery.store.get(ref, authorized_tenant=tenant_a,
                                                     deadline=time.monotonic() + 3)
                assert data == expected_payloads[entry.slot.kind.value]
                assert hashlib.sha256(data).hexdigest() == ref.sha256
                assert b'fixture-secret' not in data and b'alice@example.test' not in data
                with pytest.raises(PayloadAccessDenied):
                    await live_delivery.store.get(ref, authorized_tenant=tenant_b,
                                                  deadline=time.monotonic() + 3)
            assert b'fixture-secret' not in stored
            from prometheus_client import generate_latest
            metrics = generate_latest(live_delivery.registry).decode()
            assert 'evidence_delivery_events_total{' in metrics
            assert live_delivery.registry.get_sample_value('evidence_delivery_events_total',
                {'component': 'runtime', 'outcome': 'committed', 'reason': 'none'}) == 1
            for private in ('fixture-secret', 'alice@example.test', str(run_id), tenant_a, tenant_b):
                assert private not in metrics and private not in caplog.text
    asyncio.run(scenario())


@pytest.mark.parametrize('mode,tenant,expected', [
    ('cache', 'synthetic-tenant', 'partial'),
    ('unapproved', 'synthetic-tenant', 'partial'),
    ('normal', None, 'metadata_only'),
])
def test_live_denial_preserves_answer(live_delivery, mode, tenant, expected):
    async def scenario():
        async with live_delivery.api(mode=mode) as api:
            response = await api.post(tenant=tenant)
            assert response.status_code == 200
            events = [json.loads(line) for line in response.text.splitlines()]
            assert events[-1]['content'] == (LITERAL if mode == 'cache' else
                                            'This is a deterministic local response.')
            envelope, data = await live_delivery.wait_run(UUID(response.headers['X-Run-ID']))
            assert envelope.completeness == expected
            assert b'fixture-secret' not in data
            if mode == 'cache':
                assert any(e.slot.kind == 'output' and e.state == 'unavailable' for e in envelope.manifest)
            if mode == 'unapproved':
                denied = {e.slot.kind.value: e for e in envelope.manifest
                          if e.slot.kind.value in ('model_request', 'model_response', 'output')}
                assert set(denied) == {'model_request', 'model_response', 'output'}
                assert all(e.state == 'unavailable' and e.reason == 'unapproved_source'
                           and e.reference is None for e in denied.values())
    asyncio.run(scenario())


@pytest.mark.parametrize('at', ['http.response.start', 'http.response.body'])
def test_live_disconnect_releases_content_and_commits_once(live_delivery, at):
    async def scenario():
        async with live_delivery.api() as api:
            sent = await api.disconnect(at)
            headers = dict(sent[0]['headers'])
            envelope, _ = await live_delivery.wait_run(UUID(headers[b'x-run-id'].decode()))
            assert envelope.safe_run.status == 'cancelled'
            assert (await live_delivery.sql('SELECT count(*) FROM evaluation_runs')) == [(1,)]
            assert api.runtime._pool.active_runs == api.runtime._pool.resident_bytes == 0
    asyncio.run(scenario())


@pytest.mark.parametrize('fault', ['partial_upload', 'database_admission'])
def test_live_storage_failure_preserves_answer(live_delivery, fault):
    from services.api.app.evaluation.payloads import PayloadStorageError
    from services.api.app.evaluation.outbox import EvidenceUnavailable

    class PartialStore:
        def __getattr__(self, name):
            return getattr(live_delivery.store, name)
        async def put(self, run_id, tenant_partition, payload, **kwargs):
            if payload.slot.kind == 'model_request':
                raise PayloadStorageError('failed')
            return await live_delivery.store.put(run_id, tenant_partition, payload, **kwargs)
    class FailedCommit:
        def __getattr__(self, name):
            return getattr(live_delivery.repo, name)
        async def enqueue(self, *args, **kwargs):
            raise EvidenceUnavailable()

    async def scenario():
        async with live_delivery.api(store=PartialStore() if fault == 'partial_upload' else None,
                                     repo=FailedCommit() if fault == 'database_admission' else None) as api:
            response = await api.post()
            assert response.status_code == 200
            assert json.loads(response.text.splitlines()[-1])['content'] == 'This is a deterministic local response.'
            deadline = time.monotonic() + 5
            while api.runtime.pending_count:
                assert time.monotonic() < deadline
                await asyncio.sleep(.01)
            if fault == 'partial_upload':
                envelope, _ = await live_delivery.wait_run(UUID(response.headers['X-Run-ID']))
                assert envelope.completeness == 'partial'
                assert sum(e.state == 'stored' for e in envelope.manifest) == 3
                assert any(e.reason == 'upload_failed' for e in envelope.manifest)
            else:
                assert (await live_delivery.sql('SELECT count(*) FROM evaluation_runs')) == [(0,)]
                assert (await live_delivery.sql('SELECT count(*) FROM evaluation_outbox')) == [(0,)]
                assert live_delivery.s3.list_object_versions(Bucket=live_delivery.bucket).get('Versions')
            assert api.runtime.pending_bytes == 0
    asyncio.run(scenario())


def test_live_slow_upload_overflow_does_not_block_answers(live_delivery):
    async def scenario():
        release = asyncio.Event()
        entered = asyncio.Event()
        class SlowStore:
            def __getattr__(self, name):
                return getattr(live_delivery.store, name)
            async def put(self, *args, **kwargs):
                entered.set()
                await release.wait()
                return await live_delivery.store.put(*args, **kwargs)
        async with live_delivery.api(store=SlowStore(), pending_runs=1, runtime_workers=1) as api:
            first = await api.post()
            await asyncio.wait_for(entered.wait(), 2)
            second = await api.post()
            assert first.status_code == second.status_code == 200
            assert json.loads(first.text.splitlines()[-1])['content'] == json.loads(second.text.splitlines()[-1])['content']
            assert api.runtime.pending_count == 1
            assert live_delivery.registry.get_sample_value('evidence_delivery_events_total',
                {'component': 'admission', 'outcome': 'dropped', 'reason': 'pending_capacity'}) == 1
            assert (await live_delivery.sql('SELECT count(*) FROM evaluation_runs')) == [(0,)]
            release.set()
            envelope, _ = await live_delivery.wait_run(UUID(first.headers['X-Run-ID']))
            assert envelope.completeness == 'complete'
            assert (await live_delivery.sql('SELECT count(*) FROM evaluation_runs')) == [(1,)]
            assert api.runtime._pool.active_runs == 0
    asyncio.run(scenario())
