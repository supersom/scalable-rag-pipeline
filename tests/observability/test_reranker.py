import asyncio
import json

import httpx


def test_openrouter_reranker_returns_provider_order_and_scores(route, monkeypatch):
    from services.api.app.clients.reranker import OpenRouterRerankerClient
    from services.api.app.config import settings

    monkeypatch.setattr(settings, "RERANK_API_ENDPOINT", "https://openrouter.ai/api/v1/rerank")
    monkeypatch.setattr(settings, "RERANK_API_KEY", "router-secret")
    monkeypatch.setattr(settings, "RERANK_MODEL_NAME", "cohere/rerank-v3.5")
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"model": "cohere/rerank-v3.5", "results": [
            {"index": 1, "relevance_score": 0.98, "document": {"text": "second"}},
            {"index": 0, "relevance_score": 0.12, "document": {"text": "first"}},
        ]})

    async def run():
        client = OpenRouterRerankerClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client.client:
            return await client.rerank("query", ["first", "second"])

    ranked = asyncio.run(run())
    assert [(item.index, item.score) for item in ranked] == [(1, 0.98), (0, 0.12)]
    assert requests[0].url == "https://openrouter.ai/api/v1/rerank"
    assert requests[0].headers["authorization"] == "Bearer router-secret"
    assert json.loads(requests[0].content) == {
        "model": "cohere/rerank-v3.5", "query": "query", "documents": ["first", "second"], "top_n": 2
    }


def test_retrieval_reranks_and_reindexes_items(route, monkeypatch):
    from services.api.app.agents.nodes import retriever
    from services.api.app.clients.reranker import RerankResult

    async def embed(query):
        return [1.0]

    async def search(vector, limit):
        return [
            type("Point", (), {"payload": {"text": "first", "chunk_id": "chunk-1"}, "score": 0.9, "id": "1"})(),
            type("Point", (), {"payload": {"text": "second", "chunk_id": "chunk-2"}, "score": 0.8, "id": "2"})(),
        ]

    async def graph(query, parameters):
        return []

    async def rerank(query, documents):
        assert documents == ["first", "second"]
        return [RerankResult(index=1, score=0.95), RerankResult(index=0, score=0.2)]

    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.qdrant_client, "search", search)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    monkeypatch.setattr(retriever.settings, "RERANK_API_ENDPOINT", "https://openrouter.ai/api/v1/rerank")
    monkeypatch.setattr(retriever.reranker_client, "rerank", rerank)

    result = asyncio.run(retriever.retrieve_context("query"))
    assert [(item.content, item.rank, item.score, item.rerank_score, item.retrieval_rank)
            for item in result.items] == [
        ("second", 1, 0.8, 0.95, 2), ("first", 2, 0.9, 0.2, 1)
    ]
