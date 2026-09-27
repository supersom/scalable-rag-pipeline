import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from qdrant_client.http.models import ScoredPoint


def test_cache_failure_remains_best_effort_but_distinct_from_miss(route, monkeypatch):
    from libs.observability.tracing import domain_span
    from services.api.app.cache import semantic
    _, _, exporter = route
    async def fail(): raise RuntimeError("private secret")
    monkeypatch.setattr(semantic.SemanticCache, "ensure_collection", lambda self: fail())
    async def run():
        with domain_span("cache.lookup"):
            return await semantic.SemanticCache().get_cached_response("private query")
    assert asyncio.run(run()) is None
    [span] = exporter.get_finished_spans()
    assert span.attributes["operation.status"] == "error"
    assert "private" not in str(span.attributes) + str(span.events)
