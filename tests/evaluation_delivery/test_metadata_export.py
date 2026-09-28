import pytest
from pydantic import ValidationError

from services.api.app.evaluation.delivery_models import ExportEnvelope, PayloadSlot, RedactedPayload
from services.api.app.evaluation.metadata_export import project_metadata, prepare_export, seal_export


def test_projection_preserves_original_collector_snapshot(terminal_event, policy):
    before = terminal_event.model_dump()
    project_metadata(terminal_event, policy, ())
    assert terminal_event.model_dump() == before


def test_projection_pseudonymizes_identifiers(terminal_event, policy):
    safe = project_metadata(terminal_event, policy, ())
    assert safe.session_pseudonym != terminal_event.session_id
    assert safe.input_digest_pseudonym != terminal_event.input_hash
    assert safe.session_pseudonym.startswith('hmac-sha256:')


def test_user_pseudonym_always_null_even_when_collector_hash_looks_valid(terminal_event, policy):
    # terminal_event.user_id_hash is a well-formed hmac-sha256 hex string computed with the
    # SAME key string the policy fixture happens to use, but project_metadata has no way to
    # prove that binding in general (the collector and the export policy are configured
    # independently in production). Exporting it on format + non-empty-key alone would let a
    # pseudonym computed with a *different* key pass through undetected, so it must stay null
    # until a verifiable key-fingerprint binding exists (see Task 2 review notes).
    assert terminal_event.user_id_hash is not None
    safe = project_metadata(terminal_event, policy, ())
    assert safe.user_pseudonym is None


def test_user_pseudonym_null_when_not_derived(policy):
    from uuid import uuid4
    from services.api.app.evaluation.context import RunEvidenceCollector
    from services.api.app.evaluation.models import ConfigurationFingerprint
    event = RunEvidenceCollector(
        run_id=str(uuid4()), session_id='s1', tenant_id='synthetic-tenant', input_text='hi',
        configuration=ConfigurationFingerprint(generation_model='fake-chat', embedding_model='fake-embed'),
    ).finish('success')
    safe = project_metadata(event, policy, ())
    assert event.user_id_hash is None
    assert safe.user_pseudonym is None


def test_known_collector_unavailable_reasons_propagate(terminal_event, policy):
    safe = project_metadata(terminal_event, policy, ())
    assert set(terminal_event.unavailable) <= {
        'payloads_not_stored', 'corpus_index_acl_versions_not_configured',
        'cost_not_calculated', 'identity_hash_key_not_configured'}
    # The payload manifest is authoritative; the collector's legacy tag would contradict stored references.
    assert 'payloads_not_stored' in terminal_event.unavailable
    assert 'payloads_not_stored' not in safe.unavailable
    assert set(terminal_event.unavailable) - {'payloads_not_stored'} <= set(safe.unavailable)


def test_no_tenant_hashes_all_null(terminal_event, policy):
    absent = project_metadata(terminal_event.model_copy(update={'tenant_id': None}), policy, ())
    assert absent.tenant_partition is None
    assert absent.session_pseudonym is None
    assert absent.input_digest_pseudonym is None
    assert absent.output_digest_pseudonym is None
    assert absent.user_pseudonym is None
    assert 'identity_unavailable' in absent.unavailable


def test_cross_tenant_pseudonyms_differ(terminal_event, policy):
    a = project_metadata(terminal_event, policy, ())
    b = project_metadata(terminal_event.model_copy(update={'tenant_id': 'synthetic-tenant-2'}), policy, ())
    assert a.session_pseudonym != b.session_pseudonym
    assert a.tenant_partition != b.tenant_partition
    assert a.input_digest_pseudonym != b.input_digest_pseudonym


def test_missing_identity_key_nulls_pseudonyms(terminal_event, policy):
    unkeyed = policy.model_copy(update={'identity_key': ''})
    safe = project_metadata(terminal_event, unkeyed, ())
    assert safe.session_pseudonym is None
    assert safe.tenant_partition is None
    assert 'identity_unavailable' in safe.unavailable


def test_known_field_and_attribute_secret_path_strings_denied(rich_event, policy):
    safe = project_metadata(rich_event, policy, (1,))
    assert safe.configuration.corpus_version is None
    assert safe.retrieval[0].collection is None
    assert safe.model_calls[0].model is None
    assert safe.trajectory[0].tool_name is None
    assert 'tool.name' not in safe.dependencies[0].attributes
    assert safe.dependencies[0].attributes['cache.hit'] is True
    assert safe.dependencies[0].name == 'cache'


def test_raw_provider_and_completion_ids_never_exported(rich_event, policy):
    safe = project_metadata(rich_event, policy, (1,))
    call = safe.model_calls[0]
    raw = rich_event.model_calls[0]
    assert call.provider_request_pseudonym != raw.provider_request_id
    assert call.completion_pseudonym != raw.completion_id
    assert call.provider_request_pseudonym.startswith('hmac-sha256:')
    assert call.completion_pseudonym.startswith('hmac-sha256:')
    assert call.error_class == 'OtherError'
    assert call.ordinal == 1


def test_model_ordinal_mapping_required(rich_event, policy):
    with pytest.raises(ValueError):
        project_metadata(rich_event, policy, ())
    with pytest.raises(ValueError):
        project_metadata(rich_event, policy, (1, 1))


def test_prepare_export_metadata_only_when_no_payloads(terminal_event, policy):
    envelope = seal_export(prepare_export(terminal_event, policy, (), ()), ())
    assert envelope.completeness == 'metadata_only'


def test_prepare_export_payload_count_exceeded(terminal_event, policy):
    limited = policy.model_copy(update={'payload_slots': 1, 'run_bytes': 10_000, 'payload_bytes': 10_000})
    p1 = RedactedPayload(slot=PayloadSlot(kind='retrieval_chunk', ordinal=1), data=b'a' * 10, policy_version=limited.redaction_policy_version)
    p2 = RedactedPayload(slot=PayloadSlot(kind='retrieval_chunk', ordinal=2), data=b'b' * 10, policy_version=limited.redaction_policy_version)
    prepared = prepare_export(terminal_event, limited, (p1, p2), ())
    assert p1 in prepared.payloads
    assert any(e.slot == p2.slot and e.reason == 'payload_count_exceeded' for e in prepared.unavailable)


def test_prepare_export_run_budget_exceeded(terminal_event, policy):
    limited = policy.model_copy(update={'run_bytes': 30, 'payload_bytes': 25, 'payload_slots': 10})
    p1 = RedactedPayload(slot=PayloadSlot(kind='retrieval_chunk', ordinal=1), data=b'a' * 20, policy_version=limited.redaction_policy_version)
    p2 = RedactedPayload(slot=PayloadSlot(kind='retrieval_chunk', ordinal=2), data=b'b' * 20, policy_version=limited.redaction_policy_version)
    prepared = prepare_export(terminal_event, limited, (p1, p2), ())
    assert p1 in prepared.payloads
    assert any(e.slot == p2.slot and e.reason == 'run_budget_exceeded' for e in prepared.unavailable)


def test_prepare_export_payload_too_large(terminal_event, policy):
    limited = policy.model_copy(update={'payload_bytes': 5})
    p1 = RedactedPayload(slot=PayloadSlot(kind='retrieval_chunk', ordinal=1), data=b'x' * 20, policy_version=limited.redaction_policy_version)
    prepared = prepare_export(terminal_event, limited, (p1,), ())
    assert any(e.slot == p1.slot and e.reason == 'payload_too_large' for e in prepared.unavailable)


def test_envelope_hash_tamper_rejected(envelope):
    tampered = envelope.model_dump(mode='json')
    tampered['redaction_policy_version'] = 'other-version'
    with pytest.raises(ValidationError):
        ExportEnvelope.model_validate({**tampered, 'export_hash': envelope.export_hash})


def test_registered_review_event_is_safe_versioned_and_reconstructable(terminal_event, policy):
    from services.api.app.evaluation.models import OperationEvidence, RunEvidence
    from libs.observability.operation_contracts import (
        FieldRule, OperationSchema, PhaseRule, register_operation_schema,
    )

    register_operation_schema(OperationSchema('test.evidence.review', 1, {
        'result': PhaseRule({
            'verdict': FieldRule(str, frozenset({'otel_safe', 'evidence_only'}), max_length=16),
            'rubric_id': FieldRule(str, frozenset({'evidence_only'}), max_length=32),
        }, frozenset({'rationale'})),
    }))
    operation = OperationEvidence(
        operation_id='a' * 32, parent_operation_id=None, name='test.evidence.review',
        schema_version=1, phase='result', sequence=1, timestamp=terminal_event.completed_at,
        facts={'verdict': 'pass', 'rubric_id': 'private-rubric'},
        content_slots={'rationale': {'ordinal': 1, 'item_ordinal': 1}},
    )
    run = RunEvidence.model_validate(terminal_event.model_dump(mode='json') | {
        'schema_version': '1.1', 'operation_events': [operation.model_dump(mode='json')],
    })
    safe = project_metadata(run, policy, ())
    assert safe.operation_events[0].facts['verdict'] == 'pass'
    assert safe.operation_events[0].facts['rubric_id'].startswith('hmac-sha256:')
    assert safe.operation_events[0].content_slots['rationale'] == PayloadSlot(
        kind='operation_content', ordinal=1, item_ordinal=1)
    sealed = seal_export(prepare_export(run, policy, (), ()), ())
    assert (sealed.export_version, sealed.run_schema_version) == ('1.1', '1.1')
    assert b'private-rubric' not in sealed.canonical_bytes()
    assert ExportEnvelope.model_validate_json(sealed.canonical_bytes()).canonical_bytes() == sealed.canonical_bytes()
