from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError


def test_canonical_encoding_is_order_independent_and_finite():
    from services.api.app.evaluation.delivery_models import canonical_bytes
    assert canonical_bytes({"b": 2, "a": 1}) == b'{"a":1,"b":2}'
    assert canonical_bytes({"text": "é"}) == '{"text":"é"}'.encode()
    with pytest.raises(ValueError):
        canonical_bytes({"bad": float("nan")})


def test_slot_identity_and_model_are_strict():
    from services.api.app.evaluation.delivery_models import EventIdentity, PayloadSlot
    with pytest.raises(ValidationError):
        EventIdentity(run_id="not-a-uuid")
    with pytest.raises(ValidationError):
        EventIdentity(run_id=uuid4(), export_version="2.0")
    with pytest.raises(ValidationError):
        PayloadSlot(kind="input", ordinal=0)
    with pytest.raises(ValidationError):
        PayloadSlot(kind="input", ordinal=1, item_ordinal=0)
    with pytest.raises(ValidationError):
        PayloadSlot(kind="input", ordinal=1, surprise="raw")


def test_generic_operation_content_slot_is_bounded_and_distinct():
    from services.api.app.evaluation.delivery_models import PayloadSlot

    slot = PayloadSlot(kind="operation_content", ordinal=1, item_ordinal=1)
    assert slot.model_dump(mode="json") == {"kind": "operation_content", "ordinal": 1, "item_ordinal": 1}
    assert PayloadSlot(kind="model_request", ordinal=1).kind.value == "model_request"
    with pytest.raises(ValidationError):
        PayloadSlot(kind="operation_content", ordinal=1)
    with pytest.raises(ValidationError):
        PayloadSlot(kind="operation_content", ordinal=1, item_ordinal=0)


def test_operation_event_rejects_duplicate_content_slot_mapping():
    from services.api.app.evaluation.models import OperationEvidence

    with pytest.raises(ValidationError, match="duplicate operation content slot"):
        OperationEvidence(
            operation_id="a" * 32, name="agent.review", schema_version=1,
            phase="result", sequence=1, timestamp=datetime.now(timezone.utc),
            content_slots={
                "rationale": {"ordinal": 1, "item_ordinal": 1},
                "candidate": {"ordinal": 1, "item_ordinal": 1},
            },
        )


def test_manifest_requires_exactly_ref_or_reason():
    from services.api.app.evaluation.delivery_models import ManifestEntry, PayloadSlot, PayloadRef
    slot = PayloadSlot(kind="input", ordinal=1)
    now = datetime.now(timezone.utc)
    ref = PayloadRef(bucket="evidence", key="env/tenant/key", version_id="version-1",
                     tenant_partition="tenant", slot=slot, byte_size=5,
                     sha256="a" * 64, policy_version="restricted-synthetic-v1",
                     created_at=now, expires_at=now + timedelta(days=30))
    assert ManifestEntry(slot=slot, state="stored", reference=ref).reference == ref
    for fields in ({"state": "stored"}, {"state": "unavailable", "reference": ref},
                   {"state": "stored", "reference": ref, "reason": "upload_failed"}):
        with pytest.raises(ValidationError):
            ManifestEntry(slot=slot, **fields)
    with pytest.raises(ValidationError):
        PayloadRef(bucket="evidence", key="env/tenant/key", version_id="",
                   tenant_partition="tenant", slot=slot, byte_size=5,
                   sha256="a" * 64, policy_version="restricted-synthetic-v1",
                   created_at=now, expires_at=now + timedelta(days=30))
    with pytest.raises(ValidationError):
        PayloadRef.model_validate(ref.model_dump() | {"created_at": now.replace(tzinfo=None)})


def test_export_hash_is_canonical_and_detects_tampering(terminal_event):
    from services.api.app.evaluation.delivery_models import ExportEnvelope, SafeRun
    from hashlib import sha256
    safe = SafeRun(run_id=terminal_event.run_id, started_at=terminal_event.started_at,
                   completed_at=terminal_event.completed_at, status="success")
    envelope = ExportEnvelope(run_id=terminal_event.run_id, safe_run=safe,
                              redaction_policy_version="restricted-synthetic-v1",
                              completeness="metadata_only")
    body = envelope.canonical_bytes()
    assert envelope.export_hash == sha256(envelope.canonical_bytes(exclude_hash=True)).hexdigest()
    assert b'"export_hash"' in body and b'"completeness":"metadata_only"' in body
    with pytest.raises(ValidationError):
        ExportEnvelope.model_validate(envelope.model_dump() | {"completeness": "complete"})
    with pytest.raises(ValidationError):
        SafeRun(run_id=terminal_event.run_id, started_at=terminal_event.started_at.replace(tzinfo=None),
                completed_at=terminal_event.completed_at, status="success")


def test_policy_key_is_private_in_representations():
    from services.api.app.evaluation.delivery_models import ExportPolicy
    policy = ExportPolicy(identity_key="dedicated-private-key")
    assert "dedicated-private-key" not in repr(policy)
    assert "dedicated-private-key" not in policy.model_dump_json()


def test_object_version_retains_delete_marker_identity():
    from services.api.app.evaluation.delivery_models import ObjectVersion
    version = ObjectVersion(key="env/tenant/key", version_id="v1", is_delete_marker=True)
    assert version.version_id == "v1" and version.is_delete_marker


def test_safe_nested_metrics_reject_nonfinite_values(terminal_event):
    from services.api.app.evaluation.delivery_models import SafeRun, SafeOperations
    with pytest.raises(ValidationError):
        SafeOperations(latency_ms=float("nan"))
    with pytest.raises(ValidationError):
        SafeRun(run_id=terminal_event.run_id, started_at=terminal_event.started_at,
                completed_at=terminal_event.completed_at, status="success",
                model_calls=({"ordinal": 1, "status": "made-up-status"},))


def test_dependency_attributes_cannot_change_sealed_envelope(terminal_event):
    from services.api.app.evaluation.delivery_models import ExportEnvelope, SafeDependency, SafeRun
    attributes = {"attempts": 1}
    dependency = SafeDependency(sequence=1, status="success", latency_ms=1,
                                attributes=attributes)
    safe = SafeRun(run_id=terminal_event.run_id, started_at=terminal_event.started_at,
                   completed_at=terminal_event.completed_at, status="success",
                   dependencies=(dependency,))
    envelope = ExportEnvelope(run_id=terminal_event.run_id, safe_run=safe,
                              redaction_policy_version="restricted-synthetic-v1",
                              completeness="metadata_only")
    original = envelope.canonical_bytes()
    attributes["attempts"] = 2
    with pytest.raises(TypeError):
        dependency.attributes["attempts"] = 3
    assert envelope.canonical_bytes() == original
    assert ExportEnvelope.model_validate_json(original).canonical_bytes() == original
    assert envelope.model_copy(deep=True).canonical_bytes() == original


def test_legacy_envelope_without_operation_events_keeps_original_hash(envelope):
    from hashlib import sha256
    from services.api.app.evaluation.delivery_models import ExportEnvelope, canonical_bytes

    old = envelope.model_dump(mode="json")
    old["safe_run"].pop("operation_events", None)
    old["export_version"] = old["run_schema_version"] = "1.0"
    old["export_hash"] = sha256(canonical_bytes({k: v for k, v in old.items() if k != "export_hash"})).hexdigest()
    original = canonical_bytes(old)
    restored = ExportEnvelope.model_validate_json(original)
    assert restored.canonical_bytes() == original
    assert restored.export_hash == old["export_hash"]
