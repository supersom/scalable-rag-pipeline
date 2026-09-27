import asyncio
import importlib
import json

import httpx
import pytest


def _load_app():
    try:
        return importlib.import_module("services.fake_models.app").app
    except ModuleNotFoundError:
        pytest.fail("the deterministic local fake-model service is missing")


async def _post(path: str, payload: dict, headers: dict | None = None) -> httpx.Response:
    transport = httpx.ASGITransport(app=_load_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://fake-models") as client:
        return await client.post(path, json=payload, headers=headers)


def test_chat_completion_returns_openai_compatible_metadata():
    response = asyncio.run(
        _post(
            "/v1/chat/completions",
            {
                "model": "fake-local-llm",
                "messages": [{"role": "user", "content": "hello local model"}],
                "temperature": 0,
                "max_tokens": 100,
            },
        )
    )

    assert response.status_code == 200
    body = response.json()
    assert body["model"] == "fake-local-llm"
    assert body["choices"] == [
        {
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "This is a deterministic local response.",
            },
            "finish_reason": "stop",
        }
    ]
    assert body["usage"] == {
        "prompt_tokens": 3,
        "completion_tokens": 6,
        "total_tokens": 9,
    }
    assert body["id"].startswith("fake-chat-")


def test_chat_completion_returns_valid_planner_json_for_greetings():
    response = asyncio.run(
        _post(
            "/v1/chat/completions",
            {
                "model": "fake-local-llm",
                "messages": [
                    {"role": "system", "content": "Classify the user query and respond with ONLY JSON."},
                    {"role": "user", "content": "hello"},
                ],
            },
        )
    )

    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == {
        "action": "direct_answer",
        "tool_name": None,
        "reasoning": "local deterministic greeting",
        "refined_query": "hello",
    }


def test_chat_completion_routes_prefixed_smoke_greeting_without_retrieval():
    response = asyncio.run(
        _post(
            "/v1/chat/completions",
            {
                "model": "fake-local-llm",
                "messages": [
                    {"role": "system", "content": "Classify the user query and respond with ONLY JSON."},
                    {"role": "user", "content": "hello local smoke 123"},
                ],
            },
        )
    )

    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content)["action"] == "direct_answer"


def test_embedding_is_deterministic_and_matches_local_dimension():
    first = asyncio.run(_post("/api/embeddings", {"model": "fake-local-embed", "prompt": "same text"}))
    second = asyncio.run(_post("/api/embeddings", {"model": "fake-local-embed", "prompt": "same text"}))
    different = asyncio.run(_post("/api/embeddings", {"model": "fake-local-embed", "prompt": "different text"}))

    assert first.status_code == 200
    assert len(first.json()["embedding"]) == 1024
    assert first.json() == second.json()
    assert first.json() != different.json()


def test_fake_model_can_force_provider_failure():
    response = asyncio.run(
        _post(
            "/v1/chat/completions",
            {"model": "fake-local-llm", "messages": []},
            headers={"x-fake-scenario": "error"},
        )
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "fake model forced error"}
