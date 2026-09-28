"""Committed records survive replacement of worker objects and lost acknowledgements."""
import asyncio
from datetime import timedelta
from uuid import UUID

import consumer_fixture


def test_live_broker_outage_restart_and_ack_loss_are_identical(live_delivery):
    from services.api.app.evaluation.delivery_models import DeliveryReason
    from services.api.app.evaluation.outbox import EvidenceUnavailable
    from services.api.app.evaluation.publisher import PublishError
    from services.evaluation_delivery.worker import DeliveryWorker

    class Outage:
        async def publish(self, *args, **kwargs):
            raise PublishError(DeliveryReason.broker_transient, permanent=False)

    class AckLost:
        def __getattr__(self, name):
            return getattr(live_delivery.repo, name)
        async def mark_delivered(self, claim, receipt):
            raise EvidenceUnavailable()

    async def scenario():
        async with live_delivery.api() as api:
            response = await api.post()
            envelope, original = await live_delivery.wait_run(UUID(response.headers['X-Run-ID']))
            assert envelope.completeness == 'complete'
            # Publication starts only after a committed row is observed read-only.
            await DeliveryWorker(live_delivery.repo, Outage(), live_delivery.clock).run_once()
            assert (await live_delivery.sql('SELECT state FROM evaluation_outbox')) == [('pending',)]
            live_delivery.now += timedelta(seconds=61)
            await live_delivery.publish(AckLost())
            first = live_delivery.receive()['Body'].encode()
            live_delivery.now += timedelta(seconds=61)
            # New Python process/repository/worker: no in-memory claim state survives.
            live_delivery.publish_in_fresh_process()
            second = live_delivery.receive()['Body'].encode()
            assert first == second == original
            assert await consumer_fixture.accept(live_delivery.engine, first, now=live_delivery.now) == 'accepted'
            assert await consumer_fixture.accept(live_delivery.engine, second, now=live_delivery.now) == 'duplicate'
            # Same identity with a valid recomputed but conflicting hash is quarantined.
            from services.api.app.evaluation.delivery_models import ExportEnvelope
            changed = envelope.model_dump()
            changed.pop('export_hash')
            changed['safe_run']['status'] = 'error'
            conflict = ExportEnvelope(**changed)
            assert await consumer_fixture.accept(live_delivery.engine, conflict.canonical_bytes(),
                                                   now=live_delivery.now) == 'conflict'
            assert len(await consumer_fixture.receipts(live_delivery.engine)) == 1
            assert (await live_delivery.sql('SELECT state FROM evaluation_outbox')) == [('delivered',)]
    asyncio.run(scenario())
