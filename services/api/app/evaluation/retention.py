"""Evidence retention: metadata expiry and coordinated orphan/expired-content cleanup.

Every remote deletion is authorized by a cleanup claim that PostgreSQL committed first
(``OutboxRepository.claim_orphan``), is scoped to the exact versions listed under one run namespace,
and runs outside any database transaction. A claim authorizes deletion only while its lease is
strictly in the future: read-only previews (``inspect_cleanup``) carry ``expires_at == now`` and so
never qualify, and a lease that lapsed mid-cleanup is re-claimed rather than trusted. Completion is
recorded only when every listed version was resolved; anything else leaves the claim to lapse so a
later run resumes. Any database doubt defers cleanup and counts a failure: no coordination, no delete.

Dry-run uses only read-only queries (``count_expired_runs``, ``cleanup_candidates``,
``inspect_cleanup``, ``list_versions``). Reports carry counts only, never keys or payload bytes.
"""
from __future__ import annotations

import re
import time
from collections.abc import Callable
from contextlib import aclosing
from datetime import datetime, timedelta

from .delivery_models import CleanupClaim, CleanupReport, DeliveryReason
from .outbox import EvidencePersistenceError
from .payloads import KEY_ROOT, PayloadAccessDenied, PayloadStorageError

_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9:_-]{0,127}"
_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
# Exactly one run's namespace (environment/tenant/run): never a tenant- or environment-wide prefix.
RUN_NAMESPACE = re.compile(rf"^{KEY_ROOT}/{_SEGMENT}/{_SEGMENT}/{_UUID}/$")


class _DatabaseUnavailable(Exception):
    """Internal signal: stop the cycle, coordination cannot be established."""


class _Tally:
    def __init__(self) -> None:
        self.counts = dict.fromkeys(CleanupReport.model_fields, 0)

    def add(self, name: str, amount: int = 1) -> None:
        self.counts[name] += amount

    def report(self) -> CleanupReport:
        return CleanupReport(**self.counts)


class RetentionService:
    def __init__(self, repo, store, clock: Callable[[], datetime], *, monotonic: Callable[[], float] = time.monotonic,
                 io_timeout_seconds: float = 30.0, page_size: int = 100, max_claims: int = 3, metrics=None) -> None:
        if min(io_timeout_seconds, page_size, max_claims) <= 0:
            raise ValueError("retention limits must be positive")
        self._repo = repo
        self._store = store  # None when content capture is not configured: metadata expiry only
        self._clock = clock
        self._monotonic = monotonic
        self._io_timeout = io_timeout_seconds
        self._page_size = page_size
        self._max_claims = max_claims
        self._metrics = metrics

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() != timedelta(0):
            raise ValueError("retention clock must return UTC-aware timestamps")
        return now

    def _authorizes(self, claim: CleanupClaim, namespace: str) -> bool:
        # Strictly greater: a preview's expires_at equals its "now" and must never qualify.
        return claim.namespace == namespace and claim.expires_at > self._now()

    def _fail(self, tally: _Tally, reason: DeliveryReason) -> None:
        tally.add("failures")
        if self._metrics is not None:
            self._metrics.inc("cleanup", "failed", reason)

    async def run_once(self, *, apply: bool, limit: int, namespaces: tuple[str, ...] | None = None) -> CleanupReport:
        """Expire metadata, then clean at most ``limit`` namespaces (discovered, or the explicit targets)."""
        if limit <= 0:
            raise ValueError("retention limit must be positive")
        if namespaces is not None and not all(isinstance(ns, str) and RUN_NAMESPACE.fullmatch(ns)
                                              for ns in namespaces):
            raise ValueError("cleanup targets must be exact run namespaces")
        tally = _Tally()
        try:
            await self._expire(apply, limit, tally)
            if self._store is not None:
                await self._clean_namespaces(apply, limit, namespaces, tally)
        except _DatabaseUnavailable:
            self._fail(tally, DeliveryReason.database_unavailable)
        return tally.report()

    async def _repo_call(self, call):
        try:
            return await call
        except EvidencePersistenceError:
            raise _DatabaseUnavailable() from None

    async def _expire(self, apply: bool, limit: int, tally: _Tally) -> None:
        now = self._now()
        repo_call = self._repo.expire_runs if apply else self._repo.count_expired_runs
        tally.add("runs_expired", await self._repo_call(repo_call(now, limit)))

    async def _clean_namespaces(self, apply: bool, limit: int, namespaces, tally: _Tally) -> None:
        async with aclosing(self._targets(limit, namespaces)) as targets:
            async for namespace in targets:
                tally.add("namespaces_examined")
                if not RUN_NAMESPACE.fullmatch(namespace):
                    continue  # discovered coordination rows for broader prefixes are never cleaned here
                if apply:
                    await self._clean(namespace, tally)
                else:
                    await self._preview(namespace, tally)

    async def _targets(self, limit: int, namespaces):
        if namespaces is not None:
            for namespace in namespaces[:limit]:
                yield namespace
            return
        remaining, after = limit, None
        while remaining > 0:
            page = await self._repo_call(self._repo.cleanup_candidates(
                self._now(), min(self._page_size, remaining), after=after))
            for namespace in page:
                yield namespace
            if len(page) < min(self._page_size, remaining):
                return
            remaining -= len(page)
            after = page[-1]

    async def _list(self, namespace: str, deadline: float, tally: _Tally):
        try:
            return await self._store.list_versions(namespace, deadline=deadline)
        except (PayloadStorageError, PayloadAccessDenied):
            self._fail(tally, DeliveryReason.cleanup_failed)
            return None

    async def _preview(self, namespace: str, tally: _Tally) -> None:
        preview = await self._repo_call(self._repo.inspect_cleanup(namespace, self._now()))
        if preview is None:
            return
        tally.add("namespaces_eligible")
        versions = await self._list(namespace, self._monotonic() + self._io_timeout, tally)
        if versions is not None:
            tally.add("versions_examined", len(versions))

    async def _claim(self, namespace: str) -> CleanupClaim | None:
        return await self._repo_call(self._repo.claim_orphan(namespace, self._now()))

    async def _clean(self, namespace: str, tally: _Tally) -> None:
        claim, claims = None, 0
        while True:
            if claim is None or not self._authorizes(claim, namespace):
                # First claim, or the lease lapsed (or was never live) before deletion: re-claim.
                if claims >= self._max_claims:
                    self._fail(tally, DeliveryReason.cleanup_failed)
                    return
                claim = await self._claim(namespace)
                claims += 1
                if claim is None:
                    return  # not eligible: live intent/reference, grace pending, or another cleaner's lease
                if claims == 1:
                    tally.add("namespaces_eligible")
                continue
            versions = await self._list(namespace, self._monotonic() + self._io_timeout, tally)
            if versions is None:
                return  # listing failed and was counted; the claim lapses and a later run resumes
            if self._authorizes(claim, namespace):
                break
        tally.add("versions_examined", len(versions))
        deleted = 0
        if versions:
            try:
                deleted = await self._store.delete_versions(versions, deadline=self._monotonic() + self._io_timeout)
            except (PayloadStorageError, PayloadAccessDenied):
                self._fail(tally, DeliveryReason.cleanup_failed)
                return
        tally.add("versions_deleted", deleted)
        if deleted != len(versions):
            self._fail(tally, DeliveryReason.cleanup_failed)  # partial: resumable, never completed
            return
        if not await self._repo_call(self._repo.complete_cleanup(claim)):
            # Lease lapsed during deletion: a later claim relists (now empty) and records completion.
            self._fail(tally, DeliveryReason.cleanup_failed)
            return
        tally.add("namespaces_cleaned")
        if self._metrics is not None:
            self._metrics.inc("cleanup", "cleaned")
