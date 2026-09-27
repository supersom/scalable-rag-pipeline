"""Attach HTTP tracing once to each application."""
import os
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from libs.observability.tracing import configure_tracing, shutdown_tracing
from services.api.app.config import settings


def _safe_http_request(span, scope):
    if not span.is_recording():
        return
    # Use the registered route template, never the raw URL/query/path supplied
    # by a client. Unmatched paths can also contain secrets or identifiers.
    route = (getattr(span, "attributes", None) or {}).get("http.route", "/unmatched")
    for key in ("http.url", "http.target", "url.full", "url.path"):
        span.set_attribute(key, route)
    span.set_attribute("url.query", "")

def setup_observability(app: FastAPI, *, provider=None):
    if getattr(app.state, "tracer_provider", None) is not None:
        return app.state.tracer_provider
    if provider is None:
        provider = configure_tracing(
            settings.OTEL_SERVICE_NAME,
            endpoint=settings.OTEL_EXPORTER_OTLP_ENDPOINT,
            environment=settings.ENV,
            service_version=settings.OTEL_SERVICE_VERSION,
            sampler_name=settings.OTEL_TRACES_SAMPLER,
            sample_ratio=settings.OTEL_TRACES_SAMPLER_ARG,
            console_enabled=settings.OTEL_CONSOLE_EXPORT_ENABLED,
            export_timeout=settings.OTEL_EXPORTER_OTLP_TIMEOUT,
        )
    # Even if an operator enables header capture, header values stay redacted.
    # Both pinned 0.43b0 and newer integrations honor this sanitization setting.
    os.environ["OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS"] = ".*"
    FastAPIInstrumentor.instrument_app(
        app, tracer_provider=provider, excluded_urls="/health/.*",
        server_request_hook=_safe_http_request,
    )
    app.state.tracer_provider = provider
    return provider


async def shutdown_observability(app: FastAPI):
    await shutdown_tracing(app.state.tracer_provider)
