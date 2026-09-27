"""Exercise real graph nodes/clients with local HTTP and database boundaries."""
import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks
from qdrant_client.http import models

from services.fake_models.app import app as fake_models


class Memory:
    async def get_history(self, session_id, limit): return []
    async def add_message(self, *args): pass


class GraphDriver:
    def __init__(self, error=None): self.error = error
    @asynccontextmanager
    async def session(self):
        yield self
    async def run(self, query, parameters):
        if self.error: raise self.error
        async def rows():
            yield SimpleNamespace(data=lambda: {"text": "private graph fact", "relationship_id": "r1", "score": .5})
        return rows()


@pytest.mark.parametrize("query,action", [("hi", "direct_answer"), ("policy details", "retrieve")])
def test_real_graph_span_tree_and_metadata_without_logs(route, monkeypatch, query, action):
    chat, _, exporter = route
    from services.api.app.agents.nodes import planner, responder, retriever
    from services.api.app.clients.ray_llm import RayLLMClient
    from services.api.app.clients.ray_embed import RayEmbedClient
    from services.api.app.cache.semantic import SemanticCache
    from services.api.app.config import settings
    from services.api.app.clients.qdrant import qdrant_client
    from services.api.app.clients.neo4j import neo4j_client

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=fake_models), base_url="http://fake") as http:
            llm = RayLLMClient()
            llm.client, llm.endpoint = http, "/v1/chat/completions"
            embedding = RayEmbedClient()
            # The embedding client will own an HTTP boundary, not one client per call.
            embedding.client = http
            monkeypatch.setattr(settings, "RAY_EMBED_ENDPOINT", "http://fake/api/embeddings")
            monkeypatch.setattr(settings, "EMBED_DIM", 1024)
            monkeypatch.setattr(settings, "QDRANT_COLLECTION", "domain_test")
            monkeypatch.setattr(planner, "llm_client", llm)
            monkeypatch.setattr(responder, "llm_client", llm)
            monkeypatch.setattr(retriever, "embed_client", embedding)
            from services.api.app.cache import semantic
            monkeypatch.setattr(semantic, "embed_client", embedding)
            monkeypatch.setattr(neo4j_client, "_driver", GraphDriver())
            await qdrant_client.client.create_collection("domain_test", vectors_config=models.VectorParams(size=1024, distance=models.Distance.COSINE))
            vector = await embedding.embed_query(query)
            await qdrant_client.client.upsert("domain_test", points=[models.PointStruct(id=1, vector=vector, payload={"text": "private document", "filename": "private.pdf"})])
            exporter.clear()
            response = await chat.chat_stream(chat.ChatRequest(message=query), BackgroundTasks(),
                                              user={"id": "private-user"}, cache=SemanticCache(), memory=Memory(), llm=llm)
            events = [json.loads(e) async for e in response.body_iterator]
            # A second identical request must take the real semantic cache-hit path.
            second = await chat.chat_stream(chat.ChatRequest(message=query), BackgroundTasks(),
                                            user={"id": "private-user"}, cache=SemanticCache(), memory=Memory(), llm=llm)
            cached = [json.loads(e) async for e in second.body_iterator]
            return response.headers["X-Run-ID"], events, cached

    run_id, events, cached = asyncio.run(run())
    assert events[-1]["type"] == cached[-1]["type"] == "answer"
    assert next(e for e in events if e["type"] == "routing")["action"] == action
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "agent.run" and s.attributes["run_id"] == run_id)
    spans = [s for s in spans if s.context.trace_id == root.context.trace_id]
    names = [s.name for s in spans]
    for name in ("cache.lookup", "memory.load", "planner", "llm.chat", "responder", "memory.persist", "cache.write", "embedding.create"):
        assert name in names
    if action == "retrieve":
        assert "retrieval" in names and "qdrant.search" in names and "neo4j.search" in names
        retrieval = next(s for s in spans if s.name == "retrieval")
        assert retrieval.attributes["retrieval.result_count"] == 2
        for name in ("embedding.create", "qdrant.search", "neo4j.search"):
            assert any(s.name == name and s.parent.span_id == retrieval.context.span_id for s in spans)
    else:
        assert "retrieval" not in names
    for name in ("planner", "responder"):
        parent = next(s for s in spans if s.name == name)
        model = next(s for s in spans if s.name == "llm.chat" and s.parent.span_id == parent.context.span_id)
        assert model.attributes["gen_ai.usage.input_tokens"] > 0
        assert model.attributes["gen_ai.response.finish_reason"] == "stop"
    for span in spans:
        if span.name != "agent.run":
            assert span.attributes["operation.latency_ms"] >= 0
        assert span.end_time <= root.end_time
        assert "private" not in str(span.attributes) + str(span.events)
    cache_root = next(s for s in exporter.get_finished_spans() if s.name == "agent.run" and s.attributes["run_id"] != run_id)
    cache_names = [s.name for s in exporter.get_finished_spans() if s.context.trace_id == cache_root.context.trace_id]
    assert cache_root.attributes["cache.hit"] is True
    assert "planner" not in cache_names and "cache.lookup" in cache_names and "memory.persist" in cache_names


def test_real_tool_graph_preserves_parentage_and_outcome(route, monkeypatch):
    from services.api.app.agents.nodes import planner, responder
    from services.api.app.clients.ray_llm import RayLLMClient
    chat, _, exporter = route
    class Cache:
        async def get_cached_response(self, message): return None
        async def set_cached_response(self, message, answer): pass
    def respond(request):
        body = json.loads(request.content)
        planning = body["messages"][0]["role"] == "system"
        return httpx.Response(200, json={
            "choices": [{"message": {"content": json.dumps({"action": "tool_use", "tool_name": "calculator", "refined_query": "2+6"}) if planning else "8"}}],
        })
    async def run():
        llm = RayLLMClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as llm.client:
            monkeypatch.setattr(planner, "llm_client", llm)
            monkeypatch.setattr(responder, "llm_client", llm)
            response = await chat.chat_stream(chat.ChatRequest(message="2+6"), BackgroundTasks(),
                user={"id": "private-user"}, cache=Cache(), memory=Memory(), llm=llm)
            return [json.loads(e) async for e in response.body_iterator]
    events = asyncio.run(run())
    assert events[-1]["content"] == "8"
    spans = exporter.get_finished_spans()
    root = next(s for s in spans if s.name == "agent.run")
    tool = next(s for s in spans if s.name == "tool.execute")
    assert tool.parent.span_id == root.context.span_id
    assert tool.attributes["tool.name"] == "calculator"
    assert tool.attributes["operation.status"] == "success"
    assert not any(s.name == "retrieval" for s in spans)
