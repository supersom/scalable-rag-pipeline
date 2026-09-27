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
