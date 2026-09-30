import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI


class Cache:
    def __init__(self):
        self.reads = []
        self.writes = []

    async def get_cached_response(self, question):
        self.reads.append(question)
        return "cached answer"

    async def set_cached_response(self, question, answer):
        self.writes.append((question, answer))


class Memory:
    def __init__(self):
        self.loads = []
        self.writes = []

    async def get_history(self, session_id, limit):
        self.loads.append((session_id, limit))
        return []

    async def add_message(self, *args):
        self.writes.append(args)


class Graph:
    async def astream(self, state, config):
        yield {"planner": {"action": "direct_answer"}}
        yield {"responder": {"messages": [{"content": "fresh answer"}]}}


def test_local_load_header_bypasses_cache_read_and_write_but_loads_memory(route, monkeypatch):
    chat, _, exporter = route
    monkeypatch.setattr(chat, "settings", SimpleNamespace(
        ENV="dev", LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED=True, EVAL_IDENTITY_HASH_KEY=None,
    ))
    monkeypatch.setattr(chat, "agent_app", Graph())
    cache, memory = Cache(), Memory()
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v1/chat")
    app.dependency_overrides.update({
        chat.get_current_user: lambda: {"id": "load-user"},
        chat.get_semantic_cache: lambda: cache,
        chat.get_memory: lambda: memory,
        chat.get_llm_client: lambda: SimpleNamespace(),
    })

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/v1/chat/stream",
                json={"message": "same warranty question", "session_id": "old-session"},
                headers={"X-Load-Test-Bypass-Cache": "1"},
            )

    response = asyncio.run(run())
    events = [json.loads(line) for line in response.text.splitlines()]
    assert response.status_code == 200
    assert events[-1]["type"] == "answer" and events[-1]["content"] == "fresh answer"
    assert cache.reads == cache.writes == []
    assert memory.loads == [("old-session", 6)]
    assert len(memory.writes) == 2
    root = next(span for span in exporter.get_finished_spans() if span.name == "agent.run")
    assert root.attributes["cache.bypassed"] is True


@pytest.mark.parametrize("environment,enabled", [("dev", False), ("prod", True)])
def test_bypass_header_is_ignored_outside_enabled_local_stack(route, monkeypatch, environment, enabled):
    chat, _, _ = route
    monkeypatch.setattr(chat, "settings", SimpleNamespace(
        ENV=environment, LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED=enabled, EVAL_IDENTITY_HASH_KEY=None,
    ))
    cache, memory = Cache(), Memory()
    app = FastAPI()
    app.include_router(chat.router, prefix="/api/v1/chat")
    app.dependency_overrides.update({
        chat.get_current_user: lambda: {"id": "load-user"},
        chat.get_semantic_cache: lambda: cache,
        chat.get_memory: lambda: memory,
        chat.get_llm_client: lambda: SimpleNamespace(),
    })

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return await client.post(
                "/api/v1/chat/stream", json={"message": "same warranty question"},
                headers={"X-Load-Test-Bypass-Cache": "1"},
            )

    response = asyncio.run(run())
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events[-1]["content"] == "cached answer"
    assert cache.reads == ["same warranty question"]
    assert memory.loads == []
