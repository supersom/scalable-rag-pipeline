import os
from pathlib import Path

import pytest
import yaml
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTOR_CONFIG = REPO_ROOT / "deploy/otel/collector.local.yaml"


def _load_config():
    return yaml.safe_load(COLLECTOR_CONFIG.read_text())


def test_collector_exposes_otlp_receivers_and_health_endpoint():
    config = _load_config()

    assert config["extensions"]["health_check"]["endpoint"] == "0.0.0.0:13133"
    assert config["receivers"]["otlp"]["protocols"] == {
        "grpc": {"endpoint": "0.0.0.0:4317"},
        "http": {"endpoint": "0.0.0.0:4318"},
    }
    assert "health_check" in config["service"]["extensions"]


def test_collector_trace_pipeline_limits_memory_batches_and_exports():
    config = _load_config()

    assert config["processors"]["memory_limiter"] == {
        "check_interval": "1s",
        "limit_mib": 128,
    }
    assert config["processors"]["batch"] == {}
    assert config["exporters"]["debug"] == {"verbosity": "detailed"}
    assert config["service"]["pipelines"]["traces"] == {
        "receivers": ["otlp"],
        "processors": ["memory_limiter", "batch"],
        "exporters": ["debug"],
    }


@pytest.mark.skipif(
    os.getenv("RUN_LOCAL_STACK_TESTS") != "1",
    reason="set RUN_LOCAL_STACK_TESTS=1 after make local-up",
)
def test_running_collector_accepts_otlp_grpc_span():
    memory_exporter = InMemorySpanExporter()
    provider = TracerProvider(
        resource=Resource.create({"service.name": "local-eval-pytest"})
    )
    provider.add_span_processor(SimpleSpanProcessor(memory_exporter))

    with provider.get_tracer(__name__).start_as_current_span(
        "local-eval.collector-smoke"
    ) as span:
        span.set_attribute("test.kind", "otlp-ingestion")

    spans = memory_exporter.get_finished_spans()
    provider.shutdown()
    assert len(spans) == 1

    exporter = OTLPSpanExporter(
        endpoint=os.getenv("LOCAL_OTEL_GRPC_ENDPOINT", "127.0.0.1:14317"),
        insecure=True,
        timeout=5,
    )
    try:
        assert exporter.export(spans) is SpanExportResult.SUCCESS
    finally:
        exporter.shutdown()
