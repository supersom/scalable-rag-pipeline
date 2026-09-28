import asyncio
import json

import httpx
import pytest
from uuid import uuid4
from opentelemetry import trace
from opentelemetry.trace import StatusCode
from qdrant_client.http.models import ScoredPoint


def test_tool_domain_records_preserve_specialized_trajectory(route):
    from services.api.app.agents.nodes.tool import tool_node
    from services.api.app.evaluation.context import RunEvidenceCollector
    from services.api.app.evaluation.instrumentation_adapter import bind_instrumentation
    from services.api.app.evaluation.models import ConfigurationFingerprint

    collector = RunEvidenceCollector(run_id=str(uuid4()), session_id='s', input_text='2+6',
                                     configuration=ConfigurationFingerprint())
    async def run():
        with bind_instrumentation(collector, None):
            return await tool_node({'tool_name': 'calculator', 'tool_input': '2+6'})
    assert asyncio.run(run())['documents'] == ['8']
    event = collector.finish('success')
    assert [part.phase for part in event.operation_events] == ['request', 'result']
    assert len(event.trajectory) == 1
    assert event.trajectory[0].tool_name == 'calculator'


@pytest.mark.parametrize("expression,status", [("1/0", "error"), ("2+6", "success")])
def test_real_calculator_failure_is_not_a_successful_tool_span(route, expression, status):
    from services.api.app.agents.nodes.tool import tool_node
    _, _, exporter = route
    result = asyncio.run(tool_node({"tool_name": "calculator", "tool_input": expression}))
    assert result["documents"] == (["8"] if status == "success" else ["Error: division by zero"])
    spans = [s for s in exporter.get_finished_spans() if s.name == "tool.execute"]
    assert len(spans) == 1
    assert spans[0].attributes["operation.status"] == status
    assert spans[0].attributes["tool.name"] == "calculator"
    assert "division by zero" not in str(spans[0].attributes) + str(spans[0].events)


def test_disabled_web_search_is_explicit_not_success(route, monkeypatch):
    from services.api.app.agents.nodes.tool import tool_node
    _, _, exporter = route
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    result = asyncio.run(tool_node({"tool_name": "web_search", "tool_input": "private query"}))
    assert "disabled" in result["documents"][0]
    [span] = exporter.get_finished_spans()
    assert span.attributes["operation.status"] == "disabled"
    assert "private query" not in str(span.attributes)


@pytest.mark.parametrize("tool_name", ["web_search", "sandbox", "graph_search"])
def test_external_tool_failure_marks_tool_span_without_payloads(route, monkeypatch, tool_name):
    from services.api.app.agents.nodes.tool import tool_node
    from services.api.app.tools import graph_search
    from services.api.app.clients.ray_llm import RayLLMClient
    _, _, exporter = route
    monkeypatch.setenv("TAVILY_API_KEY", "private-api-key")
    client_type = httpx.AsyncClient
    def fail(request): raise httpx.ReadTimeout("private secret")
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client_type(transport=httpx.MockTransport(fail)))
    llm = RayLLMClient()
    llm.client = client_type(transport=httpx.MockTransport(lambda r: httpx.Response(503, json={})))
    monkeypatch.setattr(graph_search, "llm_client", llm)
    async def run():
        try:
            return await tool_node({"tool_name": tool_name, "tool_input": "private query"})
        finally:
            await llm.close()
    result = asyncio.run(run())
    assert result["documents"]
    span = next(s for s in exporter.get_finished_spans() if s.name == "tool.execute")
    assert span.attributes["operation.status"] == ("error" if tool_name == "graph_search" else "timeout")
    assert "private" not in str(span.attributes) + str(span.events)


def test_graph_tool_extracts_entities_and_runs_parameterized_query(route, monkeypatch):
    from services.api.app.tools import graph_search
    from services.api.app.agents.nodes.tool import tool_node
    from services.api.app.clients.ray_llm import RayLLMClient
    _, _, exporter = route
    client = RayLLMClient()
    seen = []
    async def query(cypher, parameters):
        seen.append(parameters)
        return [{"source": "Alice", "rel": "KNOWS", "target": "Bob"}]
    monkeypatch.setattr(graph_search.neo4j_client, "query", query)
    monkeypatch.setattr(graph_search, "llm_client", client)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": '{"entities": ["Alice"]}'}}],
        }))) as client.client:
            return await tool_node({"tool_name": "graph_search", "tool_input": "private query"})
    result = asyncio.run(run())
    assert "Bob" in result["documents"][0]
    assert seen == [{"names": ["Alice"]}]
    span = next(s for s in exporter.get_finished_spans() if s.name == "tool.execute")
    assert span.attributes["operation.status"] == "success"


def test_planner_does_not_export_unknown_model_tool_name(route, monkeypatch):
    from services.api.app.agents.nodes import planner
    from services.api.app.clients.ray_llm import RayLLMClient
    _, _, exporter = route
    client = RayLLMClient()
    monkeypatch.setattr(planner, "llm_client", client)
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
            "choices": [{"message": {"content": '{"action":"retrieve","tool_name":"private prompt echoed here"}'}}],
        }))) as client.client:
            return await planner.planner_node({"messages": [{"content": "private prompt"}]})
    assert asyncio.run(run())["action"] == "retrieve"
    planner_span = next(s for s in exporter.get_finished_spans() if s.name == "planner")
    assert "tool.name" not in planner_span.attributes
    assert "private" not in str([s.attributes for s in exporter.get_finished_spans()])
