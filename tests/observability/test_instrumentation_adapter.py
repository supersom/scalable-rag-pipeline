"""The API adapter is the only bridge from domain records to run evidence."""

from uuid import uuid4
from pathlib import Path

import pytest


@pytest.fixture
def policy():
    from services.api.app.evaluation.delivery_models import ExportPolicy
    from services.api.app.evaluation.provenance import SyntheticManifest

    manifest = SyntheticManifest.load(Path(__file__).parents[2] / "deploy/evaluation/restricted-synthetic-v1.json")
    return ExportPolicy(identity_key="dedicated-synthetic-evidence-key", environment="local",
                        allowed_models=("fake-chat",), synthetic_sources=manifest.sources)


def _collector(run_id):
    from services.api.app.evaluation.context import RunEvidenceCollector
    from services.api.app.evaluation.models import ConfigurationFingerprint

    return RunEvidenceCollector(
        run_id=str(run_id), session_id="synthetic-session", tenant_id="synthetic-tenant",
        input_text="synthetic question", configuration=ConfigurationFingerprint(generation_model="fake-chat"),
    )


def test_unsampled_model_records_update_generic_and_specialized_evidence(monkeypatch):
    from libs.observability import tracing
    from services.api.app.evaluation.instrumentation_adapter import bind_instrumentation
    from services.api.app.evaluation.operation_schemas import register_api_operation_schemas

    register_api_operation_schemas()
    provider = tracing.create_tracer_provider("adapter-test", sampler_name="always_off")
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("test.adapter"))
    collector = _collector(uuid4())
    try:
        with bind_instrumentation(collector, None):
            with tracing.domain_span("llm.chat", schema_version=1) as op:
                op.record("request", facts={
                    "purpose": "generation", "request_hash": "sha256:" + "a" * 64,
                    "requested_model": "fake-chat",
                })
                op.record("result", facts={
                    "model": "fake-chat", "status": "success", "input_tokens": 4,
                    "output_tokens": 2, "retry_count": 1,
                })
        event = collector.finish("success")
    finally:
        provider.shutdown()
    assert len(event.operation_events) == 2
    assert len(event.model_calls) == 1
    assert event.model_calls[0].request_hash == "sha256:" + "a" * 64
    assert (event.model_calls[0].input_tokens, event.model_calls[0].output_tokens) == (4, 2)
    assert event.model_calls[0].retry_count == 1
    assert event.operations.input_tokens == 4


def test_unapproved_reviewer_content_is_omitted_with_safe_metadata(policy):
    from libs.observability import tracing
    from libs.observability.operation_contracts import (
        FieldRule, OperationSchema, PhaseRule, register_operation_schema,
    )
    from services.api.app.evaluation.capture import CapturePool
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    from services.api.app.evaluation.instrumentation_adapter import bind_instrumentation

    register_operation_schema(OperationSchema("test.adapter.review", 1, {
        "result": PhaseRule({"verdict": FieldRule(str, frozenset({"otel_safe"}), max_length=16)},
                            frozenset({"rationale"})),
    }))
    run_id = uuid4()
    collector = _collector(run_id)
    config = DeliveryConfig(
        enabled=True, capture_content=True, environment="local", database_url="postgresql://local/test",
        queue_url="http://localhost/queue", identity_key="evidence-only",
        content_policy="restricted-synthetic-v1", fixture_manifest="local-fixture.json",
        fixture_case="synthetic-question", bucket="local-bucket", kms_key_id="local-key",
        s3_endpoint_url="http://localhost:5000",
    )
    capture = CapturePool(config, policy).open(run_id, "synthetic-tenant", "synthetic-question")
    with bind_instrumentation(collector, capture):
        with tracing.domain_span("test.adapter.review", schema_version=1) as op:
            op.record("result", facts={"verdict": "pass"},
                      content={"rationale": op.content("private rationale", provenance=None)})
    event = collector.finish("success")
    assert event.operation_events[0].facts == {"verdict": "pass"}
    assert "rationale" in event.operation_events[0].content_slots
    assert capture.unavailable[0].reason == "unapproved_source"
    prepared = capture.detach(event).prepared
    assert prepared.safe_run.operation_events[0].facts["verdict"] == "pass"
    assert not prepared.payloads
    assert prepared.unavailable[0].reason == "unapproved_source"


def test_binding_resets_ports_after_exception():
    from libs.observability import tracing
    from services.api.app.evaluation.instrumentation_adapter import bind_instrumentation

    with pytest.raises(RuntimeError):
        with bind_instrumentation(_collector(uuid4()), None):
            assert tracing.record_observer.get() is not None
            raise RuntimeError("abort")
    assert tracing.record_observer.get() is None
    assert tracing.provenance_port.get() is None


def test_schema_registration_is_idempotent():
    from libs.observability.operation_contracts import get_operation_schema
    from services.api.app.evaluation.operation_schemas import register_api_operation_schemas

    register_api_operation_schemas()
    register_api_operation_schemas()
    assert get_operation_schema("retrieval", 1) is not None
    assert get_operation_schema("llm.chat", 1) is not None
    assert get_operation_schema("tool.execute", 1) is not None
