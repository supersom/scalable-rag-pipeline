import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from qdrant_client.http.models import ScoredPoint


def test_retrieval_failure_cancels_sibling_before_ending_parent_span(route, monkeypatch):
    from services.api.app.agents.nodes import retriever
    _, _, exporter = route
    closed = []
    async def embed(query): return [1.]
    async def run():
        started = asyncio.Event()
        async def graph(query, parameters):
            started.set()
            try: await asyncio.Event().wait()
            finally: closed.append(True)
        async def vector(vector, limit):
            await started.wait()
            raise RuntimeError("private secret")
        monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
        monkeypatch.setattr(retriever.neo4j_client, "query", graph)
        monkeypatch.setattr(retriever.qdrant_client, "search", vector)
        with pytest.raises(RuntimeError):
            await retriever.retrieve_context("private query")
        assert closed == [True]  # must not rely on event-loop teardown
    asyncio.run(run())
    [span] = exporter.get_finished_spans()
    assert span.attributes["operation.status"] == "error"
