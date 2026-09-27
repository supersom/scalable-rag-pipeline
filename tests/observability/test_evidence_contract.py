"""Versioning, privacy and reproducible collector snapshots are API contracts."""
import importlib.util
import json
from concurrent.futures import ThreadPoolExecutor
from pydantic import ValidationError

import pytest


def evidence_modules():
    assert importlib.util.find_spec("services.api.app.evaluation") is not None, "Missing evidence package"
    from services.api.app.evaluation import context, models
    return context, models


def collector():
    context, models = evidence_modules()
    return context.RunEvidenceCollector(
        run_id="run-1", session_id="session-1", trace_id="1" * 32,
        input_text="private question", user_id="private-user", tenant_id="tenant-a",
        identity_hash_key="local-test-key",
        configuration=models.ConfigurationFingerprint(generation_model="model-v1", embedding_model="embed-v2"),
    )


def test_terminal_record_roundtrips_and_does_not_embed_sensitive_content():
    context, models = evidence_modules()
    c = collector()
    c.record_output("private answer")
    c.mark_first_output()
    event = c.finish("success")
    wire = event.model_dump_json()
    restored = models.RunEvidence.model_validate_json(wire)
    assert restored == event
    assert event.schema_version == "1.0"
    assert event.trace_id == "1" * 32
    assert event.operations.latency_ms >= 0
    assert event.operations.time_to_first_output_ms is not None
    assert event.input_ref is event.output_ref is None
    assert event.configuration.fingerprint.startswith("sha256:")
    assert event.user_id_hash.startswith("hmac-sha256:")
    assert "private" not in wire
    assert "payloads_not_stored" in event.unavailable
    with pytest.raises(RuntimeError, match="finalized"):
        c.finish("error")
    with pytest.raises(RuntimeError, match="finalized"):
        c.record_output("late answer")
    with pytest.raises(ValidationError):
        models.RunEvidence.model_validate_json(wire.replace('"1.0"', '"2.0"', 1))


def test_identity_hashes_are_keyed_tenant_scoped_and_optional():
    context, models = evidence_modules()
    def one(tenant, key):
        return context.RunEvidenceCollector(run_id="r", session_id="s", input_text="q", user_id="user",
            tenant_id=tenant, identity_hash_key=key, configuration=models.ConfigurationFingerprint()).finish("success")
    a, b, again, missing = one("a", "key"), one("b", "key"), one("a", "key"), one("a", None)
    assert a.user_id_hash == again.user_id_hash != b.user_id_hash
    assert missing.user_id_hash is None
    assert "identity_hash_key_not_configured" in missing.unavailable


def test_concurrent_observations_are_append_only_and_snapshots_are_detached():
    _, models = evidence_modules()
    c = collector()
    def record(index):
        c.record_operation("llm.chat", {"operation.status": "success", "operation.latency_ms": 1.,
            "gen_ai.usage.input_tokens": 2, "gen_ai.usage.output_tokens": 1, "retry.count": 0})
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(record, range(32)))
    event = c.finish("success")
    assert len(event.dependencies) == 32
    assert [o.sequence for o in event.dependencies] == list(range(1, 33))
    event.dependencies.clear()
    assert len(c.event.dependencies) == 32


def test_unknown_usage_is_not_reported_as_zero_and_fingerprint_tracks_configuration():
    _, models = evidence_modules()
    c = collector()
    c.record_model_call(models.ModelCall(purpose="planner", request_hash="sha256:" + "a" * 64))
    event = c.finish("success")
    assert event.operations.input_tokens is event.operations.output_tokens is None
    a = models.ConfigurationFingerprint(generation_model="v1")
    same = models.ConfigurationFingerprint(generation_model="v1")
    changed = models.ConfigurationFingerprint(generation_model="v2")
    assert a.fingerprint == same.fingerprint != changed.fingerprint


def test_fixture_schema_1_remains_readable_and_rejects_raw_extra_fields():
    _, models = evidence_modules()
    from pathlib import Path
    fixture = Path(__file__).parent / "fixtures" / "run-evidence-v1.json"
    event = models.RunEvidence.model_validate_json(fixture.read_text())
    assert event.run_id == "fixture-run"
    assert event.model_calls[0].input_tokens == 12
    assert event.retrieval[0].items[0].chunk_id == "chunk-7"
    assert event.trajectory[0].tool_name == "calculator"
    wire = json.loads(event.model_dump_json())
    wire["prompt"] = "private prompt"
    with pytest.raises(ValidationError): models.RunEvidence.model_validate(wire)
