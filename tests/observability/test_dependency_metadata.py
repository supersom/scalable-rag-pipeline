import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from qdrant_client.http.models import ScoredPoint


def test_retrieval_preserves_rank_identity_score_and_deterministic_dedup(route, monkeypatch):
    from services.api.app.agents.nodes import retriever
    points = [
        ScoredPoint(id=7, version=1, score=.9, payload={"text": "alpha", "source_path": "private/path.pdf", "content_hash": "v3"}),
        ScoredPoint(id=7, version=1, score=.95, payload={"text": "alpha", "source_path": "private/path.pdf", "content_hash": "v3"}),
        ScoredPoint(id=8, version=1, score=.8, payload={"text": "beta", "metadata": {"filename": "b.pdf", "document_id": "doc-b", "document_version": "v2", "chunk_id": "chunk-b"}}),
    ]
    async def embed(query): return [1., 0.]
    async def search(vector, limit): return points
    async def graph(query, parameters): return [{"text": "graph fact", "relationship_id": "rel-1", "score": .4}]
    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.qdrant_client, "search", search)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    result = asyncio.run(retriever.retrieve_context("private query"))
    assert result.vector_status == result.graph_status == "success"
    assert [r.content for r in result.items] == ["alpha", "beta", "graph fact"]
    assert [(r.chunk_id, r.rank, r.score) for r in result.items] == [("7", 1, .95), ("chunk-b", 3, .8), ("rel-1", 1, .4)]
    assert result.items[0].document_id.startswith("sha256:")
    assert result.items[0].document_version == "v3"
    assert result.items[1].document_id == "doc-b"
    assert result.items[2].document_id is None
    assert result.items[2].retrieval_source == "neo4j"
    node = asyncio.run(retriever.retrieve_node({"current_query": "private query"}))
    assert node["documents"] == ["alpha [Source: unknown]", "beta [Source: b.pdf]", "graph fact"]


@pytest.mark.parametrize("fails", [False, True])
def test_graph_empty_and_failed_search_have_distinct_outcomes(route, monkeypatch, fails):
    from services.api.app.agents.nodes import retriever
    async def embed(query): return [1.]
    async def search(vector, limit): return []
    async def graph(query, parameters):
        if fails: raise httpx.ReadTimeout("private secret")
        return []
    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.qdrant_client, "search", search)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    result = asyncio.run(retriever.retrieve_context("private query"))
    assert result.items == []
    assert result.vector_status == "success"
    assert result.graph_status == ("timeout" if fails else "success")
    assert result.graph_error_type == ("ReadTimeout" if fails else None)


def test_graph_failure_is_logged_once_per_error_class_without_the_message(route, monkeypatch, caplog):
    import logging
    from services.api.app.agents.nodes import retriever
    failures = iter([httpx.ReadTimeout("private secret"), httpx.ReadTimeout("private secret"), ValueError("private secret")])
    async def embed(query): return [1.]
    async def search(vector, limit): return []
    async def graph(query, parameters): raise next(failures)
    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.qdrant_client, "search", search)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    monkeypatch.setattr(retriever, "_graph_errors_logged", set())
    with caplog.at_level(logging.WARNING, logger=retriever.logger.name):
        results = [asyncio.run(retriever.retrieve_context("private query")) for _ in range(3)]
    assert [r.graph_status for r in results] == ["timeout", "timeout", "error"]
    messages = [r.getMessage() for r in caplog.records if r.name == retriever.logger.name]
    assert len(messages) == 2
    assert "ReadTimeout" in messages[0] and "ValueError" in messages[1]
    assert not any("private" in m for m in messages)


def test_vector_failure_still_fails_the_retrieval(route, monkeypatch):
    from services.api.app.agents.nodes import retriever
    async def embed(query): return [1.]
    async def search(vector, limit): raise RuntimeError("vector store down")
    async def graph(query, parameters): return [{"text": "graph fact", "relationship_id": "rel-1", "score": .4}]
    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.qdrant_client, "search", search)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    with pytest.raises(RuntimeError):
        asyncio.run(retriever.retrieve_context("private query"))


def test_graph_failure_still_returns_the_vector_results_to_the_agent(route, monkeypatch):
    from services.api.app.agents.nodes import retriever
    points = [ScoredPoint(id=1, version=1, score=.9, payload={"text": "alpha", "metadata": {"filename": "a.pdf"}}),
              ScoredPoint(id=2, version=1, score=.8, payload={"text": "beta", "metadata": {"filename": "b.pdf"}})]
    async def embed(query): return [1.]
    async def search(vector, limit): return points
    async def graph(query, parameters): raise ValueError("graph down")
    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.qdrant_client, "search", search)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    result = asyncio.run(retriever.retrieve_context("private query"))
    assert [(i.retrieval_source, i.content) for i in result.items] == [("qdrant", "alpha"), ("qdrant", "beta")]
    assert (result.vector_status, result.graph_status, result.graph_error_type) == ("success", "error", "ValueError")
    node = asyncio.run(retriever.retrieve_node({"current_query": "private query"}))
    assert node["documents"] == ["alpha [Source: a.pdf]", "beta [Source: b.pdf]"]
