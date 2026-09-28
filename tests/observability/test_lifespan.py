import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

@pytest.mark.parametrize("startup_fails", [False, True])
def test_real_app_lifespan_flushes_spans_even_when_startup_fails(route, monkeypatch, startup_fails):
    from services.api.app import observability
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=60000))
    monkeypatch.setattr(observability, "configure_tracing", lambda *args, **kwargs: provider)
    from services.api import main
    monkeypatch.setattr(main.app.state, "tracer_provider", provider)
    monkeypatch.setattr(main, "neo4j_client", SimpleNamespace(connect=Mock(), close=AsyncMock()))
    monkeypatch.setattr(main, "redis_client", SimpleNamespace(connect=AsyncMock(), close=AsyncMock()))
    for name in ("llm_client", "embed_client"):
        monkeypatch.setattr(main, name, SimpleNamespace(start=AsyncMock(), close=AsyncMock()))
    @asynccontextmanager
    async def begin():
        yield SimpleNamespace(run_sync=AsyncMock())
    monkeypatch.setattr(main, "engine", SimpleNamespace(begin=begin))
    async def ensure_collection():
        with provider.get_tracer("test").start_as_current_span("startup-probe"):
            pass
        if startup_fails:
            raise RuntimeError("startup failed")
    monkeypatch.setattr(main, "semantic_cache", SimpleNamespace(ensure_collection=ensure_collection))
    async def run():
        async with main.lifespan(main.app):
            assert not exporter.get_finished_spans()  # still batched before shutdown
    if startup_fails:
        with pytest.raises(RuntimeError, match="startup failed"):
            asyncio.run(run())
    else:
        asyncio.run(run())
    assert [s.name for s in exporter.get_finished_spans()] == ["startup-probe"]


def _stub_clients(main, monkeypatch, closed):
    monkeypatch.setattr(main, "neo4j_client", SimpleNamespace(connect=Mock(), close=AsyncMock(side_effect=lambda: closed.append("neo4j"))))
    monkeypatch.setattr(main, "redis_client", SimpleNamespace(connect=AsyncMock(), close=AsyncMock()))
    for name in ("llm_client", "embed_client"):
        monkeypatch.setattr(main, name, SimpleNamespace(start=AsyncMock(), close=AsyncMock()))
    @asynccontextmanager
    async def begin():
        yield SimpleNamespace(run_sync=AsyncMock())
    monkeypatch.setattr(main, "engine", SimpleNamespace(begin=begin))
    monkeypatch.setattr(main, "semantic_cache", SimpleNamespace(ensure_collection=AsyncMock()))
    monkeypatch.setattr(main, "shutdown_observability", AsyncMock(side_effect=lambda app: closed.append("observability")))


@pytest.mark.parametrize("evidence_fails", [False, True])
def test_lifespan_owns_the_evidence_runtime(route, monkeypatch, evidence_fails):
    from services.api import main
    from services.api.app.evaluation import runtime as runtime_module
    closed = []
    _stub_clients(main, monkeypatch, closed)

    class Runtime:
        config = SimpleNamespace(shutdown_drain_seconds=10)

        async def close(self, deadline):
            assert runtime_module.active_runtime() is None  # admission stops before the drain
            closed.append("evidence")
            return 0
    started = Runtime()

    async def start(config, settings):
        assert config.enabled is False  # defaults stay disabled; the factory decides allocation
        if evidence_fails:
            raise RuntimeError("evidence startup failed")
        return started
    monkeypatch.setattr(main, "start_evidence_runtime", start)

    async def run():
        async with main.lifespan(main.app):
            assert runtime_module.active_runtime() is started
    if evidence_fails:
        with pytest.raises(RuntimeError, match="evidence startup failed"):
            asyncio.run(run())
        assert closed == ["neo4j", "observability"]
    else:
        asyncio.run(run())
        assert closed == ["evidence", "neo4j", "observability"]
    assert runtime_module.active_runtime() is None


def test_default_lifespan_allocates_no_evidence_runtime(route, monkeypatch):
    from services.api import main
    from services.api.app.evaluation import runtime as runtime_module
    _stub_clients(main, monkeypatch, [])
    monkeypatch.setattr(runtime_module, "create_async_engine", Mock(side_effect=AssertionError("no engine")))

    async def run():
        async with main.lifespan(main.app):
            assert runtime_module.active_runtime() is None
    asyncio.run(run())
