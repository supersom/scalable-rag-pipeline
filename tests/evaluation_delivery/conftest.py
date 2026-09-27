"""Offline fixtures for delivery contract tests."""
from uuid import uuid4

import pytest


@pytest.fixture
def policy():
    from pathlib import Path
    from services.api.app.evaluation.provenance import SyntheticManifest
    from services.api.app.evaluation.delivery_models import ExportPolicy
    manifest = SyntheticManifest.load(Path(__file__).parents[2] / 'deploy/evaluation/restricted-synthetic-v1.json')
    return ExportPolicy(identity_key='dedicated-synthetic-evidence-key',
                        allowed_models=('fake-chat', 'fake-embed'), allowed_tools=('calculator',),
                        allowed_collections=('synthetic-documents',), allowed_dependencies=('cache',),
                        synthetic_sources=manifest.sources)


@pytest.fixture
def provenance_for(policy):
    from services.api.app.evaluation.provenance import Provenance, SourceProof
    def make(case_id='literal', tenant='synthetic-tenant', tool_name=None):
        record = next(r for r in policy.synthetic_sources if r.case_id == case_id and r.tenant_id == tenant)
        return Provenance(tenant_id=tenant, case_id=case_id, tool_name=tool_name,
                          sources=(SourceProof(**record.model_dump(exclude={'tenant_id', 'case_id'})),))
    return make


@pytest.fixture
def approved_input(provenance_for):
    return provenance_for()


@pytest.fixture
def envelope(terminal_event, policy):
    from services.api.app.evaluation.metadata_export import prepare_export, seal_export
    return seal_export(prepare_export(terminal_event, policy, (), ()), ())


@pytest.fixture
def rich_event():
    from datetime import datetime, timezone
    from services.api.app.evaluation.models import (
        ConfigurationFingerprint, DependencyObservation, ModelCall, OperationalEvidence,
        RetrievalEvidence, RetrievedEvidence, RunEvidence, ToolStep,
    )
    started = datetime(2026, 1, 1, tzinfo=timezone.utc)
    completed = datetime(2026, 1, 1, 0, 0, 1, tzinfo=timezone.utc)
    content_hash = "sha256:" + "ab" * 32
    return RunEvidence(
        run_id="11111111-1111-1111-1111-111111111111", session_id="rich-session",
        trace_id="a" * 32, started_at=started, completed_at=completed, status="success",
        tenant_id="synthetic-tenant", input_hash=content_hash, output_hash=content_hash,
        configuration=ConfigurationFingerprint(generation_model="fake-chat", embedding_model="fake-embed",
                                               corpus_version="/etc/passwd"),
        retrieval=[RetrievalEvidence(query_hash=content_hash, collection="unknown-collection",
            items=[RetrievedEvidence(retrieval_source="qdrant", rank=1, content_hash=content_hash,
                document_id="doc-1", chunk_id="chunk-1")])],
        model_calls=[ModelCall(request_hash=content_hash, model="unknown-model",
            provider_request_id="raw-provider-id-12345", completion_id="raw-completion-id-999",
            status="success", error_type="BoomError")],
        trajectory=[ToolStep(step=1, tool_name="unapproved-tool", started_at=started, latency_ms=1.0,
            status="success", arguments_hash=content_hash, result_hash=content_hash)],
        dependencies=[DependencyObservation(sequence=1, name="cache", status="success", latency_ms=1.0,
            attributes={"tool.name": "/etc/passwd", "cache.hit": True})],
        operations=OperationalEvidence(latency_ms=5.0),
    )


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
