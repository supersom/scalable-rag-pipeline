import asyncio

import httpx
import pytest


def test_completed_graph_evidence_survives_vector_failure(route, monkeypatch):
    from services.api.app.agents.nodes import retriever
    from services.api.app.evaluation.context import RunEvidenceCollector
    from services.api.app.evaluation.instrumentation_adapter import bind_instrumentation
    from services.api.app.evaluation.operation_schemas import register_api_operation_schemas
    register_api_operation_schemas()
    from services.api.app.evaluation.models import ConfigurationFingerprint
    graph_done = asyncio.Event()
    async def embed(query): return [1.]
    async def graph(query, parameters):
        graph_done.set()
        return [{"text": "private evidence", "relationship_id": "rel-1", "score": .8}]
    async def vector(**kwargs):
        await graph_done.wait()
        raise httpx.ReadTimeout("private secret")
    monkeypatch.setattr(retriever.embed_client, "embed_query", embed)
    monkeypatch.setattr(retriever.neo4j_client, "query", graph)
    monkeypatch.setattr(retriever.qdrant_client, "search", vector)
    c = RunEvidenceCollector(run_id="r", session_id="s", input_text="q",
                             configuration=ConfigurationFingerprint())
    async def run():
        with bind_instrumentation(c, None), pytest.raises(httpx.ReadTimeout):
            await retriever.retrieve_context("private query")
    asyncio.run(run())
    event = c.finish("timeout")
    assert len(event.retrieval) == 1
    assert [record.phase for record in event.operation_events] == ['request', 'hit', 'result']
    evidence = event.retrieval[0]
    assert evidence.vector_status == "timeout"
    assert evidence.graph_status == "success"
    assert evidence.items[0].chunk_id == "rel-1"
    assert "private" not in event.model_dump_json()
