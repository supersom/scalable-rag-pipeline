"""Bounded local-only content sidecars, independent of graph state and delivery IO.

Only a server-selected fixture case can authorize content. Derived values retain
source proofs by digest; no additional raw prompts, filenames or tool IO are kept.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock
from uuid import UUID
import hashlib
import re

from .delivery_models import CapturedRun, ManifestEntry, PayloadSlot
from .metadata_export import prepare_export
from .provenance import Provenance, SourceProof
from .redaction import redact_payload

current_capture = ContextVar('evaluation_capture', default=None)
current_provenance = ContextVar('evaluation_provenance', default=None)


@contextmanager
def bind_capture(session, provenance=None):
    token = current_capture.set(session)
    proof_token = current_provenance.set(provenance)
    try:
        yield session
    finally:
        current_provenance.reset(proof_token)
        current_capture.reset(token)


def _digest(text):
    try:
        return 'sha256:' + hashlib.sha256(text.encode()).hexdigest()
    except UnicodeError:
        return None


class CapturePool:
    def __init__(self, config, policy):
        self.config = config
        self.policy = policy.model_copy(update={
            'payload_bytes': min(config.payload_bytes, policy.payload_bytes, 65536),
            'run_bytes': min(config.run_bytes, policy.run_bytes, 1048576),
            'payload_slots': min(config.payload_slots, policy.payload_slots, 64),
        })
        self._lock = RLock()
        self._runs = set()
        self._bytes = 0

    @property
    def active_runs(self):
        with self._lock:
            return len(self._runs)

    @property
    def resident_bytes(self):
        with self._lock:
            return self._bytes

    def open(self, run_id, tenant, fixture_case):
        if not self.config.enabled or not self.config.capture_content:
            return None
        if not isinstance(tenant, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,127}', tenant):
            return None
        if not isinstance(fixture_case, str) or len(fixture_case) > 128:
            return None
        try:
            run_id = UUID(str(run_id))
        except (TypeError, ValueError):
            return None
        with self._lock:
            if run_id in self._runs or len(self._runs) >= min(self.config.active_runs, 32):
                return None
            self._runs.add(run_id)
            return CaptureSession(self, run_id, tenant, fixture_case)


class CaptureSession:
    def __init__(self, pool, run_id, tenant, fixture_case):
        self.pool, self.run_id = pool, run_id
        self.tenant, self.fixture_case = tenant, fixture_case
        self._payloads, self._unavailable = {}, {}
        self._bytes, self._closed = 0, False
        self._model_count = 0
        self._model_indexes = {}
        self._derived = {}
        self._counters = {}
        self._overflow = None
        self._provenance_overflow = False

    @property
    def resident_bytes(self):
        with self.pool._lock:
            return self._bytes

    @property
    def unavailable(self):
        with self.pool._lock:
            return (*self._unavailable.values(), *((self._overflow,) if self._overflow else ()))

    def unknown(self):
        return Provenance(tenant_id=self.tenant, case_id=self.fixture_case)

    def approved(self, provenance):
        return (isinstance(provenance, Provenance) and provenance.tenant_id == self.tenant
                and provenance.case_id == self.fixture_case and provenance.approved(self.pool.policy))

    def has_payload(self, slot):
        with self.pool._lock:
            return slot in self._payloads

    def messages_provenance(self, messages):
        if not isinstance(messages, list) or not messages or len(messages) > 64:
            return self.unknown()
        proofs = []
        for message in messages:
            if not isinstance(message, dict) or set(message) != {'role', 'content'}:
                return self.unknown()
            if message['role'] not in ('system', 'user', 'assistant'):
                return self.unknown()
            proofs.append(self.proof('template', message['content']) if message['role'] == 'system'
                          else self.source(message['content']))
        return self.combine(*proofs)

    def proof(self, kind, text, **identity):
        """Resolve actual bytes against trusted manifest records, never document flags."""
        if self._closed or not isinstance(text, str) or len(text) > self.pool.policy.payload_bytes:
            return self.unknown()
        digest = _digest(text)
        if digest is None:
            return self.unknown()
        records = (r for r in self.pool.policy.synthetic_sources
                   if r.tenant_id == self.tenant and r.case_id == self.fixture_case
                   and r.source_kind == kind and r.source_hash == digest
                   and all(getattr(r, key) == value for key, value in identity.items()))
        record = next(records, None)
        return (Provenance(tenant_id=self.tenant, case_id=self.fixture_case,
                          sources=(SourceProof(**record.model_dump(exclude={'tenant_id', 'case_id'})),))
                if record else self.unknown())

    def source(self, text):
        if not isinstance(text, str) or len(text) > self.pool.policy.payload_bytes:
            return self.unknown()
        with self.pool._lock:
            if self._closed or self._provenance_overflow:
                return self.unknown()
            digest = _digest(text)
            if digest in self._derived:
                return self._derived[digest]
        for kind in ('input', 'history'):
            proof = self.proof(kind, text)
            if self.approved(proof):
                return proof
        return self.unknown()

    def combine(self, *proofs):
        if not proofs or any(not self.approved(p) for p in proofs):
            return self.unknown()
        sources = tuple(dict.fromkeys(s for p in proofs for s in p.sources))
        if len(sources) > self.pool.policy.payload_slots:
            return self.unknown()
        return Provenance(tenant_id=self.tenant, case_id=self.fixture_case, sources=sources)

    def remember(self, text, provenance):
        if not isinstance(text, str) or len(text) > self.pool.policy.payload_bytes:
            return
        with self.pool._lock:
            if self._closed:
                return
            digest = _digest(text)
            if digest is None:
                return
            if digest in self._derived:
                provenance = self.combine(self._derived[digest], provenance)
            elif len(self._derived) >= self.pool.policy.payload_slots:
                # Once provenance capacity is exhausted, never fall back to a
                # fixture match that could hide an untracked denied contributor.
                self._provenance_overflow = True
                return
            self._derived[digest] = provenance if self.approved(provenance) else self.unknown()

    def next_ordinal(self, kind):
        with self.pool._lock:
            if kind not in ('retrieval', 'tool'):
                raise ValueError('unsupported capture ordinal')
            self._counters[kind] = self._counters.get(kind, 0) + 1
            return self._counters[kind]

    def begin_model(self, provenance):
        # Allocate at invocation, never at completion; no content is retained here.
        with self.pool._lock:
            self._model_count += 1
            return self._model_count

    def finish_model(self, ordinal, metadata_index):
        with self.pool._lock:
            if not self._closed and 0 < ordinal <= self.pool.policy.payload_slots:
                self._model_indexes.setdefault(metadata_index, ordinal)

    def omit(self, slot, reason):
        with self.pool._lock:
            if not self._closed:
                self._store(slot, ManifestEntry(slot=slot, state='unavailable', reason=reason))

    def _store(self, slot, result):
        if slot in self._payloads or slot in self._unavailable:
            return
        count = len(self._payloads) + len(self._unavailable)
        # Keep all 64 usable payload slots. One additional fixed omission marker
        # describes overflow; subsequent attempts allocate no more manifest entries.
        if count >= self.pool.policy.payload_slots:
            if self._overflow is None:
                self._overflow = ManifestEntry(slot=slot, state='unavailable', reason='payload_count_exceeded')
            return
        if isinstance(result, ManifestEntry):
            self._unavailable[slot] = result
            return
        size = len(result.data)
        if (self._bytes + size > self.pool.policy.run_bytes
                or self.pool._bytes + size > min(self.pool.config.active_bytes, 16777216)):
            self._unavailable[slot] = ManifestEntry(slot=slot, state='unavailable', reason='run_budget_exceeded')
            return
        # Reserve before attaching any redacted bytes to the resident sidecar.
        self.pool._bytes += size
        self._bytes += size
        self._payloads[slot] = result

    def record(self, slot, value, provenance):
        with self.pool._lock:
            if self._closed or slot in self._payloads or slot in self._unavailable:
                return
            if len(self._payloads) + len(self._unavailable) >= self.pool.policy.payload_slots:
                self._store(slot, None)
                return
            if not self.approved(provenance):
                result = ManifestEntry(slot=slot, state='unavailable', reason='unapproved_source')
            else:
                try:
                    result = redact_payload(slot, value, provenance, self.pool.policy)
                except Exception:
                    result = ManifestEntry(slot=slot, state='unavailable', reason='redaction_failed')
            self._store(slot, result)

    def detach(self, event):
        with self.pool._lock:
            if self._closed:
                return None
            try:
                if UUID(event.run_id) != self.run_id:
                    raise ValueError('capture identity mismatch')
                # Session identity is the authenticated server identity. The original
                # PR3 snapshot is preserved, while export cannot inherit a forged tenant.
                export_event = event.model_copy(update={'tenant_id': self.tenant})
                used = set(self._model_indexes.values())
                # Exact invocation mapping is retained for every call that can
                # own payloads. Beyond the content cap, metadata-only calls keep
                # PR3 completion order and get unique non-replay fallback ordinals.
                next_fallback = self._model_count + 1
                ordinals = []
                for index in range(len(event.model_calls)):
                    ordinal = self._model_indexes.get(index)
                    if ordinal is None:
                        while next_fallback in used:
                            next_fallback += 1
                        ordinal = next_fallback
                        used.add(ordinal)
                        next_fallback += 1
                    ordinals.append(ordinal)
                prepared = prepare_export(export_event, self.pool.policy, self._payloads.values(),
                                          self.unavailable, tuple(ordinals))
                return CapturedRun(event=event, prepared=prepared)
            finally:
                self.close()

    def close(self):
        with self.pool._lock:
            if self._closed:
                return
            self._closed = True
            self.pool._bytes -= self._bytes
            self.pool._runs.discard(self.run_id)
            self._bytes = 0
            self._payloads.clear()
            self._unavailable.clear()
            self._derived.clear()
            self._model_indexes.clear()
            self._overflow = None
