import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from qdrant_client.http.models import ScoredPoint


def test_typed_llm_result_preserves_provider_metadata_and_string_wrapper(route):
    from services.api.app.clients.ray_llm import RayLLMClient
    payloads = []
    def respond(request):
        import json
        payloads.append(json.loads(request.content))
        return httpx.Response(200, headers={"x-request-id": "request-7"}, json={
            "id": "completion-1", "model": "actual-model-v2",
            "choices": [{"message": {"role": "assistant", "content": "private answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4,
                      "prompt_tokens_details": {"cached_tokens": 3}},
        })
    async def run():
        client = RayLLMClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client.client:
            result = await client.chat_completion_result([{"role": "user", "content": "private prompt"}])
            text = await client.chat_completion([{"role": "user", "content": "private prompt"}])
        return result, text
    result, text = asyncio.run(run())
    assert text == result.text == "private answer"
    assert (result.model, result.input_tokens, result.output_tokens, result.cached_tokens) == ("actual-model-v2", 12, 4, 3)
    assert (result.finish_reason, result.provider_request_id) == ("stop", "request-7")
    assert result.completion_id == "completion-1"
    assert payloads[0]["messages"][0]["content"] == "private prompt"


def test_missing_provider_usage_is_unknown_not_estimated(route):
    from services.api.app.clients.ray_llm import RayLLMClient
    async def run():
        client = RayLLMClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": "answer"}}],
        }))) as client.client:
            return await client.chat_completion_result([])
    result = asyncio.run(run())
    assert result.model is None
    assert result.input_tokens is result.output_tokens is result.cached_tokens is None
    assert result.finish_reason is result.provider_request_id is None


@pytest.mark.parametrize("body", [
    {"unexpected": True}, {"error": "private secret"},
    {"choices": [{"message": {"content": None}}]},
])
def test_malformed_provider_response_is_explicit_and_safe(route, body):
    from services.api.app.clients.ray_llm import RayLLMClient
    async def run():
        client = RayLLMClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=body))) as client.client:
            with pytest.raises(ValueError, match="Invalid LLM response") as error:
                await client.chat_completion_result([])
            assert "private secret" not in str(error.value)
    asyncio.run(run())


@pytest.mark.parametrize("recover", [False, True])
def test_transient_model_retries_are_bounded_and_observable(route, recover):
    from services.api.app.clients.ray_llm import RayLLMClient
    from services.api.app.evaluation.context import RunEvidenceCollector, bind_collector
    from services.api.app.evaluation.models import ConfigurationFingerprint
    _, _, exporter = route
    collector = RunEvidenceCollector(run_id="r", session_id="s", input_text="q",
                                     configuration=ConfigurationFingerprint())
    requests = []
    def respond(request):
        requests.append(request)
        if not recover or len(requests) < 3:
            raise httpx.ReadTimeout("private secret")
        return httpx.Response(200, json={"choices": [{"message": {"content": "answer"}}]})
    async def run():
        client = RayLLMClient()
        with bind_collector(collector, purpose="planner"):
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client.client:
                if recover:
                    assert await client.chat_completion([]) == "answer"
                else:
                    with pytest.raises(httpx.ReadTimeout): await client.chat_completion([])
    asyncio.run(run())
    assert len(requests) == 3
    [span] = exporter.get_finished_spans()
    assert span.attributes["retry.count"] == 2
    assert span.attributes["operation.status"] == ("success" if recover else "timeout")
    assert "private secret" not in str(span.attributes) + str(span.events)
    evidence = collector.finish("success" if recover else "timeout")
    assert len(evidence.model_calls) == 1
    assert evidence.model_calls[0].purpose == "planner"
    assert evidence.model_calls[0].retry_count == evidence.operations.retry_count == 2
    assert evidence.model_calls[0].status == ("success" if recover else "timeout")
    assert evidence.operations.input_tokens is None
    assert evidence.operations.usage_complete is False
    assert "private" not in evidence.model_dump_json()


@pytest.mark.parametrize("vector", [None, [], [True], ["private secret"], [float("inf")]])
def test_malformed_embedding_response_fails_safely(route, vector):
    from services.api.app.clients.ray_embed import RayEmbedClient
    _, _, exporter = route
    async def run():
        client = RayEmbedClient()
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=json.dumps({"embedding": vector})))) as client.client:
            with pytest.raises(ValueError, match="Invalid embedding response"):
                await client.embed_query("private prompt")
    asyncio.run(run())
    [span] = exporter.get_finished_spans()
    assert span.attributes["operation.status"] == "error"
    assert "private" not in str(span.attributes) + str(span.events)


@pytest.mark.parametrize("pooled", [False, True])
def test_query_and_document_timeouts_are_preserved_with_or_without_pool(route, monkeypatch, pooled):
    from services.api.app.clients.ray_embed import RayEmbedClient
    seen = []
    def respond(request):
        seen.append(request.extensions["timeout"])
        return httpx.Response(200, json={"embedding": [1., 2.]})
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(respond), **kwargs))
    async def run():
        client = RayEmbedClient()
        if pooled: await client.start()
        try:
            assert await client.embed_query("query") == [1., 2.]
            assert await client.embed_documents(["document"]) == [[1., 2.]]
        finally:
            await client.close()
    asyncio.run(run())
    assert seen == [
        {"connect": 5., "read": 5., "write": 5., "pool": 5.},
        {"connect": 60., "read": 60., "write": 60., "pool": 60.},
    ]
