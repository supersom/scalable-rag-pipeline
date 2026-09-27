import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


@pytest.fixture
def route(monkeypatch):
    for key, value in {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "NEO4J_PASSWORD": "test",
        "S3_BUCKET_NAME": "test",
        "JWT_SECRET_KEY": "test-secret",
    }.items():
        monkeypatch.setenv(key, value)
    # Injected route dependencies do not need a remote Qdrant connection.
    # Use its real embedded backend to avoid import-time network probes.
    import qdrant_client
    client_type = qdrant_client.AsyncQdrantClient
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", lambda **kwargs: client_type(location=":memory:"))
    from services.api.app.routes import chat

    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(chat, "tracer", provider.get_tracer("test.chat"))
    yield chat, provider, exporter
    provider.shutdown()
