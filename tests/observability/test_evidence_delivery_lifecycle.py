"""Chat lifecycle integration of the evidence runtime: fail-open, exactly-once terminal admission."""
import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks

LITERAL = 'Contact alice@example.test; token=fixture-secret'
TENANT_USER = {"id": "synthetic-user", "tenant_id": "synthetic-tenant"}


def policy():
    from services.api.app.evaluation.delivery_models import ExportPolicy
    from services.api.app.evaluation.provenance import SyntheticManifest
    manifest = SyntheticManifest.load(Path(__file__).parents[2] / 'deploy/evaluation/restricted-synthetic-v1.json')
    return ExportPolicy(identity_key='dedicated-synthetic-evidence-key', allowed_models=('fake-chat',),
                        synthetic_sources=manifest.sources)


def config(capture=True, **overrides):
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    values = dict(enabled=True, database_url='postgresql://local/test', queue_url='http://localhost/queue',
                  identity_key='dedicated-synthetic-evidence-key')
    if capture:
        values.update(capture_content=True, environment='local', content_policy='restricted-synthetic-v1',
                      fixture_manifest='local-fixture.json', fixture_case='literal', bucket='local-bucket',
                      kms_key_id='local-key', s3_endpoint_url='http://localhost:5000')
    values.update(overrides)
    return DeliveryConfig(**values)


class BlockingRepo:
    def __init__(self):
        self.release = asyncio.Event()
        self.calls, self.enqueued = [], []

    async def begin_upload(self, run_id, namespace, lease_seconds):
        from datetime import datetime, timedelta, timezone
        from uuid import uuid4
        from services.api.app.evaluation.delivery_models import UploadIntent
        self.calls.append('begin_upload')
        await self.release.wait()
        return UploadIntent(run_id=run_id, namespace=namespace, lease_token=uuid4(),
                            expires_at=datetime.now(timezone.utc) + timedelta(seconds=lease_seconds))

    async def enqueue(self, envelope, intent):
        from services.api.app.evaluation.delivery_models import DeliveryReceipt
        self.calls.append('enqueue')
        await self.release.wait()
        self.enqueued.append(envelope)
        return DeliveryReceipt(status='committed', export_hash=envelope.export_hash)


class BlockingStore:
    def __init__(self):
        self.release = asyncio.Event()
        self.puts = []

    def namespace(self, tenant_partition, run_id):
        return f'evidence/local/{tenant_partition}/{run_id}/'

    async def put(self, run_id, tenant_partition, payload, *, deadline):
        import hashlib
        from datetime import datetime, timedelta, timezone
        from services.api.app.evaluation.delivery_models import PayloadRef
        self.puts.append(payload)
        await self.release.wait()
        now = datetime.now(timezone.utc)
        return PayloadRef(bucket='local-bucket', key=f'{self.namespace(tenant_partition, run_id)}x/{payload.slot.kind.value}',
                          version_id='v1', tenant_partition=tenant_partition, slot=payload.slot,
                          byte_size=len(payload.data), sha256=hashlib.sha256(payload.data).hexdigest(),
                          policy_version=payload.policy_version, created_at=now, expires_at=now + timedelta(days=30))

    async def close(self, deadline):
        return 0


class Cache:
    def __init__(self, answer=None, error=None):
        self.answer, self.error = answer, error

    async def get_cached_response(self, message):
        if self.error:
            raise self.error
        return self.answer

    async def set_cached_response(self, message, answer):
        pass


class Memory:
    def __init__(self):
        self.writes = []

    async def get_history(self, session_id, limit):
        return []

    async def add_message(self, *args):
        self.writes.append(args)


class Graph:
    def __init__(self, error=None):
        self.error = error
        self.captures = []

    async def astream(self, state, config):
        from services.api.app.evaluation.capture import current_capture
        self.captures.append(current_capture.get())
        yield {"planner": {"action": "direct_answer"}}
        if self.error:
            raise self.error
        self.captures.append(current_capture.get())
        yield {"responder": {"messages": [{"content": "Safe answer"}]}}


@pytest.fixture
def install(monkeypatch):
    from services.api.app.evaluation import runtime as runtime_module

    def install(runtime):
        monkeypatch.setattr(runtime_module, "_active", runtime)
        return runtime
    return install


def counting_runtime(cfg, repo=None, store=None):
    from services.api.app.evaluation.runtime import EvidenceRuntime

    class Counting(EvidenceRuntime):
        admitted = 0
        submitted = 0

        def admit(self, session, event):
            type(self).admitted += 1
            self.admitted_events = getattr(self, 'admitted_events', []) + [event]
            return super().admit(session, event)

        def submit(self, captured):
            type(self).submitted += 1
            return super().submit(captured)
    if repo is None:
        repo, store = BlockingRepo(), BlockingStore()
        repo.release.set()
        store.release.set()
    return Counting(cfg, policy(), repo, store)


async def asgi(response, *, disconnect_on=None):
    sent = []
    blocked = asyncio.Event()

    async def send(message):
        sent.append(message)
        if message["type"] == disconnect_on:
            blocked.set()
            await asyncio.Event().wait()

    async def receive():
        if disconnect_on is None:
            await asyncio.Event().wait()
        await blocked.wait()
        return {"type": "http.disconnect"}
    await asyncio.wait_for(response({"type": "http", "asgi": {"spec_version": "2.0"}}, receive, send), 2)
    return sent


def test_answer_and_headers_are_sent_while_store_and_database_are_blocked(route, install):
    chat, _, _ = route
    repo, store = BlockingRepo(), BlockingStore()
    runtime = install(counting_runtime(config(), repo, store))

    async def run():
        await runtime.start()
        response = await chat.chat_stream(chat.ChatRequest(message=LITERAL), BackgroundTasks(), user=TENANT_USER,
                                          cache=Cache(LITERAL), memory=Memory(), llm=SimpleNamespace())
        sent = await asgi(response)
        # The full answer went out and the response finished; evidence IO has not completed.
        assert sent[0]["type"] == "http.response.start"
        bodies = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
        assert json.loads(bodies.splitlines()[0])["content"] == LITERAL
        assert sent[-1] == {"type": "http.response.body", "body": b"", "more_body": False}
        await asyncio.sleep(0.01)
        assert repo.enqueued == [] and runtime.pending_count == 1
        repo.release.set()
        store.release.set()
        await runtime.close(time.monotonic() + 2)
    asyncio.run(run())
    [envelope] = repo.enqueued
    stored = [e for e in envelope.manifest if e.state == 'stored']
    assert [e.slot.kind.value for e in stored] == ['input']
    assert b'fixture-secret' not in store.puts[0].data and b'[REDACTED' in store.puts[0].data


def paths():
    return ["normal", "cache", "error", "timeout", "cancel", "http.response.start", "http.response.body"]


@pytest.mark.parametrize("path", paths())
def test_every_exit_path_admits_exactly_once(route, install, monkeypatch, path):
    chat, _, _ = route
    runtime = install(counting_runtime(config(capture=False)))
    type(runtime).admitted = type(runtime).submitted = 0
    graph = Graph(error={"error": RuntimeError("private"), "timeout": httpx.ReadTimeout("private")}.get(path))
    monkeypatch.setattr(chat, "agent_app", graph)
    cache = Cache("cached answer" if path in ("cache", "http.response.start", "http.response.body") else None)

    async def run():
        await runtime.start()
        response = await chat.chat_stream(chat.ChatRequest(message="question"), BackgroundTasks(),
                                          user=TENANT_USER, cache=cache, memory=Memory(), llm=SimpleNamespace())
        if path.startswith("http."):
            await asgi(response, disconnect_on=path)
        elif path == "cancel":
            iterator = response.body_iterator
            await anext(iterator)
            await iterator.aclose()
            response.on_close()  # ASGI close after generator close is still one admission
        else:
            [e async for e in response.body_iterator]
            response.on_close()
        await runtime.close(time.monotonic() + 1)
        return runtime
    asyncio.run(run())
    assert type(runtime).admitted == type(runtime).submitted == 1
    expected = {"normal": "success", "cache": "success", "error": "error", "timeout": "timeout"}.get(path, "cancelled")
    assert runtime.admitted_events[0].status == expected


def test_capture_is_bound_only_while_the_generator_advances(route, install, monkeypatch):
    from services.api.app.evaluation.capture import current_capture
    chat, _, _ = route
    runtime = install(counting_runtime(config()))
    graph = Graph()
    monkeypatch.setattr(chat, "agent_app", graph)

    async def run():
        await runtime.start()
        response = await chat.chat_stream(chat.ChatRequest(message=LITERAL), BackgroundTasks(), user=TENANT_USER,
                                          cache=Cache(), memory=Memory(), llm=SimpleNamespace())
        async for _ in response.body_iterator:
            assert current_capture.get() is None
        await runtime.close(time.monotonic() + 1)
    asyncio.run(run())
    assert graph.captures[0] is not None and graph.captures[0] is graph.captures[1]
    assert runtime._pool.active_runs == 0 and runtime._pool.resident_bytes == 0


def test_stream_disconnect_finalizes_recorded_operations_once_and_resets_ports(route, install, monkeypatch):
    from libs.observability import tracing
    from services.api.app.evaluation.operation_schemas import register_api_operation_schemas
    chat, _, _ = route
    register_api_operation_schemas()
    runtime = install(counting_runtime(config(capture=False)))
    type(runtime).admitted = type(runtime).submitted = 0

    class RecordingGraph:
        async def astream(self, state, config):
            with tracing.domain_span('llm.chat', schema_version=1) as op:
                op.record('request', facts={'request_hash': 'sha256:' + 'a' * 64})
                op.record('result', facts={'status': 'success', 'input_tokens': 1, 'output_tokens': 1})
            yield {'planner': {'action': 'direct_answer'}}

    monkeypatch.setattr(chat, 'agent_app', RecordingGraph())

    async def run():
        await runtime.start()
        response = await chat.chat_stream(chat.ChatRequest(message='question'), BackgroundTasks(),
            user=TENANT_USER, cache=Cache(), memory=Memory(), llm=SimpleNamespace())
        iterator = response.body_iterator
        first = await anext(iterator)
        assert json.loads(first)['run_id'] == response.headers['X-Run-ID']
        await iterator.aclose()
        response.on_close()
        await runtime.close(time.monotonic() + 1)
        return response

    response = asyncio.run(run())
    assert response.evidence.status == 'cancelled'
    assert [record.phase for record in response.evidence.operation_events] == ['request', 'result']
    assert len(response.evidence.model_calls) == 1
    assert type(runtime).admitted == type(runtime).submitted == 1
    assert tracing.record_observer.get() is None
    assert tracing.provenance_port.get() is None


@pytest.mark.parametrize("failure", ["open", "admit", "submit"])
def test_runtime_failure_does_not_alter_chat_or_agent_status(route, install, monkeypatch, failure):
    chat, _, _ = route
    runtime = install(counting_runtime(config()))

    def boom(*args, **kwargs):
        raise RuntimeError("runtime broke")
    monkeypatch.setattr(runtime, {"open": "open_capture", "admit": "admit", "submit": "submit"}[failure], boom)
    memory = Memory()

    async def run():
        await runtime.start()
        response = await chat.chat_stream(chat.ChatRequest(message=LITERAL), BackgroundTasks(), user=TENANT_USER,
                                          cache=Cache("answer"), memory=memory, llm=SimpleNamespace())
        events = [json.loads(e) async for e in response.body_iterator]
        await runtime.close(time.monotonic() + 1)
        return response, events
    response, events = asyncio.run(run())
    assert [e["type"] for e in events] == ["answer"]
    assert response.evidence.status == "success"
    assert len(memory.writes) == 2
    assert runtime._pool.active_runs == 0


def test_disabled_runtime_binds_no_capture_and_changes_nothing(route, install, monkeypatch):
    chat, _, _ = route
    install(None)
    graph = Graph()
    monkeypatch.setattr(chat, "agent_app", graph)
    memory = Memory()

    async def run():
        response = await chat.chat_stream(chat.ChatRequest(message="question"), BackgroundTasks(),
                                          user=TENANT_USER, cache=Cache(), memory=memory, llm=SimpleNamespace())
        return response, [json.loads(e) async for e in response.body_iterator]
    response, events = asyncio.run(run())
    assert graph.captures == [None, None]
    assert [e["type"] for e in events] == ["routing", "status", "status", "answer"]
    assert response.evidence.status == "success" and len(memory.writes) == 2
