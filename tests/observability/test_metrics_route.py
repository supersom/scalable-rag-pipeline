"""The /metrics route serves the process-wide Prometheus registry, unauthenticated (cluster-internal scrape only)."""
from fastapi.testclient import TestClient


def test_metrics_route_serves_the_prometheus_registry(route, monkeypatch):
    monkeypatch.setenv("EVAL_EVIDENCE_ENABLED", "false")
    from services.api.main import app
    # No `with` block: entering TestClient as a context manager runs the full
    # app lifespan (Neo4j/Redis/Postgres/Qdrant/LLM clients, evidence runtime),
    # which this test environment can't satisfy. /metrics reads only the
    # process-wide Prometheus registry, so a bare TestClient (lifespan never
    # starts) is enough to exercise the route.
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert "# HELP" in response.text
