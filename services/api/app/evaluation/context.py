"""Append-only request evidence, independent of graph state and trace sampling."""
import hashlib
import hmac
import json
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from threading import RLock

from .models import (
    ConfigurationFingerprint, DependencyObservation, ModelCall, OperationalEvidence,
    RetrievalEvidence, RunEvidence, ToolStep, OperationEvidence,
)

current_collector = ContextVar("run_evidence_collector", default=None)
model_purpose = ContextVar("model_call_purpose", default="unspecified")


def content_hash(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


class RunEvidenceCollector:
    def __init__(self, *, run_id, session_id, configuration: ConfigurationFingerprint,
                 input_text, user_id=None, tenant_id=None, identity_hash_key=None, trace_id=None):
        self._lock = RLock()
        self._started = time.perf_counter()
        self._started_at = datetime.now(timezone.utc)
        self._identity = dict(run_id=run_id, session_id=session_id, trace_id=trace_id, tenant_id=tenant_id)
        self._configuration = configuration.model_copy(deep=True)
        self._input_hash = content_hash(input_text)
        self._user_hash = None
        if identity_hash_key and user_id is not None:
            identity = json.dumps([tenant_id, str(user_id)], separators=(",", ":")).encode()
            self._user_hash = "hmac-sha256:" + hmac.new(identity_hash_key.encode(), identity, hashlib.sha256).hexdigest()
        self._output_hash = self._first_output = None
        self._retrieval, self._models, self._tools, self._dependencies, self._events = [], [], [], [], []
        self._event = None
        self._cache_hit = False

    def _open(self):
        if self._event is not None:
            raise RuntimeError("Evidence collector already finalized")

    @property
    def event(self):
        with self._lock:
            return self._event.model_copy(deep=True) if self._event is not None else None

    def record_output(self, text):
        with self._lock:
            self._open()
            self._output_hash = content_hash(text)

    def mark_first_output(self):
        with self._lock:
            self._open()
            if self._first_output is None:
                self._first_output = (time.perf_counter() - self._started) * 1000

    def record_retrieval(self, evidence: RetrievalEvidence):
        with self._lock:
            self._open()
            self._retrieval.append(evidence.model_copy(deep=True))

    def record_model_call(self, evidence: ModelCall):
        with self._lock:
            self._open()
            self._models.append(evidence.model_copy(deep=True))
            return len(self._models) - 1

    def record_tool_step(self, **fields):
        with self._lock:
            self._open()
            self._tools.append(ToolStep(step=len(self._tools) + 1, **fields))

    def record_event(self, event: OperationEvidence):
        with self._lock:
            self._open()
            self._events.append(event.model_copy(deep=True))

    def record_operation(self, name, attributes):
        # Only operational attributes survive; future callers cannot accidentally
        # turn this metadata event into a prompt/document payload store.
        allowed = {"cache.hit", "cache.skip_reason", "memory.message_count", "planner.action", "planner.fallback",
                   "tool.name", "retrieval.result_count", "retrieval.vector_status", "retrieval.graph_status",
                   "retrieval.limit", "db.collection.name", "embedding.model", "embedding.dimensions",
                   "http.response.status_code", "retry.count"}
        with self._lock:
            self._open()
            self._dependencies.append(DependencyObservation(
                sequence=len(self._dependencies) + 1, name=name,
                status=attributes.get("operation.status", "success"),
                latency_ms=attributes.get("operation.latency_ms", 0.), error_type=attributes.get("error.type"),
                attributes={k: v for k, v in attributes.items() if k in allowed},
            ))
            if name == "cache.lookup":
                self._cache_hit = bool(attributes.get("cache.hit", False))

    def finish(self, status, *, error_type=None):
        with self._lock:
            self._open()
            def total(field):
                values = [getattr(m, field) for m in self._models]
                return sum(v for v in values if v is not None) if any(v is not None for v in values) else None
            unavailable = ["payloads_not_stored", "corpus_index_acl_versions_not_configured", "cost_not_calculated"]
            if self._user_hash is None:
                unavailable.append("identity_hash_key_not_configured")
            self._event = RunEvidence(
                schema_version="1.1" if self._events else "1.0",
                **self._identity, started_at=self._started_at, completed_at=datetime.now(timezone.utc),
                status=status, error_type=error_type, configuration=self._configuration,
                input_hash=self._input_hash, output_hash=self._output_hash, user_id_hash=self._user_hash,
                retrieval=self._retrieval, model_calls=self._models, trajectory=self._tools,
                dependencies=self._dependencies, unavailable=unavailable,
                operation_events=self._events,
                operations=OperationalEvidence(
                    latency_ms=(time.perf_counter() - self._started) * 1000,
                    time_to_first_output_ms=self._first_output,
                    input_tokens=total("input_tokens"), output_tokens=total("output_tokens"), cached_tokens=total("cached_tokens"),
                    usage_complete=bool(self._models) and all(m.input_tokens is not None and m.output_tokens is not None for m in self._models),
                    agent_steps=sum(o.name in {"planner", "retrieval", "tool.execute", "responder"} for o in self._dependencies),
                    retry_count=sum(m.retry_count for m in self._models), cache_hit=self._cache_hit,
                    provider_throttled=any(o.attributes.get("http.response.status_code") == 429 for o in self._dependencies),
                ),
            )
            return self.event


@contextmanager
def bind_collector(collector, *, purpose=None):
    from libs.observability.tracing import operation_observer
    token = current_collector.set(collector)
    observer_token = operation_observer.set(collector.record_operation if collector is not None else None)
    purpose_token = model_purpose.set(purpose) if purpose is not None else None
    try:
        yield collector
    finally:
        if purpose_token is not None:
            model_purpose.reset(purpose_token)
        current_collector.reset(token)
        operation_observer.reset(observer_token)


def collector_from_config(config):
    return (config or {}).get("configurable", {}).get("evidence_collector", current_collector.get())
