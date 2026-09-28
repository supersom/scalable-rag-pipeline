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
def aws_env(monkeypatch):
    for name, value in {
        'AWS_ACCESS_KEY_ID': 'synthetic-access-key', 'AWS_SECRET_ACCESS_KEY': 'synthetic-secret-key',
        'AWS_SESSION_TOKEN': 'synthetic-session-token', 'AWS_DEFAULT_REGION': 'us-east-1',
        'AWS_EC2_METADATA_DISABLED': 'true',
    }.items():
        monkeypatch.setenv(name, value)
    for name in ('AWS_PROFILE', 'AWS_ENDPOINT_URL', 'AWS_ENDPOINT_URL_S3'):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def evidence_bucket():
    return 'synthetic-evidence-bucket'


@pytest.fixture
def s3_client(aws_env, evidence_bucket):
    import boto3
    from moto import mock_aws
    with mock_aws():
        client = boto3.client('s3', region_name='us-east-1')
        client.create_bucket(Bucket=evidence_bucket)
        client.put_bucket_versioning(Bucket=evidence_bucket, VersioningConfiguration={'Status': 'Enabled'})
        try:
            yield client
        finally:
            client.close()


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


# --- Opt-in live PostgreSQL fixtures -------------------------------------------------
# Each test gets a freshly generated schema; teardown drops only that exact schema.
# Shared tables (chat history, any existing evidence tables) are never touched.
import asyncio
import os
import re
from datetime import datetime, timedelta, timezone

LIVE_SCHEMA_PATTERN = re.compile(r"^eval_test_[0-9a-f]{16}$")


class FakeClock:
    def __init__(self, now=None):
        self.now = now or datetime.now(timezone.utc).replace(microsecond=0)

    def __call__(self):
        return self.now

    def advance(self, **delta):
        self.now = self.now + timedelta(**delta)
        return self.now


def live_postgres_url():
    if os.environ.get("RUN_EVAL_POSTGRES_TESTS") != "1":
        pytest.skip("live PostgreSQL tests require RUN_EVAL_POSTGRES_TESTS=1")
    url = os.environ.get("EVAL_TEST_DATABASE_URL")
    if not url:
        pytest.fail("RUN_EVAL_POSTGRES_TESTS=1 requires EVAL_TEST_DATABASE_URL")
    return url


def schema_engine(url, schema):
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool
    # NullPool: every asyncio.run gets fresh connections bound to its own loop.
    return create_async_engine(url, poolclass=NullPool,
                               connect_args={"server_settings": {"search_path": schema}})


@pytest.fixture
def live_schema():
    from sqlalchemy import text
    url = live_postgres_url()
    name = f"eval_test_{uuid4().hex[:16]}"
    assert LIVE_SCHEMA_PATTERN.fullmatch(name)
    admin = schema_engine(url, "public")

    async def execute(statement):
        async with admin.begin() as conn:
            await conn.execute(text(statement))

    asyncio.run(execute(f'CREATE SCHEMA "{name}"'))
    try:
        yield url, name
    finally:
        assert LIVE_SCHEMA_PATTERN.fullmatch(name)
        asyncio.run(execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE'))
        asyncio.run(admin.dispose())


@pytest.fixture
def pg_engine(live_schema):
    url, name = live_schema
    engine = schema_engine(url, name)
    yield engine
    asyncio.run(engine.dispose())


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def repo_factory(pg_engine, clock):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from services.api.app.evaluation.migrate import migrate
    from services.api.app.evaluation.outbox import OutboxRepository
    asyncio.run(migrate(pg_engine))

    def make(**kwargs):
        return OutboxRepository(async_sessionmaker(pg_engine, expire_on_commit=False), clock, **kwargs)
    return make


@pytest.fixture
def repo(repo_factory):
    return repo_factory()
