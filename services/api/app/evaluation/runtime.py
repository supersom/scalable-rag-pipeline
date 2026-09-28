"""API-owned terminal evidence admission: a bounded, non-durable queue drained by lifespan tasks.

``submit`` is synchronous and performs no IO: it only reserves pending count/bytes and hands the
already-redacted ``CapturedRun`` (by reference, never copied) to a fixed set of worker tasks.
``admitted`` is not a durability promise; only ``process_one`` returning ``committed`` after the
outbox transaction is. Every failure becomes a fixed reason and metric, never a request error.

Per run, the monotonic budget starts at admission: content work (intent + uploads) stops at
``deadline - commit_reserve`` and the metadata commit must finish by ``deadline``. Uploads that
succeed before a failed commit are left for intent-expiry orphan cleanup, never deleted inline.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from .capture import CapturePool
from .delivery_config import DeliveryConfig
from .delivery_metrics import DeliveryMetrics, default_metrics
from .delivery_models import (CapturedRun, DeliveryReason, DeliveryReceipt, EventIdentity, ExportPolicy,
                              ManifestEntry, UnavailableReason)
from .metadata_export import prepare_export, seal_export
from .outbox import EvidencePersistenceError, OutboxRepository
from .payloads import DisabledPayloadStore, PayloadStorageError, S3PayloadStore
from .provenance import SyntheticManifest

logger = logging.getLogger(__name__)

# Fixed server-side span names that may appear as dependency metadata.
DEPENDENCY_NAMES = ("cache.lookup", "cache.write", "memory.load", "memory.persist", "planner", "retrieval",
                    "qdrant.search", "embedding.create", "llm.chat", "responder", "tool.execute")


class _Pending:
    __slots__ = ("captured", "size", "admitted_at")

    def __init__(self, captured: CapturedRun, size: int, admitted_at: float) -> None:
        self.captured, self.size, self.admitted_at = captured, size, admitted_at


def _unavailable(slot, reason: UnavailableReason) -> ManifestEntry:
    return ManifestEntry(slot=slot, state="unavailable", reason=reason)


class EvidenceRuntime:
    def __init__(self, config: DeliveryConfig, policy: ExportPolicy, repo, store, *,
                 metrics: DeliveryMetrics | None = None, monotonic: Callable[[], float] = time.monotonic,
                 on_close: Callable[[], Awaitable[object]] | None = None) -> None:
        self.config = config
        self.policy = policy
        self._repo = repo
        self._store = store
        self._metrics = metrics
        self._monotonic = monotonic
        self._on_close = on_close
        self._enabled = config.enabled
        self._pool = CapturePool(config, policy) if config.enabled and config.capture_content else None
        # Admission-time validation of the server-selected case against the trusted manifest.
        self._cases = frozenset((r.tenant_id, r.case_id) for r in policy.synthetic_sources) \
            if self._pool is not None else frozenset()
        self._queue: asyncio.Queue | None = None
        self._workers: tuple[asyncio.Task, ...] = ()
        self._pending: dict[int, _Pending] = {}
        self._pending_bytes = 0
        self._accepting = False
        self._closed = False

    # --- accounting ---------------------------------------------------------------

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def pending_bytes(self) -> int:
        return self._pending_bytes

    def _count(self, component: str, outcome: str, reason=None) -> None:
        if self._metrics is not None:
            try:
                self._metrics.inc(component, outcome, reason)
            except Exception:
                logger.warning("evidence metric rejected component=%s", component)

    def _observe(self, component: str, seconds: float, size: int | None) -> None:
        if self._metrics is not None:
            try:
                self._metrics.observe(component, max(0.0, seconds), size)
            except Exception:
                logger.warning("evidence metric rejected component=%s", component)

    def _gauge(self) -> None:
        if self._metrics is not None:
            oldest = min((p.admitted_at for p in self._pending.values()), default=None)
            age = 0.0 if oldest is None else max(0.0, self._monotonic() - oldest)
            try:
                self._metrics.pending(len(self._pending), self._pending_bytes, age)
            except Exception:
                logger.warning("evidence metric rejected component=admission")

    def _release(self, item: _Pending) -> None:
        if self._pending.pop(id(item), None) is not None:
            self._pending_bytes -= item.size
        item.captured = None  # drop the last runtime reference to the redacted buffers
        self._gauge()

    # --- lifecycle ------------------------------------------------------------------

    async def start(self) -> None:
        if not self._enabled or self._closed or self._workers:
            return
        self._queue = asyncio.Queue()
        self._workers = tuple(asyncio.create_task(self._work(), name=f"evidence-runtime-{index}")
                              for index in range(self.config.runtime_workers))
        self._accepting = True

    async def close(self, deadline: float) -> int:
        """Stop admission, drain until ``deadline`` (monotonic), drop the rest, close owned IO.

        Returns the number of blocking SDK calls still running after the deadline.
        """
        if self._closed:
            return 0
        self._closed = True
        self._accepting = False
        if not self._enabled:
            return 0
        residue = 0
        try:
            if self._queue is not None:
                for _ in self._workers:
                    self._queue.put_nowait(None)
                if self._workers:
                    _, pending = await asyncio.wait(self._workers,
                                                    timeout=max(0.0, deadline - self._monotonic()))
                    for task in pending:
                        task.cancel()
                    if pending:
                        await asyncio.wait(pending)
                while not self._queue.empty():
                    item = self._queue.get_nowait()
                    if item is not None:
                        self._count("runtime", "dropped", DeliveryReason.runtime_stopped)
                        self._release(item)
            if self._pending:
                logger.warning("evidence runtime dropped pending runs=%d", len(self._pending))
                for item in list(self._pending.values()):
                    self._count("runtime", "dropped", DeliveryReason.runtime_stopped)
                    self._release(item)
        finally:
            try:
                # The store's BoundedIO keeps permits for calls still running on SDK threads.
                residue = await self._store.close(deadline)
            finally:
                if self._on_close is not None:
                    await self._on_close()
                self._gauge()
        return residue

    # --- request-path API (synchronous, no IO) ----------------------------------------

    def open_capture(self, run_id, tenant):
        if self._pool is None or self._closed:
            return None
        try:
            if not isinstance(tenant, str) or not tenant:
                self._count("capture", "omitted", UnavailableReason.missing_tenant)
                return None
            case = self.config.fixture_case
            if (tenant, case) not in self._cases:
                # Content is denied; metadata collection for the run continues.
                self._count("capture", "omitted", UnavailableReason.unapproved_source)
                return None
            session = self._pool.open(run_id, tenant, case)
            if session is None:
                self._count("capture", "omitted", DeliveryReason.active_capacity)
            return session
        except Exception:
            self._count("capture", "omitted", UnavailableReason.unsupported_shape)
            return None

    def metadata_run(self, event) -> CapturedRun:
        ordinals = tuple(range(1, len(event.model_calls) + 1))
        return CapturedRun(event=event, prepared=prepare_export(event, self.policy, (), (), ordinals))

    def admit(self, session, event) -> DeliveryReceipt:
        """Terminal helper: detach the session (or project metadata) and submit, never raising."""
        try:
            if not self._enabled:
                return DeliveryReceipt(status="disabled")
            captured = session.detach(event) if session is not None else self.metadata_run(event)
            if captured is None:
                raise ValueError("capture already detached")
            return self.submit(captured)
        except Exception:
            self._count("admission", "failed", DeliveryReason.metadata_invalid)
            return DeliveryReceipt(status="failed", reason=DeliveryReason.metadata_invalid)
        finally:
            if session is not None:
                session.close()

    def submit(self, captured: CapturedRun) -> DeliveryReceipt:
        if not self._enabled:
            return DeliveryReceipt(status="disabled")
        try:
            identity = EventIdentity(run_id=captured.prepared.safe_run.run_id)
            self._count("admission", "attempted")
            if not self._accepting:
                return self._drop(identity, DeliveryReason.runtime_stopped)
            size = sum(len(payload.data) for payload in captured.prepared.payloads)
            if (len(self._pending) >= self.config.pending_runs
                    or self._pending_bytes + size > self.config.pending_bytes):
                return self._drop(identity, DeliveryReason.pending_capacity)
            item = _Pending(captured, size, self._monotonic())
            self._queue.put_nowait(item)
            self._pending[id(item)] = item
            self._pending_bytes += size
            self._count("admission", "admitted")
            self._gauge()
            return DeliveryReceipt(status="admitted", identity=identity)
        except Exception:
            self._count("admission", "failed", DeliveryReason.metadata_invalid)
            return DeliveryReceipt(status="failed", reason=DeliveryReason.metadata_invalid)

    def _drop(self, identity: EventIdentity, reason: DeliveryReason) -> DeliveryReceipt:
        self._count("admission", "dropped", reason)
        return DeliveryReceipt(status="dropped", reason=reason, identity=identity)

    # --- background processing ------------------------------------------------------------

    async def _work(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            try:
                await self.process_one(item.captured, admitted_at=item.admitted_at)
            except asyncio.CancelledError:
                self._count("runtime", "dropped", DeliveryReason.runtime_stopped)
                raise
            except Exception:
                logger.warning("evidence runtime processing failed")
            finally:
                self._release(item)

    async def process_one(self, captured: CapturedRun, *, admitted_at: float | None = None) -> DeliveryReceipt:
        started = self._monotonic()
        deadline = (started if admitted_at is None else admitted_at) + self.config.deadline_seconds
        content_deadline = deadline - self.config.commit_reserve_seconds
        prepared = captured.prepared
        identity = EventIdentity(run_id=prepared.safe_run.run_id)
        try:
            receipt = await self._process(prepared, identity, deadline, content_deadline)
        except Exception:
            receipt = DeliveryReceipt(status="failed", reason=DeliveryReason.metadata_invalid, identity=identity)
        self._count("runtime", receipt.status, receipt.reason)
        self._observe("runtime", self._monotonic() - started,
                      sum(len(payload.data) for payload in prepared.payloads))
        if receipt.status != "committed":
            logger.info("evidence run not committed reason=%s",
                        receipt.reason.value if receipt.reason else "none")
        return receipt

    async def _process(self, prepared, identity, deadline, content_deadline) -> DeliveryReceipt:
        def failed(reason: DeliveryReason) -> DeliveryReceipt:
            return DeliveryReceipt(status="failed", reason=reason, identity=identity)

        intent, entries = None, []
        if prepared.payloads:
            partition = prepared.safe_run.tenant_partition
            if partition is None:
                entries = [_unavailable(p.slot, UnavailableReason.missing_tenant) for p in prepared.payloads]
            elif self._monotonic() >= content_deadline:
                entries = [_unavailable(p.slot, UnavailableReason.upload_timeout) for p in prepared.payloads]
            else:
                intent, namespace = await self._begin(identity.run_id, partition, content_deadline)
                if intent is None:
                    # No intent, no content writes: fall back to metadata-only within the reserve.
                    entries = [_unavailable(p.slot, UnavailableReason.upload_failed) for p in prepared.payloads]
                else:
                    entries = await self._upload(identity.run_id, partition, namespace, prepared.payloads,
                                                 content_deadline)
        try:
            envelope = seal_export(prepared, tuple(entries))
            if len(envelope.canonical_bytes()) > self.config.queue_body_bytes:
                return failed(DeliveryReason.metadata_too_large)
        except Exception:
            return failed(DeliveryReason.metadata_invalid)
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            return failed(DeliveryReason.deadline_expired)
        started = self._monotonic()
        try:
            receipt = await asyncio.wait_for(self._repo.enqueue(envelope, intent), timeout=remaining)
        except TimeoutError:
            # The transaction outcome is unknown; a commit that landed anyway is recovered by the outbox.
            return failed(DeliveryReason.deadline_expired)
        except Exception:
            return failed(DeliveryReason.database_unavailable)
        self._observe("commit", self._monotonic() - started, None)
        return receipt

    async def _begin(self, run_id, partition, content_deadline):
        try:
            namespace = self._store.namespace(partition, run_id)
            intent = await asyncio.wait_for(
                self._repo.begin_upload(run_id, namespace, lease_seconds=self.config.intent_lease_seconds),
                timeout=content_deadline - self._monotonic())
            return intent, namespace
        except Exception:
            self._count("upload", "failed", DeliveryReason.intent_failed)
            return None, None

    async def _upload(self, run_id, partition, namespace, payloads, content_deadline) -> list[ManifestEntry]:
        entries = []
        for payload in payloads:
            remaining = content_deadline - self._monotonic()
            if remaining <= 0:
                entries.append(_unavailable(payload.slot, UnavailableReason.upload_timeout))
                continue
            started = self._monotonic()
            reason = None
            try:
                reference = await asyncio.wait_for(
                    self._store.put(run_id, partition, payload, deadline=content_deadline), timeout=remaining)
                if reference.tenant_partition != partition or not reference.key.startswith(namespace):
                    raise PayloadStorageError("integrity")
                entry = ManifestEntry(slot=payload.slot, state="stored", reference=reference)
            except TimeoutError:
                reason = UnavailableReason.upload_timeout
            except PayloadStorageError as error:
                reason = UnavailableReason.upload_timeout if error.reason == "timeout" else UnavailableReason.upload_failed
            except Exception:
                reason = UnavailableReason.upload_failed
            if reason is not None:
                self._count("upload", "failed", reason)
                entry = _unavailable(payload.slot, reason)
            else:
                self._count("upload", "stored")
                self._observe("upload", self._monotonic() - started, len(payload.data))
            entries.append(entry)
        return entries


# --- process wiring ---------------------------------------------------------------------

_active: EvidenceRuntime | None = None


def active_runtime() -> EvidenceRuntime | None:
    return _active


def install_runtime(runtime: EvidenceRuntime | None) -> None:
    global _active
    _active = runtime


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def build_policy(config: DeliveryConfig, settings) -> ExportPolicy:
    from services.api.app.tools.registry import TOOL_REGISTRY
    sources = SyntheticManifest.load(Path(config.fixture_manifest)).sources if config.capture_content else ()
    models = (settings.LLM_MODEL_ID, settings.LLM_MODEL_NAME, settings.EMBED_MODEL_NAME)
    return ExportPolicy(
        identity_key=config.identity_key or "",
        # Provenance approval is only possible in the local synthetic policy environment.
        environment="local" if config.environment in ("local", "test") else config.environment,
        allowed_models=tuple(dict.fromkeys(m for m in models if m)),
        allowed_collections=(settings.QDRANT_COLLECTION,), allowed_tools=tuple(TOOL_REGISTRY),
        allowed_dependencies=DEPENDENCY_NAMES, synthetic_sources=sources,
        payload_bytes=min(config.payload_bytes, 65536), run_bytes=min(config.run_bytes, 1048576),
        payload_slots=min(config.payload_slots, 64))


async def start_evidence_runtime(config: DeliveryConfig, settings, *,
                                 metrics: DeliveryMetrics | None = None) -> EvidenceRuntime | None:
    """Build and start the runtime; disabled configuration allocates nothing and returns None.

    Evidence schema migrations are applied out of band (``python -m services.api.app.evaluation.migrate``).
    """
    if not config.enabled:
        return None
    engine = store = None
    try:
        policy = build_policy(config, settings)
        engine = create_async_engine(config.database_url, pool_size=config.runtime_workers, max_overflow=0,
                                     pool_pre_ping=True)
        repo = OutboxRepository(
            async_sessionmaker(engine, expire_on_commit=False), utc_now, max_attempts=config.producer_attempts,
            backoff_base_seconds=config.backoff_base_seconds, backoff_cap_seconds=config.backoff_cap_seconds,
            metadata_retention_days=config.metadata_retention_days,
            content_retention_days=config.content_retention_days, orphan_grace_seconds=config.orphan_grace_seconds)
        store = S3PayloadStore.from_config(config) if config.capture_content else DisabledPayloadStore()
        runtime = EvidenceRuntime(config, policy, repo, store, metrics=metrics or default_metrics(),
                                  on_close=engine.dispose)
        await runtime.start()
        return runtime
    except BaseException:
        try:
            if store is not None:
                await store.close(time.monotonic())
        finally:
            if engine is not None:
                await engine.dispose()
        raise
