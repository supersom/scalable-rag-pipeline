"""Evidence delivery metrics with closed label sets.

Every label value must come from the fixed sets below, so no run/tenant/user identifier, object key,
exception message, or queue body can ever become a time series.
"""
from __future__ import annotations

import math
from threading import Lock

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge, Histogram

from .delivery_models import DeliveryReason, UnavailableReason

COMPONENTS = frozenset({"capture", "admission", "runtime", "upload", "commit", "publisher", "worker",
                        "cleanup", "maintenance"})
OUTCOMES = frozenset({"attempted", "admitted", "omitted", "dropped", "stored", "committed", "failed",
                      "retried", "delivered", "quarantined", "cleaned", "disabled"})
REASONS = frozenset({"none", *(r.value for r in DeliveryReason), *(r.value for r in UnavailableReason)})

_BYTE_BUCKETS = (256, 1024, 4096, 16384, 65536, 131072, 262144, 1048576, 4194304, 16777216)


def _closed(value, allowed: frozenset, kind: str) -> str:
    text = "none" if value is None and kind == "reason" else getattr(value, "value", value)
    if not isinstance(text, str) or text not in allowed:
        raise ValueError(f"unsupported metric {kind}")
    return text


def _measure(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("metric values must be finite and non-negative")
    return float(value)


class DeliveryMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self._events = Counter("evidence_delivery_events", "Evidence capture, admission and delivery outcomes",
                               ("component", "outcome", "reason"), registry=registry)
        self._pending_runs = Gauge("evidence_pending_runs", "Admitted runs not yet committed or dropped",
                                   registry=registry)
        self._pending_bytes = Gauge("evidence_pending_bytes", "Redacted payload bytes held by pending runs",
                                    registry=registry)
        self._pending_age = Gauge("evidence_pending_oldest_age_seconds", "Age of the oldest pending run",
                                  registry=registry)
        self._quarantine_count = Gauge("evidence_quarantine_records", "Quarantined outbox records",
                                       registry=registry)
        self._quarantine_age = Gauge("evidence_quarantine_oldest_age_seconds", "Age of the oldest quarantined record",
                                     registry=registry)
        self._seconds = Histogram("evidence_processing_seconds", "Evidence processing latency", ("component",),
                                  registry=registry)
        self._bytes = Histogram("evidence_payload_bytes", "Evidence payload or envelope bytes", ("component",),
                                buckets=_BYTE_BUCKETS, registry=registry)

    def inc(self, component, outcome, reason=None) -> None:
        self._events.labels(_closed(component, COMPONENTS, "component"), _closed(outcome, OUTCOMES, "outcome"),
                            _closed(reason, REASONS, "reason")).inc()

    def pending(self, count, bytes, age) -> None:
        values = (_measure(count), _measure(bytes), _measure(age))
        self._pending_runs.set(values[0])
        self._pending_bytes.set(values[1])
        self._pending_age.set(values[2])

    def quarantine(self, count, age) -> None:
        values = (_measure(count), _measure(age))
        self._quarantine_count.set(values[0])
        self._quarantine_age.set(values[1])

    def observe(self, component, seconds, bytes=None) -> None:
        component = _closed(component, COMPONENTS, "component")
        seconds = _measure(seconds)
        size = None if bytes is None else _measure(bytes)
        self._seconds.labels(component).observe(seconds)
        if size is not None:
            self._bytes.labels(component).observe(size)


_default: DeliveryMetrics | None = None
_default_lock = Lock()


def default_metrics() -> DeliveryMetrics:
    """One instance per process on the global registry; repeated lifespans must not re-register."""
    global _default
    with _default_lock:
        if _default is None:
            _default = DeliveryMetrics(REGISTRY)
        return _default
