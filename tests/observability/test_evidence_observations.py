import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.sampling import ALWAYS_OFF


def test_unsampled_dependencies_keep_failure_and_outcome_evidence(route, monkeypatch):
    from libs.observability import tracing
    from services.api.app.evaluation.context import bind_collector, RunEvidenceCollector
    from services.api.app.evaluation.models import ConfigurationFingerprint
    provider = TracerProvider(sampler=ALWAYS_OFF)
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("unsampled"))
    c = RunEvidenceCollector(run_id="r", session_id="s", input_text="q",
                             configuration=ConfigurationFingerprint())
    with bind_collector(c):
        with tracing.domain_span("cache.lookup"):
            tracing.mark_failure(tracing.trace.get_current_span(), RuntimeError("private secret"))
    event = c.finish("success")
    assert len(event.dependencies) == 1
    assert event.dependencies[0].status == "error"
    assert event.dependencies[0].error_type == "RuntimeError"
    provider.shutdown()
