import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from qdrant_client.http.models import ScoredPoint


@pytest.mark.parametrize("error,status", [
    (RuntimeError("private secret"), "error"),
    (httpx.ReadTimeout("private secret"), "timeout"),
    (asyncio.CancelledError(), "cancelled"),
])
def test_domain_span_records_safe_failure_and_propagates_original(route, monkeypatch, error, status):
    from libs.observability import tracing
    _, provider, exporter = route
    # Removing the helper or its safe exception handling must fail this contract.
    assert callable(getattr(tracing, "domain_span", None))
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("test.domain"))
    with pytest.raises(type(error)):
        with tracing.domain_span("dependency"):
            raise error
    [span] = exporter.get_finished_spans()
    assert span.attributes["operation.status"] == status
    assert span.status.status_code == StatusCode.ERROR
    assert "private secret" not in str(span.attributes) + str(span.events) + str(span.status.description)
    assert span.attributes["operation.latency_ms"] >= 0
    assert not trace.get_current_span().get_span_context().is_valid
