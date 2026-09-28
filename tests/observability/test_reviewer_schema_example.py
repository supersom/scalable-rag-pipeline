"""A feature module can add rich records without adding core tracing methods."""

from pathlib import Path
from uuid import uuid4

from libs.observability.operation_contracts import (
    FieldRule, OperationSchema, PhaseRule, register_operation_schema,
)


def test_external_reviewer_schema_uses_domain_record_without_exporting_rationale(route, monkeypatch):
    from libs.observability import tracing
    from services.api.app.evaluation.context import RunEvidenceCollector
    from services.api.app.evaluation.instrumentation_adapter import bind_instrumentation
    from services.api.app.evaluation.capture import CapturePool
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    from services.api.app.evaluation.delivery_models import ExportPolicy
    from services.api.app.evaluation.models import ConfigurationFingerprint
    from services.api.app.evaluation.provenance import SyntheticManifest

    schema = OperationSchema('reviewer.review', 1, {
        'result': PhaseRule({
            'verdict': FieldRule(str, frozenset({'otel_safe', 'evidence_only'}), max_length=16),
        }, frozenset({'rationale'})),
    })
    register_operation_schema(schema)
    _, provider, exporter = route
    monkeypatch.setattr(tracing, 'domain_tracer', provider.get_tracer('reviewer.example'))
    run_id = uuid4()
    collector = RunEvidenceCollector(run_id=str(run_id), session_id='review-session', input_text='q',
                                     configuration=ConfigurationFingerprint())
    manifest = SyntheticManifest.load(Path(__file__).parents[2] / 'deploy/evaluation/restricted-synthetic-v1.json')
    policy = ExportPolicy(identity_key='review-key', environment='local',
                          allowed_models=('fake-chat',), synthetic_sources=manifest.sources)
    config = DeliveryConfig(enabled=True, capture_content=True, environment='local',
        database_url='postgresql://local/test', queue_url='http://localhost/queue', identity_key='review-key',
        content_policy='restricted-synthetic-v1', fixture_manifest='local.json', fixture_case='case',
        bucket='local-bucket', kms_key_id='local-key', s3_endpoint_url='http://localhost:5000')
    capture = CapturePool(config, policy).open(run_id, 'synthetic-tenant', 'case')

    with bind_instrumentation(collector, capture):
        with tracing.domain_span('reviewer.review', schema_version=1) as op:
            op.record('result', facts={'verdict': 'pass'}, content={
                'rationale': op.content('private rationale', provenance=None),
            })
    event = collector.finish('success')
    span = next(item for item in exporter.get_finished_spans() if item.name == 'reviewer.review')
    assert event.operation_events[-1].name == 'reviewer.review'
    assert event.operation_events[-1].facts == {'verdict': 'pass'}
    assert 'rationale' in event.operation_events[-1].content_slots
    assert 'private rationale' not in str(span.attributes) + str(span.events)
    assert 'private rationale' not in event.model_dump_json()
    prepared = capture.detach(event).prepared
    assert not prepared.payloads
    assert prepared.unavailable[0].reason == 'unapproved_source'
    assert prepared.safe_run.operation_events[-1].content_slots['rationale'].kind.value == 'operation_content'
