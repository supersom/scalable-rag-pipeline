import asyncio
import json

import httpx


def test_openai_embedding_client_sends_batch_and_reads_vectors(route, monkeypatch):
    from services.api.app.clients.ray_embed import RayEmbedClient
    from services.api.app.config import settings

    monkeypatch.setattr(settings, "EMBED_API_ENDPOINT", "https://api.openai.com/v1/embeddings")
    monkeypatch.setattr(settings, "EMBED_API_KEY", "embed-secret")
    monkeypatch.setattr(settings, "EMBED_MODEL_NAME", "text-embedding-3-small")
    seen = []

    def respond(request):
        seen.append((request.url, request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json={"data": [
            {"object": "embedding", "index": 1, "embedding": [0.2, 0.3]},
            {"object": "embedding", "index": 0, "embedding": [0.1, 0.2]},
        ], "model": "text-embedding-3-small", "usage": {"prompt_tokens": 5, "total_tokens": 5}})

    async def run():
        client = RayEmbedClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client.client:
            return await client.embed_documents(["first", "second"])

    vectors = asyncio.run(run())
    assert vectors == [[0.1, 0.2], [0.2, 0.3]]
    assert seen == [(httpx.URL("https://api.openai.com/v1/embeddings"), "Bearer embed-secret", {
        "model": "text-embedding-3-small", "input": ["first", "second"]
    })]


def test_openai_llm_endpoint_and_key_are_configurable(route, monkeypatch):
    from services.api.app.clients.ray_llm import RayLLMClient
    from services.api.app.config import settings

    monkeypatch.setattr(settings, "LLM_API_ENDPOINT", "https://openrouter.ai/api/v1/chat/completions")
    monkeypatch.setattr(settings, "LLM_API_KEY", "router-secret")
    monkeypatch.setattr(settings, "LLM_MODEL_NAME", "anthropic/claude-sonnet")
    monkeypatch.setattr(settings, "LLM_MODEL_ID", None)
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"model": "anthropic/claude-sonnet", "choices": [
            {"message": {"role": "assistant", "content": "answer"}, "finish_reason": "stop"}
        ]})

    async def run():
        client = RayLLMClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client.client:
            return await client.chat_completion([{"role": "user", "content": "question"}])

    assert asyncio.run(run()) == "answer"
    assert requests[0].url == "https://openrouter.ai/api/v1/chat/completions"
    assert requests[0].headers["authorization"] == "Bearer router-secret"
    assert json.loads(requests[0].content)["model"] == "anthropic/claude-sonnet"
