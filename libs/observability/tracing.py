"""Process-level OpenTelemetry configuration; payloads never belong in spans."""
import asyncio
import logging
import time
from contextlib import contextmanager
import httpx
from opentelemetry.trace import Status, StatusCode
from threading import Lock
from urllib.parse import urlparse
from weakref import WeakSet

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
from opentelemetry.sdk.trace.sampling import (
    ALWAYS_OFF, ALWAYS_ON, ParentBased, TraceIdRatioBased,
)

logger = logging.getLogger(__name__)
_lock = Lock()
_provider = None
_closed = WeakSet()
domain_tracer = trace.get_tracer("rag.domain")


def failure_status(exc: BaseException) -> str:
    if isinstance(exc, (asyncio.CancelledError, GeneratorExit)):
        return "cancelled"
    return "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "error"


def mark_failure(span, exc: BaseException, *, status: str | None = None):
    """Record only an exception class, never its message or stack arguments."""
    span.set_attribute("operation.status", status or failure_status(exc))
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(Status(StatusCode.ERROR))


@contextmanager
def domain_span(name: str, *, attributes=None):
    """Safe child span; callers supply only small, non-content attributes."""
    started = time.perf_counter()
    with domain_tracer.start_as_current_span(name, attributes=attributes, record_exception=False,
                                           set_status_on_exception=False) as span:
        span.set_attribute("operation.status", "success")
        try:
            yield span
        except BaseException as exc:
            mark_failure(span, exc)
            raise
        finally:
            span.set_attribute("operation.latency_ms", (time.perf_counter() - started) * 1000)


def create_tracer_provider(
    service_name: str,
    *,
    endpoint: str | None = None,
    environment: str = "prod",
    service_version: str = "unknown",
    sampler_name: str = "parentbased_always_on",
    sample_ratio: float = 1.0,
    console_enabled: bool = False,
    console_exporter=None,
    export_timeout: float = 5.0,
) -> TracerProvider:
    """Build an owned provider, also usable without global state in tests.

    OTLP uses gRPC. An http endpoint selects plaintext (local Collector);
    https selects TLS. No endpoint and no console flag means no export.
    """
    if not 0.0 <= sample_ratio <= 1.0:
        raise ValueError("trace sample ratio must be between 0 and 1")
    roots = {"always_on": ALWAYS_ON, "always_off": ALWAYS_OFF,
             "traceidratio": TraceIdRatioBased(sample_ratio)}
    if sampler_name.startswith("parentbased_"):
        root = roots.get(sampler_name.removeprefix("parentbased_"))
        sampler = ParentBased(root) if root is not None else None
    else:
        sampler = roots.get(sampler_name)
    if sampler is None:
        raise ValueError(f"unsupported trace sampler: {sampler_name}")
    if endpoint:
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OTLP gRPC endpoint must be an http(s) URL")
    if export_timeout <= 0:
        raise ValueError("trace export timeout must be positive")
    provider = TracerProvider(
        resource=Resource.create({
            "service.name": service_name,
            "service.version": service_version,
            "deployment.environment": environment,
        }),
        sampler=sampler,
    )
    if endpoint:
        provider.add_span_processor(BatchSpanProcessor(
            OTLPSpanExporter(endpoint=endpoint, insecure=parsed.scheme == "http", timeout=export_timeout),
            max_queue_size=2048, max_export_batch_size=512,
            export_timeout_millis=int(export_timeout * 1000),
        ))
    if console_enabled:
        provider.add_span_processor(BatchSpanProcessor(console_exporter or ConsoleSpanExporter()))
    return provider


def configure_tracing(service_name: str, **kwargs) -> TracerProvider:
    """Initialize the process provider once; repeated application imports reuse it."""
    global _provider
    with _lock:
        if _provider is None:
            _provider = create_tracer_provider(service_name, **kwargs)
            trace.set_tracer_provider(_provider)
        return _provider


async def shutdown_tracing(provider: TracerProvider) -> None:
    """Drain the bounded exporter queue away from the event loop, once."""
    with _lock:
        if provider in _closed:
            return
        _closed.add(provider)

    def drain():
        try:
            if not provider.force_flush(timeout_millis=5000):
                logger.warning("Trace flush timed out during shutdown")
        finally:
            provider.shutdown()

    await asyncio.to_thread(drain)


def get_current_span_id():
    """Legacy helper returning the active trace ID, or None outside a trace."""
    context = trace.get_current_span().get_span_context()
    return context.trace_id if context.is_valid else None
