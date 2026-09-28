"""Test-only scripted repository/store doubles for retention service and maintenance CLI tests.

They record every call in one shared event log so tests can assert ordering (cleanup marker before
remote IO) and the absence of mutating calls (dry-run).
"""
from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

from services.api.app.evaluation.delivery_models import CleanupClaim, ObjectVersion
from services.api.app.evaluation.outbox import EvidenceUnavailable
from services.api.app.evaluation.payloads import PayloadStorageError

TENANT = "hmac-sha256:" + "a" * 64
MUTATING_REPO_CALLS = {"expire_runs", "claim_orphan", "complete_cleanup", "retry_quarantined"}


def run_namespace(n: int) -> str:
    return f"evidence/test/{TENANT}/00000000-0000-4000-8000-{n:012d}/"


def versions_for(namespace: str, count: int, *, markers: int = 0) -> list[ObjectVersion]:
    found = [ObjectVersion(key=f"{namespace}2026-01-01/input/1", version_id=f"v{i}") for i in range(count)]
    found += [ObjectVersion(key=f"{namespace}2026-01-01/input/1", version_id=f"m{i}", is_delete_marker=True)
              for i in range(markers)]
    return found


class FakeRepo:
    def __init__(self, clock, events: list, *, candidates=(), eligible=None, lease=timedelta(minutes=5),
                 expired_runs: int = 0) -> None:
        self.clock = clock
        self.events = events
        self.candidates = sorted(candidates)
        self.eligible = set(candidates if eligible is None else eligible)
        self.lease = lease
        self.expired_runs = expired_runs
        self.unavailable: set[str] = set()  # method names that raise EvidenceUnavailable
        self.preview_like_claims = False
        self.claims: list[CleanupClaim] = []
        self.completed: list[CleanupClaim] = []
        self.pages: list[tuple[str | None, int]] = []

    def _enter(self, name: str, *detail) -> None:
        self.events.append(("repo", name, *detail))
        if name in self.unavailable:
            raise EvidenceUnavailable()

    async def expire_runs(self, now, limit):
        self._enter("expire_runs")
        return min(self.expired_runs, limit)

    async def count_expired_runs(self, now, limit):
        self._enter("count_expired_runs")
        return min(self.expired_runs, limit)

    async def cleanup_candidates(self, now, limit, *, after=None):
        self._enter("cleanup_candidates")
        self.pages.append((after, limit))
        return tuple(ns for ns in self.candidates if after is None or ns > after)[:limit]

    async def inspect_cleanup(self, namespace, now):
        self._enter("inspect_cleanup", namespace)
        if namespace not in self.eligible:
            return None
        return CleanupClaim(namespace=namespace, lease_token=uuid4(), expires_at=now)

    async def claim_orphan(self, namespace, now):
        self._enter("claim_orphan", namespace)
        if namespace not in self.eligible:
            return None
        expires = now if self.preview_like_claims else now + self.lease
        claim = CleanupClaim(namespace=namespace, lease_token=uuid4(), expires_at=expires)
        self.claims.append(claim)
        return claim

    async def complete_cleanup(self, claim):
        self._enter("complete_cleanup", claim.namespace)
        # Mirrors the real SQL: only the newest, unexpired owner may record completion.
        latest = [c for c in self.claims if c.namespace == claim.namespace][-1]
        if claim != latest or claim.expires_at <= self.clock():
            return False
        self.completed.append(claim)
        return True

    async def retry_quarantined(self, identity):
        self._enter("retry_quarantined")
        return False


class FakeStore:
    def __init__(self, events: list, objects: dict[str, list[ObjectVersion]] | None = None) -> None:
        self.events = events
        self.objects = {ns: list(v) for ns, v in (objects or {}).items()}
        self.on_list = None          # callable(namespace) run during list_versions (e.g. advance the clock)
        self.fail_deletes = 0        # number of delete calls that raise
        self.undeletable = 0         # versions per delete call reported as not deleted
        self.fail_lists = 0
        self.deadlines: list[float] = []

    async def list_versions(self, prefix, *, deadline):
        self.events.append(("store", "list_versions", prefix))
        self.deadlines.append(deadline)
        if self.fail_lists:
            self.fail_lists -= 1
            raise PayloadStorageError("failed")
        if self.on_list is not None:
            self.on_list(prefix)
        return tuple(self.objects.get(prefix, ()))

    async def delete_versions(self, versions, *, deadline):
        self.events.append(("store", "delete_versions", len(versions)))
        self.deadlines.append(deadline)
        if self.fail_deletes:
            self.fail_deletes -= 1
            raise PayloadStorageError("failed")
        kept = list(versions[:self.undeletable])
        for version in versions[self.undeletable:]:
            namespace = next(ns for ns in self.objects if version.key.startswith(ns))
            self.objects[namespace] = [v for v in self.objects[namespace] if v != version]
        return len(versions) - len(kept)


def calls(events, source=None):
    return [e[1] for e in events if source is None or e[0] == source]
