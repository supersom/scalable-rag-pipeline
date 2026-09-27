import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks


@pytest.mark.parametrize("failure,status", [(None, "success"), (RuntimeError("private secret"), "error"), (httpx.ReadTimeout("private secret"), "timeout")])
def test_terminal_evidence_covers_cache_and_dependency_failures(route, failure, status):
    chat, _, _ = route
    class Cache:
        async def get_cached_response(self, message):
            if failure: raise failure
            return "private answer"
    class Memory:
        async def add_message(self, *args): pass
    async def run():
        response = await chat.chat_stream(chat.ChatRequest(message="private question"), BackgroundTasks(),
            user={"id": "private-user"}, cache=Cache(), memory=Memory(), llm=SimpleNamespace())
        async for event in response.body_iterator: pass
        assert hasattr(response, "evidence"), "Missing finalized evidence"
        return response.evidence
    event = asyncio.run(run())
    assert event.status == status
    assert event.error_type == (type(failure).__name__ if failure is not None else None)
    assert event.dependencies[0].name == "cache.lookup"
    assert event.dependencies[0].status == status
    assert "private" not in event.model_dump_json()


@pytest.mark.parametrize("disconnect_on", ["http.response.start", "http.response.body"])
def test_disconnect_finalizes_evidence_before_loop_shutdown(route, disconnect_on):
    chat, _, _ = route
    class Cache:
        async def get_cached_response(self, message): return "answer"
    async def run():
        response = await chat.chat_stream(chat.ChatRequest(message="q"), BackgroundTasks(), user={"id": "u"},
            cache=Cache(), memory=SimpleNamespace(), llm=SimpleNamespace())
        sending = asyncio.Event()
        async def send(message):
            if message["type"] == disconnect_on:
                sending.set()
                await asyncio.Event().wait()
        async def receive():
            await sending.wait()
            return {"type": "http.disconnect"}
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 2)
        assert hasattr(response, "evidence"), "Missing disconnect evidence"
        assert response.evidence.status == "cancelled"
    asyncio.run(run())


def test_concurrent_runs_do_not_leak_collector_context(route):
    from services.api.app.evaluation.context import current_collector, content_hash
    chat, _, _ = route
    ready = 0
    barrier = asyncio.Event()
    class Cache:
        async def get_cached_response(self, message):
            nonlocal ready
            own = current_collector.get()
            ready += 1
            if ready == 2:
                barrier.set()
            await barrier.wait()
            assert current_collector.get() is own
            return message + " answer"
    class Memory:
        async def add_message(self, *args): pass
    async def one(question):
        response = await chat.chat_stream(chat.ChatRequest(message=question), BackgroundTasks(),
            user={"id": "u"}, cache=Cache(), memory=Memory(), llm=SimpleNamespace())
        async for _ in response.body_iterator:
            assert current_collector.get() is None
        return response.evidence
    async def run():
        return await asyncio.gather(one("first"), one("second"))
    first, second = asyncio.run(run())
    assert first.run_id != second.run_id
    assert first.input_hash == content_hash("first")
    assert second.output_hash == content_hash("second answer")
    assert [o.name for o in first.dependencies] == ["cache.lookup", "memory.persist"]
    assert [o.name for o in second.dependencies] == ["cache.lookup", "memory.persist"]
