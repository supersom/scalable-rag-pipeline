"""One span interface produces safe trace events and unsampled evidence records."""

from libs.observability.operation_contracts import (
    FieldRule, OperationSchema, PhaseRule, register_operation_schema,
)


def _review_schema():
    schema = OperationSchema("test.rich.review", 1, {
        "request": PhaseRule({
            "rubric_id": FieldRule(str, frozenset({"evidence_only"}), max_length=32),
        }, frozenset({"rationale"})),
        "result": PhaseRule({
            "verdict": FieldRule(str, frozenset({"otel_safe", "evidence_only"}), max_length=16),
        }, frozenset()),
    })
    register_operation_schema(schema)
    return schema


def test_record_keeps_content_out_of_otel_and_preserves_parent_and_sequence(route, monkeypatch):
    from libs.observability import tracing

    _review_schema()
    _, provider, exporter = route
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("test.rich"))
    seen = []
    summaries = []
    record_token = tracing.record_observer.set(lambda record, content: seen.append((record, content)))
    summary_token = tracing.operation_observer.set(lambda name, attrs: summaries.append((name, attrs)))
    try:
        with tracing.domain_span("parent") as parent:
            with tracing.domain_span("test.rich.review", schema_version=1) as op:
                op.record("request", facts={"rubric_id": "rubric-1"},
                          content={"rationale": op.content("private rationale", provenance=None)})
                op.record("result", facts={"verdict": "pass"})
    finally:
        tracing.record_observer.reset(record_token)
        tracing.operation_observer.reset(summary_token)

    assert [record.phase for record, _ in seen] == ["request", "result"]
    assert [record.sequence for record, _ in seen] == [1, 2]
    assert seen[0][0].operation_id == seen[1][0].operation_id == op.operation_id
    assert seen[0][0].parent_operation_id == parent.operation_id
    assert seen[0][1]["rationale"].value == "private rationale"
    assert [name for name, _ in summaries] == ["test.rich.review", "parent"]
    child = next(span for span in exporter.get_finished_spans() if span.name == "test.rich.review")
    assert [event.name for event in child.events] == ["operation.result"]
    assert child.events[0].attributes["verdict"] == "pass"
    assert "private rationale" not in str(child.attributes) + str(child.events)
    assert "rubric-1" not in str(child.attributes) + str(child.events)


def test_unsampled_span_still_dispatches_record(monkeypatch):
    from libs.observability import tracing

    _review_schema()
    provider = tracing.create_tracer_provider("test", sampler_name="always_off")
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("test.unsampled"))
    seen = []
    token = tracing.record_observer.set(lambda record, content: seen.append(record))
    try:
        with tracing.domain_span("test.rich.review", schema_version=1) as op:
            op.record("result", facts={"verdict": "pass"})
    finally:
        tracing.record_observer.reset(token)
        provider.shutdown()
    assert len(seen) == 1
    assert seen[0].facts["verdict"] == "pass"


def test_invalid_record_and_observer_failure_are_fail_open(route, monkeypatch):
    from libs.observability import tracing

    _review_schema()
    _, provider, exporter = route
    monkeypatch.setattr(tracing, "domain_tracer", provider.get_tracer("test.invalid"))
    seen = []
    token = tracing.record_observer.set(lambda record, content: seen.append(record))
    try:
        with tracing.domain_span("test.rich.review", schema_version=1) as op:
            op.record("result", facts={"verdict": "pass", "secret": "private"})
    finally:
        tracing.record_observer.reset(token)
    assert seen == []
    [span] = exporter.get_finished_spans()
    assert span.attributes["evidence.capture_failed"] is True
    assert "private" not in str(span.attributes) + str(span.events)

    exporter.clear()
    token = tracing.record_observer.set(lambda record, content: (_ for _ in ()).throw(RuntimeError("private")))
    try:
        with tracing.domain_span("test.rich.review", schema_version=1) as op:
            op.record("result", facts={"verdict": "pass"})
    finally:
        tracing.record_observer.reset(token)
    [span] = exporter.get_finished_spans()
    assert span.attributes["evidence.capture_failed"] is True
    assert "private" not in str(span.attributes) + str(span.events)


def test_unbound_provenance_is_inert():
    from libs.observability import tracing

    with tracing.domain_span("test.provenance") as op:
        assert op.provenance.source("private") is None
        assert op.provenance.template("template", template_id="v1") is None
        assert op.provenance.combine(None) is None
        assert op.provenance.remember("derived", None) is None
