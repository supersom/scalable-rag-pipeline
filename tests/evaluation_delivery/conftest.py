"""Offline fixtures for delivery contract tests."""
from uuid import uuid4

import pytest


@pytest.fixture
def terminal_event(monkeypatch):
    for name, value in {
        "DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
        "REDIS_URL": "redis://localhost:6379/0",
        "NEO4J_PASSWORD": "synthetic-password",
        "S3_BUCKET_NAME": "synthetic-documents",
        "JWT_SECRET_KEY": "synthetic-jwt-secret",
    }.items():
        monkeypatch.setenv(name, value)
    import qdrant_client
    client_type = qdrant_client.AsyncQdrantClient
    monkeypatch.setattr(qdrant_client, "AsyncQdrantClient", lambda **kwargs: client_type(location=":memory:"))
    from services.api.app.evaluation.context import RunEvidenceCollector
    from services.api.app.evaluation.models import ConfigurationFingerprint

    return RunEvidenceCollector(
        run_id=str(uuid4()), session_id="synthetic-session", user_id="synthetic-user",
        tenant_id="synthetic-tenant", identity_hash_key="dedicated-synthetic-evidence-key",
        input_text="synthetic question", trace_id="a" * 32,
        configuration=ConfigurationFingerprint(generation_model="fake-chat", embedding_model="fake-embed"),
    ).finish("success")
