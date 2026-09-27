import asyncio

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF


@pytest.mark.parametrize("operation", ["planner", "sandbox"])
def test_sdk_attribute_updates_survive_unsampled_operations(route, monkeypatch, operation):
    from libs.observability import tracing
    from services.api.app.agents.nodes import planner
    from services.api.app.tools.sandbox import run_python_code
    from services.api.app.evaluation.context import bind_collector, RunEvidenceCollector
    from services.api.app.evaluation.models import ConfigurationFingerprint
    provider = TracerProvider(sampler=ALWAYS_OFF)
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("unsampled"))
    c = RunEvidenceCollector(run_id="r", session_id="s", input_text="q", configuration=ConfigurationFingerprint())
    async def fail(**kwargs): raise ValueError("private secret")
    monkeypatch.setattr(planner.llm_client, "chat_completion_result", fail)
    client_type = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(
        transport=httpx.MockTransport(lambda r: httpx.Response(429))))
    async def run():
        with bind_collector(c):
            if operation == "planner":
                await planner.planner_node({"messages": [{"content": "q"}]})
            else:
                with tracing.domain_span("tool.execute"):
                    await run_python_code("q")
    try:
        asyncio.run(run())
        event = c.finish("success")
        if operation == "planner":
            assert event.dependencies[0].attributes["planner.fallback"] is True
        else:
            assert event.operations.provider_throttled is True
    finally:
        provider.shutdown()
