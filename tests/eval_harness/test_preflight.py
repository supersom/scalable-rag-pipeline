import asyncio

from eval.harness.preflight import run_preflight


def probes(**over):
    async def llm():
        return None

    async def embed():
        return 768

    async def qdrant():
        return {"exists": True, "points": 100, "vector_size": 768, "filenames": {"a.pdf", "b.pdf"}}

    async def neo4j():
        return 5

    base = {"llm": llm, "embed": embed, "qdrant": qdrant, "neo4j": neo4j}
    base.update(over)
    return base


def failing(exc):
    async def fn():
        raise exc
    return fn


def returning(value):
    async def fn():
        return value
    return fn


def preflight(layer="retrieval", sources=("a.pdf",), **over):
    return asyncio.run(run_preflight(layer=layer, embed_dim=768, expected_sources=set(sources), probes=probes(**over)))


def test_routing_needs_only_the_llm():
    result = preflight("routing", qdrant=failing(RuntimeError("must not be probed")), embed=failing(RuntimeError("x")))
    assert result.ok and result.errors == []


def test_routing_reports_llm_failure_with_fix():
    result = preflight("routing", llm=failing(RuntimeError("401")))
    assert not result.ok and "RAY_LLM_ENDPOINT" in result.errors[0] and "401" in result.errors[0]


def test_retrieval_ok_records_facts():
    result = preflight()
    assert result.ok and result.warnings == []
    assert result.facts["qdrant_points"] == 100 and result.facts["neo4j_nodes"] == 5
    assert result.facts["embed_dim_actual"] == 768 and result.facts["missing_sources"] == []


def test_embed_dimension_must_match_setting():
    result = preflight(embed=returning(1024))
    assert any("EMBED_DIM=768" in e and "1024" in e for e in result.errors)


def test_embed_endpoint_failure():
    result = preflight(embed=failing(TimeoutError("slow")))
    assert any("Embed endpoint failed" in e for e in result.errors)


def test_missing_collection_gives_create_command():
    result = preflight(qdrant=returning({"exists": False, "points": 0, "vector_size": None, "filenames": set()}))
    assert any("PUT" in e and "rag_collection" in e for e in result.errors)


def test_vector_size_mismatch():
    result = preflight(qdrant=returning({"exists": True, "points": 5, "vector_size": 1024, "filenames": {"a.pdf"}}))
    assert any("vector size 1024" in e for e in result.errors)


def test_empty_collection():
    result = preflight(qdrant=returning({"exists": True, "points": 0, "vector_size": 768, "filenames": set()}))
    assert any("empty" in e for e in result.errors)


def test_qdrant_unreachable():
    result = preflight(qdrant=failing(ConnectionError("refused")))
    assert any("Qdrant unreachable" in e and "docker compose up -d qdrant" in e for e in result.errors)


def test_missing_sources_are_warnings_not_errors():
    result = preflight(sources=("a.pdf", "gone.pdf"))
    assert result.ok and result.facts["missing_sources"] == ["gone.pdf"]
    assert any("gone.pdf" in w for w in result.warnings)


def test_neo4j_unavailable_and_empty_are_warnings():
    down = preflight(neo4j=failing(ConnectionError("refused")))
    assert down.ok and down.facts["neo4j_nodes"] is None and any("Neo4j unavailable" in w for w in down.warnings)
    empty = preflight(neo4j=returning(0))
    assert empty.ok and any("empty" in w for w in empty.warnings)
