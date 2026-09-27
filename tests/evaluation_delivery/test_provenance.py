from pathlib import Path

from services.api.app.evaluation.provenance import Provenance, SourceProof, SyntheticManifest


def test_manifest_approval(policy, approved_input):
    assert approved_input.approved(policy)
    assert not approved_input.model_copy(update={'tenant_id': 'other'}).approved(policy)
    assert not Provenance(tenant_id='synthetic-tenant').approved(policy)


def test_mixed_sources_denied(policy, approved_input):
    bad = SourceProof(source_kind='input', source_id='invented', source_hash='0' * 64)
    assert not approved_input.model_copy(update={'sources': (*approved_input.sources, bad)}).approved(policy)


def test_environment_and_policy_version_gate_approval(policy, approved_input):
    assert not approved_input.approved(policy.model_copy(update={'environment': 'prod'}))
    assert not approved_input.approved(policy.model_copy(update={'redaction_policy_version': 'other-v1'}))


def test_case_id_mismatch_denied(policy, approved_input):
    assert not approved_input.model_copy(update={'case_id': 'other-case'}).approved(policy)


def test_manifest_load_roundtrip():
    manifest = SyntheticManifest.load(Path(__file__).parents[2] / 'deploy/evaluation/restricted-synthetic-v1.json')
    assert manifest.policy_version == 'restricted-synthetic-v1'
    assert manifest.calculator_grammar == 'numeric-sqrt-v1'
    assert len(manifest.sources) > 0
