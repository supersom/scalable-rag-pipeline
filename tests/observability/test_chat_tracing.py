import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI
from opentelemetry import trace
from opentelemetry.trace import StatusCode


class Cache:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error
        self.seen_spans, self.writes = [], []

    async def get_cached_response(self, message):
        self.seen_spans.append(trace.get_current_span().get_span_context())
        if self.error:
            raise self.error
        return self.answer

    async def set_cached_response(self, message, answer):
        self.writes.append((message, answer))


class Memory:
    def __init__(self, error=None):
        self.error, self.writes = error, []

    async def get_history(self, session_id, limit):
        if self.error:
            raise self.error
        return []

    async def add_message(self, *args):
        self.writes.append(args)


class Graph:
    def __init__(self, error=None):
        self.error = error

    async def astream(self, state, config):
        yield {"planner": {"action": "direct_answer"}}
        if self.error:
            raise self.error
        yield {"responder": {"messages": [{"content": "Safe answer"}]}}


async def response_for(chat, cache, memory):
    return await chat.chat_stream(
        chat.ChatRequest(message="private prompt", session_id="conversation-1"),
        BackgroundTasks(), user={"id": "private-user"}, cache=cache, memory=memory,
        llm=SimpleNamespace(),
    )


async def consume(response):
    return [json.loads(event) async for event in response.body_iterator]


@pytest.mark.parametrize("cached", [False, True])
def test_one_run_id_and_span_cover_every_event_and_cache_lookup(route, monkeypatch, cached):
    chat, _, exporter = route
    monkeypatch.setattr(chat, "agent_app", Graph())
    cache, memory = Cache("Cached answer" if cached else None), Memory()

    async def run():
        response = await response_for(chat, cache, memory)
        return response, await consume(response)

    response, events = asyncio.run(run())
    run_id = response.headers["X-Run-ID"]
    UUID(run_id)
    assert events and all(e["run_id"] == run_id for e in events)
    assert all(e["session_id"] == "conversation-1" for e in events)
    [span] = exporter.get_finished_spans()
    assert span.name == "agent.run"
    assert span.attributes["run_id"] == run_id
    assert span.attributes["run.status"] == "success"
    assert span.attributes["cache.hit"] is cached
    assert cache.seen_spans[0].span_id == span.context.span_id
    assert len(memory.writes) == 2
    assert "private" not in str(span.attributes) + str(span.events)


@pytest.mark.parametrize("location", ["cache", "history", "graph", "persist"])
@pytest.mark.parametrize("error,status", [
    (RuntimeError("secret-token"), "error"),
    (TimeoutError("secret-token"), "timeout"),
    (httpx.ReadTimeout("secret-token"), "timeout"),
])
def test_dependency_failures_finish_one_safe_span_and_correlated_error(route, monkeypatch, location, error, status):
    chat, _, exporter = route
    cache = Cache(error=error if location == "cache" else None)
    memory = Memory(error=error if location == "history" else None)
    if location == "persist":
        async def fail(*args):
            raise error
        memory.add_message = fail
    monkeypatch.setattr(chat, "agent_app", Graph(error if location == "graph" else None))

    async def run():
        return await consume(await response_for(chat, cache, memory))

    events = asyncio.run(run())
    [span] = exporter.get_finished_spans()
    assert events[-1]["type"] == "error"
    assert events[-1]["run_id"] == span.attributes["run_id"]
    assert events[-1]["session_id"] == "conversation-1"
    assert span.attributes["run.status"] == status
    assert span.attributes["error.type"] == type(error).__name__
    assert span.status.status_code == StatusCode.ERROR
    assert "secret-token" not in str(events) + str(span.attributes) + str(span.events) + str(span.status.description)


def test_stream_close_from_another_task_finishes_cancelled_without_context_leak(route, monkeypatch):
    chat, _, exporter = route
    monkeypatch.setattr(chat, "agent_app", Graph())

    async def run():
        response = await response_for(chat, Cache(), Memory())
        await anext(response.body_iterator)
        assert not trace.get_current_span().get_span_context().is_valid
        assert exporter.get_finished_spans() == ()
        await asyncio.create_task(response.body_iterator.aclose())
        assert not trace.get_current_span().get_span_context().is_valid

    asyncio.run(run())
    [span] = exporter.get_finished_spans()
    assert span.attributes["run.status"] == "cancelled"


def test_cancellation_during_dependency_wait_propagates_and_finishes_span(route, monkeypatch):
    chat, _, exporter = route
    monkeypatch.setattr(chat, "agent_app", Graph())

    async def run():
        started = asyncio.Event()
        cache = Cache()
        async def wait(message):
            started.set()
            await asyncio.Event().wait()
        cache.get_cached_response = wait
        response = await response_for(chat, cache, Memory())
        task = asyncio.create_task(anext(response.body_iterator))
        await asyncio.wait_for(started.wait(), timeout=2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    [span] = exporter.get_finished_spans()
    assert span.attributes["run.status"] == "cancelled"


def test_http_stream_is_child_of_server_span_and_retries_get_distinct_ids(route, monkeypatch):
    chat, provider, exporter = route
    from services.api.app.observability import setup_observability
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_REQUEST", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SERVER_RESPONSE", ".*")
    monkeypatch.setenv("OTEL_INSTRUMENTATION_HTTP_CAPTURE_HEADERS_SANITIZE_FIELDS", "")
    app = FastAPI()
    setup_observability(app, provider=provider)
    setup_observability(app, provider=provider)  # must not duplicate middleware
    app.include_router(chat.router, prefix="/api/v1/chat")
    app.dependency_overrides.update({
        chat.get_current_user: lambda: {"id": "private-user"},
        chat.get_semantic_cache: lambda: Cache("Cached answer"),
        chat.get_memory: Memory,
        chat.get_llm_client: lambda: SimpleNamespace(),
    })

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            return [await client.post(
                "/api/v1/chat/stream?token=secret-token&message=private-prompt",
                json={"message": "private prompt"},
                headers={"Authorization": "Bearer secret-token", "X-User": "private-user"},
            ) for _ in range(2)]

    responses = asyncio.run(run())
    assert all(r.status_code == 200 for r in responses)
    assert responses[0].headers["X-Run-ID"] != responses[1].headers["X-Run-ID"]
    spans = exporter.get_finished_spans()
    domains = [s for s in spans if s.name == "agent.run"]
    servers = [s for s in spans if s.kind == trace.SpanKind.SERVER]
    assert len(domains) == len(servers) == 2
    assert "secret-token" not in str([s.attributes for s in spans])
    assert "private-prompt" not in str([s.attributes for s in spans])
    assert "private-user" not in str([s.attributes for s in spans])
    for domain in domains:
        server = next(s for s in servers if s.context.trace_id == domain.context.trace_id)
        assert domain.parent.span_id == server.context.span_id
        assert domain.end_time <= server.end_time


@pytest.mark.parametrize("disconnect_on", ["http.response.start", "http.response.body"])
def test_asgi_client_disconnect_closes_run_span(route, monkeypatch, disconnect_on):
    chat, _, exporter = route

    async def run():
        response = await response_for(chat, Cache("Cached answer"), Memory())
        sent_answer = asyncio.Event()
        async def send(message):
            if message["type"] == disconnect_on:
                sent_answer.set()
                # Simulate transport blocked while a disconnect arrives.
                await asyncio.Event().wait()
        async def receive():
            await sent_answer.wait()
            return {"type": "http.disconnect"}
        await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 2)
        assert len(exporter.get_finished_spans()) == 1  # before loop shutdown/GC

    asyncio.run(run())
    [span] = exporter.get_finished_spans()
    assert span.attributes["run.status"] == "cancelled"


def test_concurrent_streams_do_not_share_run_context(route, monkeypatch):
    chat, _, exporter = route
    monkeypatch.setattr(chat, "agent_app", Graph())
    caches = [Cache(), Cache()]
    async def one(cache):
        await asyncio.sleep(0)
        response = await response_for(chat, cache, Memory())
        return await consume(response)
    async def run():
        return await asyncio.gather(*(one(cache) for cache in caches))
    results = asyncio.run(run())
    assert results[0][0]["run_id"] != results[1][0]["run_id"]
    assert len(exporter.get_finished_spans()) == 2
    assert caches[0].seen_spans[0].trace_id != caches[1].seen_spans[0].trace_id


def test_disconnect_closes_underlying_graph_before_finishing_run(route, monkeypatch):
    chat, _, exporter = route
    closed = []
    class ActiveGraph:
        async def astream(self, state, config):
            try:
                yield {"planner": {"action": "direct_answer"}}
                await asyncio.Event().wait()
            finally:
                closed.append(True)
    monkeypatch.setattr(chat, "agent_app", ActiveGraph())
    async def run():
        response = await response_for(chat, Cache(), Memory())
        await anext(response.body_iterator)
        await response.body_iterator.aclose()
        assert closed == [True]
        assert len(exporter.get_finished_spans()) == 1
    asyncio.run(run())
