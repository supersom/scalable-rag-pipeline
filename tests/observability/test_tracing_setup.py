import asyncio
import io
import json
import subprocess
import sys

import pytest
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from libs.observability import tracing


def test_configure_is_idempotent_and_sets_global_provider_once():
    script = """
from opentelemetry import trace
from libs.observability.tracing import configure_tracing
p1 = configure_tracing('test-service')
p2 = configure_tracing('ignored-second-name')
assert p1 is p2 is trace.get_tracer_provider()
assert p1.resource.attributes['service.name'] == 'test-service'
p1.shutdown()
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "Overriding" not in result.stderr


def test_resource_identity_and_console_export_are_explicit():
    stream = io.StringIO()
    provider = tracing.create_tracer_provider(
        "test-api", environment="test", service_version="abc123",
        console_enabled=True, console_exporter=ConsoleSpanExporter(out=stream),
    )
    with provider.get_tracer("test").start_as_current_span("test.run"):
        pass
    assert provider.force_flush(timeout_millis=2000)
    provider.shutdown()
    exported = json.loads(stream.getvalue())
    assert exported["name"] == "test.run"
    assert exported["resource"]["attributes"]["service.name"] == "test-api"
    assert exported["resource"]["attributes"]["deployment.environment"] == "test"
    assert exported["resource"]["attributes"]["service.version"] == "abc123"


@pytest.mark.parametrize("sampler,ratio,expected", [
    ("always_on", 1.0, 1), ("always_off", 1.0, 0),
    ("traceidratio", 0.0, 0), ("traceidratio", 1.0, 1),
    ("parentbased_always_on", 1.0, 1),
    ("parentbased_traceidratio", 0.0, 0),
])
def test_sampling_controls_exported_spans(sampler, ratio, expected):
    provider = tracing.create_tracer_provider("test", sampler_name=sampler, sample_ratio=ratio)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    with provider.get_tracer("test").start_as_current_span("sampled"):
        pass
    assert len(exporter.get_finished_spans()) == expected
    provider.shutdown()


@pytest.mark.parametrize("kwargs", [
    {"sampler_name": "typo"}, {"sample_ratio": -0.1}, {"sample_ratio": 1.1},
    {"endpoint": "ftp://collector:4317"},
])
def test_invalid_tracing_configuration_fails_early(kwargs):
    with pytest.raises(ValueError):
        tracing.create_tracer_provider("test", **kwargs)


def test_shutdown_flushes_completed_spans_and_can_be_repeated():
    stream = io.StringIO()
    provider = tracing.create_tracer_provider(
        "test", console_enabled=True, console_exporter=ConsoleSpanExporter(out=stream),
    )
    with provider.get_tracer("test").start_as_current_span("last-run"):
        pass
    asyncio.run(tracing.shutdown_tracing(provider))
    asyncio.run(tracing.shutdown_tracing(provider))
    assert json.loads(stream.getvalue())["name"] == "last-run"


def test_no_console_payload_is_exported_by_default(capsys):
    provider = tracing.create_tracer_provider("test")
    with provider.get_tracer("test").start_as_current_span("silent"):
        pass
    provider.force_flush()
    provider.shutdown()
    assert capsys.readouterr().out == ""


def test_otlp_batch_export_delivers_resource_and_span_to_grpc_receiver():
    from concurrent.futures import ThreadPoolExecutor
    from queue import Queue
    import grpc
    from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

    received = Queue()
    class Receiver(trace_service_pb2_grpc.TraceServiceServicer):
        def Export(self, request, context):
            received.put(request)
            return trace_service_pb2.ExportTraceServiceResponse()
    server = grpc.server(ThreadPoolExecutor(max_workers=1))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(Receiver(), server)
    port = server.add_insecure_port("127.0.0.1:0")
    server.start()
    provider = tracing.create_tracer_provider("grpc-test", endpoint=f"http://127.0.0.1:{port}")
    try:
        with provider.get_tracer("test").start_as_current_span("agent.run") as span:
            span.set_attribute("run_id", "export-test")
        assert provider.force_flush(timeout_millis=2000)
        request = received.get(timeout=2)
        [resource_spans] = request.resource_spans
        attributes = {a.key: a.value.string_value for a in resource_spans.resource.attributes}
        assert attributes["service.name"] == "grpc-test"
        [exported] = resource_spans.scope_spans[0].spans
        assert exported.name == "agent.run"
        assert exported.trace_id == span.get_span_context().trace_id.to_bytes(16, "big")
    finally:
        provider.shutdown()
        server.stop(grace=0).wait()
