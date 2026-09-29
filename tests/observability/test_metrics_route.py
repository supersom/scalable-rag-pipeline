"""The /metrics route serves the process-wide Prometheus registry, unauthenticated (cluster-internal scrape only)."""
import re

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


def test_metrics_route_serves_evidence_delivery_metrics(route, monkeypatch):
    """`default_metrics()` registers evidence_delivery_* series onto the same global
    REGISTRY `/metrics` reads, independently of whether `lifespan` ever ran (it's a
    process-wide singleton — see `default_metrics()` in delivery_metrics.py). Record
    one event directly and confirm the route actually surfaces it, not just the
    prometheus_client default collectors (PROCESS_COLLECTOR/PLATFORM_COLLECTOR/GC_COLLECTOR),
    which are what the test above proves on their own."""
    monkeypatch.setenv("EVAL_EVIDENCE_ENABLED", "false")
    from services.api.app.evaluation.delivery_metrics import default_metrics
    from services.api.main import app
    # "maintenance"/"cleaned" is a combination no other test in the suite increments on
    # the global registry (grep for `default_metrics()` in tests/ confirmed this), so this
    # assertion can't pass due to leakage from another test in the same process.
    default_metrics().inc("maintenance", "cleaned")
    client = TestClient(app)
    response = client.get("/metrics")
    assert response.status_code == 200
    match = re.search(
        r'evidence_delivery_events_total\{component="maintenance",outcome="cleaned",reason="none"\} (\S+)',
        response.text)
    assert match is not None, "evidence_delivery_events_total series missing from /metrics response"
    # >= 1 rather than == 1: this counter lives on the process-wide global REGISTRY, so a
    # value of exactly 1 isn't guaranteed if this test process re-runs the module (e.g. reruns/repeat
    # plugins) — what matters here is that the route surfaces the series this test just recorded.
    assert float(match.group(1)) >= 1
