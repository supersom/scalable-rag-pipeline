"""Controlled-clock expiry against real PostgreSQL and exact Moto object versions."""
import asyncio
from datetime import timedelta
import time
from uuid import UUID

import pytest
import consumer_fixture


def test_live_expiry_failed_delete_resume_and_metadata_retention(live_delivery):
    from services.api.app.evaluation.payloads import PayloadAccessDenied, PayloadStorageError
    from services.api.app.evaluation.retention import RetentionService

    class FailedDelete:
        def __getattr__(self, name):
            return getattr(live_delivery.store, name)
        async def delete_versions(self, *args, **kwargs):
            raise PayloadStorageError('failed')

    async def scenario():
        async with live_delivery.api() as api:
            response = await api.post()
            envelope, original = await live_delivery.wait_run(UUID(response.headers['X-Run-ID']))
            refs = [entry.reference for entry in envelope.manifest]
            assert refs and all(refs)
            first = refs[0]
            # A later version and delete marker must not change the immutable resolver target.
            live_delivery.s3.put_object(Bucket=first.bucket, Key=first.key, Body=b'other version')
            marker = live_delivery.s3.delete_object(Bucket=first.bucket, Key=first.key)
            assert marker['VersionId'] != first.version_id
            assert await live_delivery.store.get(first, first.tenant_partition, deadline=time.monotonic() + 3)
            live_delivery.now = first.created_at + timedelta(days=30)
            with pytest.raises(PayloadAccessDenied, match='expired'):
                await live_delivery.store.get(first, first.tenant_partition, deadline=time.monotonic() + 3)
            assert all(r.expires_at == r.created_at + timedelta(days=30) for r in refs)
            namespace = live_delivery.store.namespace(first.tenant_partition, envelope.run_id)
            failed = await RetentionService(live_delivery.repo, FailedDelete(), live_delivery.clock).run_once(
                apply=True, limit=10, namespaces=(namespace,))
            assert failed.failures == 1
            assert live_delivery.s3.list_object_versions(Bucket=first.bucket).get('Versions')
            live_delivery.now += timedelta(seconds=301)  # production cleanup lease is five minutes
            cleaned = await RetentionService(live_delivery.repo, live_delivery.store, live_delivery.clock).run_once(
                apply=True, limit=10, namespaces=(namespace,))
            assert cleaned.failures == 0
            remaining = live_delivery.s3.list_object_versions(Bucket=first.bucket)
            assert not remaining.get('Versions') and not remaining.get('DeleteMarkers')
            assert (await live_delivery.wait_run(envelope.run_id))[1] == original
            live_delivery.now = envelope.safe_run.completed_at + timedelta(days=90)
            assert await consumer_fixture.accept(live_delivery.engine, original, now=live_delivery.now) == 'expired'
            assert await live_delivery.publish() == 0
            await RetentionService(live_delivery.repo, live_delivery.store, live_delivery.clock).run_once(
                apply=True, limit=10)
            for table in ('evaluation_runs', 'evaluation_outbox', 'evaluation_payload_refs'):
                assert (await live_delivery.sql(f'SELECT count(*) FROM {table}')) == [(0,)]
    asyncio.run(scenario())
