"""Contracts for rich operation records, independent of OTel sampling."""

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone

import pytest


def test_registered_schema_accepts_declared_facts_and_content_only():
    from libs.observability.operation_contracts import (
        FieldRule, OperationSchema, PhaseRule, get_operation_schema,
        register_operation_schema, validate_record,
    )

    schema = OperationSchema("test.agent.review", 1, {
        "result": PhaseRule({
            "verdict": FieldRule(str, frozenset({"otel_safe", "evidence_only"}), max_length=32),
            "rubric_id": FieldRule(str, frozenset({"evidence_only"}), max_length=64),
        }, frozenset({"rationale"})),
    })
    register_operation_schema(schema)
    assert get_operation_schema("test.agent.review", 1) == schema
    assert validate_record(schema, "result", {"verdict": "pass"}, ("rationale",)) == {"verdict": "pass"}
    with pytest.raises(ValueError, match="unknown fact"):
        validate_record(schema, "result", {"unreviewed_text": "secret"}, ())
    with pytest.raises(ValueError, match="unknown phase"):
        validate_record(schema, "request", {}, ())
    with pytest.raises(ValueError, match="unknown content"):
        validate_record(schema, "result", {}, ("private_notes",))


def test_schema_registration_rejects_conflicting_definition():
    from libs.observability.operation_contracts import (
        FieldRule, OperationSchema, PhaseRule, register_operation_schema,
    )

    first = OperationSchema("test.conflict", 1, {"result": PhaseRule({}, frozenset())})
    register_operation_schema(first)
    register_operation_schema(first)
    other = OperationSchema("test.conflict", 1, {
        "result": PhaseRule({"status": FieldRule(str, frozenset({"otel_safe"}))}, frozenset()),
    })
    with pytest.raises(ValueError, match="conflicting schema"):
        register_operation_schema(other)


@pytest.mark.parametrize("value", [True, 1.5, float("nan"), float("inf"), {"x": 1}])
def test_int_field_rejects_wrong_or_nonfinite_values(value):
    from libs.observability.operation_contracts import FieldRule, OperationSchema, PhaseRule, validate_record

    schema = OperationSchema("test.int", 1, {
        "result": PhaseRule({"count": FieldRule(int, frozenset({"otel_safe"}))}, frozenset()),
    })
    with pytest.raises(ValueError, match="invalid fact"):
        validate_record(schema, "result", {"count": value}, ())


def test_string_and_fact_count_bounds_are_enforced_before_copy():
    from libs.observability.operation_contracts import FieldRule, OperationSchema, PhaseRule, validate_record

    schema = OperationSchema("test.bounds", 1, {
        "result": PhaseRule({f"f{i}": FieldRule(str, frozenset({"evidence_only"}), max_length=4)
                             for i in range(16)}, frozenset()),
    })
    with pytest.raises(ValueError, match="invalid fact"):
        validate_record(schema, "result", {"f0": "12345"}, ())
    with pytest.raises(ValueError, match="too many facts"):
        validate_record(schema, "result", {f"f{i}": "x" for i in range(17)}, ())
    with pytest.raises(ValueError, match="too many fields"):
        PhaseRule({f"f{i}": FieldRule(str, frozenset({"evidence_only"})) for i in range(17)}, frozenset())


def test_schema_cannot_allow_strings_larger_than_record_contract():
    from libs.observability.operation_contracts import FieldRule, OperationSchema, PhaseRule, validate_record

    with pytest.raises(ValueError, match="invalid fact length bound"):
        FieldRule(str, frozenset({"evidence_only"}), max_length=1025)
    with pytest.raises(ValueError, match="invalid fact length bound"):
        FieldRule(str, frozenset({"otel_safe"}), max_length=129)
    schema = OperationSchema('test.long.identifier', 1, {
        'hit': PhaseRule({'document_id': FieldRule(str, frozenset({'evidence_only'}), max_length=512)}, frozenset()),
    })
    value = 'd' * 300
    assert validate_record(schema, 'hit', {'document_id': value}, ()) == {'document_id': value}


def test_record_is_immutable_metadata_and_candidate_keeps_content_separate():
    from libs.observability.operation_contracts import ContentCandidate, OperationRecord

    record = OperationRecord(
        operation_id="op-1", parent_operation_id=None, name="test.agent.review",
        schema_version=1, phase="result", sequence=1,
        timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc), facts={"verdict": "pass"},
    )
    candidate = ContentCandidate(value="private rationale", provenance=None)
    assert record.facts["verdict"] == "pass"
    assert "private rationale" not in repr(record)
    assert candidate.value == "private rationale"
    with pytest.raises((FrozenInstanceError, AttributeError)):
        record.name = "modified"
    with pytest.raises(TypeError):
        record.facts["verdict"] = "fail"


@pytest.mark.parametrize("unsafe", [["private"], {"text": "private"}, float("nan")])
def test_record_rejects_values_that_cannot_be_safe_metadata(unsafe):
    from libs.observability.operation_contracts import OperationRecord

    with pytest.raises(ValueError, match="invalid operation fact"):
        OperationRecord(
            operation_id="op-2", parent_operation_id=None, name="agent.review",
            schema_version=1, phase="result", sequence=1,
            timestamp=datetime(2026, 9, 28, tzinfo=timezone.utc), facts={"verdict": unsafe},
        )
