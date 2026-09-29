"""Evidence delivery worker: claim committed outbox rows, publish exact bytes, record the outcome.

Each claim goes through: expiry check → envelope/claim agreement → one finite send → token-checked
acknowledgement or retry. Repository calls are separate short transactions, so no database lock is
held across the send. A lost acknowledgement write leaves the lease to lapse and a later claim
resends identical bytes (at-least-once; consumers deduplicate on identity/hash). Logs carry only
fixed reason codes and counts: never run IDs, envelope bytes, or exception messages.

Run: ``python -m services.evaluation_delivery.worker``
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys
import time
from collections.abc import Callable
from datetime import datetime, timezone

from services.api.app.evaluation.delivery_models import DeliveryReason, EventIdentity, OutboxClaim
from services.api.app.evaluation.outbox import EvidencePersistenceError
from services.api.app.evaluation.publisher import PublishError, parse_envelope

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class DeliveryWorker:
    def __init__(self, repo, publisher, clock: Callable[[], datetime], *, batch: int = 10, send_workers: int = 2,
                 lease_seconds: float = 60, send_timeout_seconds: float = 6, ack_reserve_seconds: float = 5,
                 poll_seconds: float = 1, monotonic: Callable[[], float] = time.monotonic, metrics=None) -> None:
        if min(batch, send_workers, lease_seconds, send_timeout_seconds, poll_seconds) <= 0 or ack_reserve_seconds < 0:
            raise ValueError("delivery worker limits must be positive")
        if ack_reserve_seconds >= lease_seconds:
            raise ValueError("acknowledgement reserve must be shorter than the lease")
        self._repo = repo
        self._publisher = publisher
        self._clock = clock
        self._batch = batch
        self._send_workers = send_workers
        self._lease_seconds = lease_seconds
        self._send_timeout = send_timeout_seconds
        self._ack_reserve = ack_reserve_seconds
        self._poll = poll_seconds
        self._monotonic = monotonic
        self._metrics = metrics  # optional DeliveryMetrics; fixed component/outcome/reason labels only
        self._closing = False
        self._closed = asyncio.Event()
        self._cycles: set[asyncio.Task] = set()

    async def run_once(self) -> int:
        """Claim one batch and settle it; returns the number of claims taken (0 when idle or closing)."""
        if self._closing:
            return 0
        cycle = asyncio.create_task(self._cycle())
        self._cycles.add(cycle)
        cycle.add_done_callback(self._cycles.discard)
        try:
            return await cycle
        except asyncio.CancelledError:
            # close() cancelled an undrained cycle; its leases lapse and are redelivered later.
            if cycle.cancelled() and self._closing and asyncio.current_task().cancelling() == 0:
                return 0
            raise

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set() and not self._closing:
            claimed = await self.run_once()
            if claimed < self._batch:
                waits = {asyncio.create_task(stop_event.wait()), asyncio.create_task(self._closed.wait())}
                _, pending = await asyncio.wait(waits, timeout=self._poll, return_when=asyncio.FIRST_COMPLETED)
                for wait in pending:
                    wait.cancel()

    async def close(self, deadline: float) -> int:
        """Stop claiming, drain in-flight cycles until ``deadline`` (monotonic), cancel the rest.

        Returns the number of SDK calls still running on publisher threads after the deadline.
        """
        self._closing = True
        self._closed.set()
        cycles = set(self._cycles)
        if cycles:
            _, pending = await asyncio.wait(cycles, timeout=max(0.0, deadline - self._monotonic()))
            for cycle in pending:
                cycle.cancel()
            if pending:
                await asyncio.wait(pending)
                logger.warning("evidence delivery drain cancelled cycles=%d", len(pending))
        return await self._publisher.close(deadline)

    async def _cycle(self) -> int:
        try:
            claims = await self._repo.claim_batch(self._batch, self._lease_seconds)
        except EvidencePersistenceError:
            logger.warning("evidence delivery claim failed reason=%s", DeliveryReason.database_unavailable.value)
            self._count("failed", DeliveryReason.database_unavailable)
            return 0
        if self._metrics is not None:
            try:
                count, age = await self._repo.quarantine_summary(self._clock())
                self._metrics.quarantine(count, age)
            except EvidencePersistenceError:
                logger.warning("evidence delivery quarantine summary failed reason=%s",
                               DeliveryReason.database_unavailable.value)
        slots = asyncio.Semaphore(self._send_workers)

        async def settle(claim: OutboxClaim) -> None:
            async with slots:
                await self._deliver(claim)
        # Settle every claim before surfacing an unexpected error, so no send runs unsupervised.
        outcomes = await asyncio.gather(*(settle(claim) for claim in claims), return_exceptions=True)
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                raise outcome
        return len(claims)

    async def _deliver(self, claim: OutboxClaim) -> None:
        now = self._clock()
        if claim.expires_at <= now:
            # Expired metadata is never published, even if an earlier attempt failed.
            await self._retry(claim, DeliveryReason.expired, permanent=True)
            return
        try:
            envelope = parse_envelope(claim.envelope_bytes)
        except PublishError as error:
            await self._retry(claim, error.reason, permanent=True)
            return
        identity = EventIdentity(export_version=envelope.export_version,
                                 run_schema_version=envelope.run_schema_version, run_id=envelope.run_id)
        if identity != claim.identity or envelope.export_hash != claim.export_hash:
            await self._retry(claim, DeliveryReason.metadata_invalid, permanent=True)
            return
        remaining = (claim.lease_expires_at - now).total_seconds() - self._ack_reserve
        if remaining <= 0:
            # Too little lease left to send and acknowledge safely; let it lapse and be reclaimed.
            logger.info("evidence delivery skipped reason=lease_short")
            return
        deadline = self._monotonic() + min(self._send_timeout, remaining)
        failure: tuple[DeliveryReason, bool] | None = None
        try:
            receipt = await self._publisher.publish(claim.envelope_bytes, deadline=deadline)
        except PublishError as error:
            failure = (error.reason, error.permanent)
        except Exception:
            failure = (DeliveryReason.broker_transient, False)
        if failure is not None:
            await self._retry(claim, failure[0], permanent=failure[1])
            return
        try:
            marked = await self._repo.mark_delivered(claim, receipt)
        except EvidencePersistenceError:
            # The broker has the message; the lease lapses and a later claim resends identical bytes.
            logger.warning("evidence delivery acknowledgement unrecorded reason=%s",
                           DeliveryReason.database_unavailable.value)
            return
        if not marked:
            # A stale lease authorizes no further write; the newer claimant owns the row.
            logger.info("evidence delivery acknowledgement stale")
            return
        self._count("delivered")

    def _count(self, outcome: str, reason: DeliveryReason | None = None) -> None:
        if self._metrics is not None:
            self._metrics.inc("worker", outcome, reason)

    async def _retry(self, claim: OutboxClaim, reason: DeliveryReason, *, permanent: bool) -> None:
        logger.warning("evidence delivery failed reason=%s permanent=%s", reason.value, permanent)
        self._count("quarantined" if permanent else "retried", reason)
        try:
            await self._repo.schedule_retry(claim, reason.value, permanent=permanent)
        except EvidencePersistenceError:
            logger.warning("evidence delivery retry unrecorded reason=%s", DeliveryReason.database_unavailable.value)


EXIT_RUNTIME_FAILURE = 1
EXIT_CONFIGURATION = 2


async def serve(config, publisher) -> int:
    """Own the engine, repository and worker; always drain, unregister signals and dispose on exit."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from services.api.app.evaluation.delivery_metrics import default_metrics
    from services.api.app.evaluation.outbox import OutboxRepository

    loop = asyncio.get_running_loop()
    engine = None
    worker = None
    metrics = default_metrics()
    try:
        engine = create_async_engine(config.database_url, pool_size=config.send_workers + 1, pool_pre_ping=True)
        repo = OutboxRepository(
            async_sessionmaker(engine, expire_on_commit=False), utc_now, max_attempts=config.producer_attempts,
            backoff_base_seconds=config.backoff_base_seconds, backoff_cap_seconds=config.backoff_cap_seconds,
            metadata_retention_days=config.metadata_retention_days,
            content_retention_days=config.content_retention_days, orphan_grace_seconds=config.orphan_grace_seconds)
        worker = DeliveryWorker(
            repo, publisher, utc_now, batch=config.delivery_batch, send_workers=config.send_workers,
            lease_seconds=config.claim_lease_seconds, ack_reserve_seconds=config.ack_reserve_seconds,
            send_timeout_seconds=config.sdk_attempts * (config.sdk_connect_timeout_seconds
                                                        + config.sdk_read_timeout_seconds), metrics=metrics)
        stop = asyncio.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop.set)
        runner = asyncio.create_task(worker.run(stop))
        stopping = asyncio.create_task(stop.wait())
        logger.info("evidence delivery worker started")
        try:
            await asyncio.wait({runner, stopping}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            stopping.cancel()
            residue = await worker.close(time.monotonic() + config.shutdown_drain_seconds)
        await runner  # re-raises a crashed run loop, after draining
    finally:
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        if worker is None:
            await publisher.close(time.monotonic())
        if engine is not None:
            await engine.dispose()
    logger.info("evidence delivery worker stopped residual_calls=%d", residue)
    return 0


def main() -> int:
    """Exit 0 after a signalled stop, 2 for rejected configuration, 1 for any runtime failure.

    Only fixed messages are logged: configuration errors can echo URLs or keys, and runtime errors
    (including pydantic ValidationError, a ValueError subclass) can echo envelope content.
    """
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    from services.api.app.evaluation.delivery_metrics import default_metrics
    from services.api.app.evaluation.publisher import RunEvidencePublisher
    try:
        from services.api.app.config import Settings
        config = DeliveryConfig.from_settings(Settings())
        publisher = RunEvidencePublisher.from_config(config, metrics=default_metrics()) if config.enabled else None
    except Exception:
        logger.error("evidence delivery configuration invalid")
        return EXIT_CONFIGURATION
    if publisher is None:
        logger.error("evidence delivery disabled; set EVAL_EVIDENCE_ENABLED to run the worker")
        return EXIT_CONFIGURATION
    try:
        return asyncio.run(serve(config, publisher))
    except Exception:
        logger.error("evidence delivery worker failed")
        return EXIT_RUNTIME_FAILURE


if __name__ == "__main__":
    sys.exit(main())
