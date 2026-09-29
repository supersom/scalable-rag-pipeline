"""Maintenance CLI: explicit modes/targets, fixed content-free output, explicit operator retry."""
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from services.evaluation_delivery import maintenance

from retention_fakes import TENANT, FakeRepo, FakeStore, calls, run_namespace, versions_for

T0 = datetime(2026, 3, 1, tzinfo=timezone.utc)
OUTPUT_KEYS = ["mode", "runs_expired", "namespaces_examined", "namespaces_eligible", "namespaces_cleaned",
               "versions_examined", "versions_deleted", "failures"]


def resources_for(repo, store, clock, opened=None):
    @asynccontextmanager
    async def open_resources():
        if opened is not None:
            opened.append(True)
        yield repo, store, clock
    return open_resources


def fields(line):
    command, *pairs = line.split()
    return command, dict(pair.split("=", 1) for pair in pairs)


@pytest.fixture
def events():
    return []


def test_cleanup_dry_run_prints_only_fixed_counts(capsys, events):
    ns = run_namespace(1)
    clock = lambda: T0
    repo = FakeRepo(clock, events, candidates=[ns], expired_runs=2)
    store = FakeStore(events, {ns: versions_for(ns, 2, markers=1)})
    code = maintenance.main(["cleanup", "--dry-run", "--limit", "5"], open_resources=resources_for(repo, store, clock))
    out, err = capsys.readouterr()
    assert code == maintenance.EXIT_OK
    command, values = fields(out.strip())
    assert command == "cleanup" and list(values) == OUTPUT_KEYS
    assert values == {"mode": "dry-run", "runs_expired": "2", "namespaces_examined": "1",
                      "namespaces_eligible": "1", "namespaces_cleaned": "0", "versions_examined": "3",
                      "versions_deleted": "0", "failures": "0"}
    assert "claim_orphan" not in calls(events) and "expire_runs" not in calls(events)
    assert TENANT not in out + err and ns not in out + err


def test_cleanup_apply_with_explicit_namespace_never_echoes_it(capsys, events):
    ns = run_namespace(7)
    clock = lambda: T0
    repo = FakeRepo(clock, events, eligible=[ns])
    store = FakeStore(events, {ns: versions_for(ns, 1)})
    code = maintenance.main(["cleanup", "--apply", "--limit", "5", "--namespace", ns],
                            open_resources=resources_for(repo, store, clock))
    out, err = capsys.readouterr()
    assert code == maintenance.EXIT_OK
    assert fields(out.strip())[1]["versions_deleted"] == "1"
    assert ns not in out + err and "cleanup_candidates" not in calls(events)


def test_cleanup_failures_exit_incomplete(capsys, events):
    ns = run_namespace(1)
    clock = lambda: T0
    repo = FakeRepo(clock, events, candidates=[ns])
    repo.unavailable.add("claim_orphan")
    code = maintenance.main(["cleanup", "--apply", "--limit", "5"],
                            open_resources=resources_for(repo, FakeStore(events), clock))
    assert code == maintenance.EXIT_INCOMPLETE
    assert fields(capsys.readouterr().out.strip())[1]["failures"] == "1"


@pytest.mark.parametrize("argv", [
    ["cleanup", "--limit", "5"],                                 # mode is mandatory
    ["cleanup", "--dry-run", "--apply", "--limit", "5"],         # modes are exclusive
    ["cleanup", "--apply"],                                      # limit is mandatory
    ["cleanup", "--apply", "--limit", "0"],
    ["cleanup", "--apply", "--limit", "-3"],
    ["cleanup", "--apply", "--limit", "5", "--namespace", f"evidence/test/{TENANT}/"],
    ["cleanup", "--apply", "--limit", "5", "--namespace", "evidence/"],
    ["retry", "--run-id", "not-a-uuid", "--export-version", "1.0", "--run-schema-version", "1.0"],
    ["retry", "--run-id", str(uuid4()), "--export-version", "2.0", "--run-schema-version", "1.0"],
    ["retry", "--run-id", str(uuid4()), "--export-version", "1.0", "--run-schema-version", "0.9"],
    ["retry", "--run-id", str(uuid4())],
    [],
])
def test_invalid_arguments_are_rejected_before_any_resource(capsys, argv):
    opened = []
    code = maintenance.main(argv, open_resources=resources_for(None, None, None, opened))
    assert code == maintenance.EXIT_CONFIGURATION and opened == []
    out, err = capsys.readouterr()
    assert "not-a-uuid" not in out + err and TENANT not in out + err


def test_resource_failure_is_fixed_message_only(capsys):
    @asynccontextmanager
    async def broken():
        raise RuntimeError("postgresql://secret@db/evidence leaked text")
        yield
    code = maintenance.main(["cleanup", "--dry-run", "--limit", "1"], open_resources=broken)
    out, err = capsys.readouterr()
    assert code == maintenance.EXIT_RUNTIME_FAILURE
    assert "secret" not in out + err and "leaked" not in out + err


def test_retry_prints_fixed_status(capsys, events):
    clock = lambda: T0
    repo = FakeRepo(clock, events)
    run_id = uuid4()
    code = maintenance.main(["retry", "--run-id", str(run_id), "--export-version", "1.0",
                             "--run-schema-version", "1.0"], open_resources=resources_for(repo, None, clock))
    out, err = capsys.readouterr()
    assert code == maintenance.EXIT_INCOMPLETE
    assert out.strip() == "retry status=not_requeued"
    assert str(run_id) not in out + err
    assert calls(events) == ["retry_quarantined"]


# --- Live PostgreSQL retry --------------------------------------------------------------

def retry_argv(run_id):
    return ["retry", "--run-id", str(run_id), "--export-version", "1.0", "--run-schema-version", "1.0"]


@pytest.fixture
def quarantined(repo, envelope):
    async def setup():
        assert (await repo.enqueue(envelope, intent=None)).status == "committed"
        claim, = await repo.claim_batch(limit=1, lease_seconds=60)
        assert await repo.schedule_retry(claim, "broker_denied", permanent=True)
        return claim
    return asyncio.run(setup())


def run_cli(argv, repo, clock):
    return maintenance.main(argv, open_resources=resources_for(repo, None, clock))


def test_retry_requeues_quarantine_preserving_bytes_and_hash(capsys, repo, clock, envelope, quarantined):
    assert run_cli(retry_argv(envelope.run_id), repo, clock) == maintenance.EXIT_OK
    assert capsys.readouterr().out.strip() == "retry status=requeued"
    claim, = asyncio.run(repo.claim_batch(limit=1, lease_seconds=60))
    assert claim.envelope_bytes == envelope.canonical_bytes() and claim.export_hash == envelope.export_hash
    assert claim.attempts == 1  # a fresh, explicit retry budget
    # The lease that led to quarantine is stale and can never acknowledge.
    from services.api.app.evaluation.delivery_models import PublishReceipt
    assert asyncio.run(repo.mark_delivered(quarantined, PublishReceipt(acknowledgement_id="late"))) is False


def test_retry_cannot_revive_expired_metadata(capsys, repo, clock, envelope, quarantined):
    clock.now = envelope.safe_run.completed_at + timedelta(days=90)  # exactly at expiry
    assert run_cli(retry_argv(envelope.run_id), repo, clock) == maintenance.EXIT_INCOMPLETE
    assert capsys.readouterr().out.strip() == "retry status=not_requeued"
    assert asyncio.run(repo.claim_batch(limit=1, lease_seconds=60)) == ()


def test_retry_of_missing_or_other_identity_changes_nothing(capsys, repo, clock, envelope, quarantined, pg_engine):
    assert run_cli(retry_argv(uuid4()), repo, clock) == maintenance.EXIT_INCOMPLETE
    assert capsys.readouterr().out.strip() == "retry status=not_requeued"
    assert asyncio.run(_states(pg_engine)) == [("quarantined", 1)]


def test_retry_refuses_live_or_stale_leased_rows(capsys, repo, clock, envelope, pg_engine):
    async def lease():
        assert (await repo.enqueue(envelope, intent=None)).status == "committed"
        return (await repo.claim_batch(limit=1, lease_seconds=60))[0]
    claim = asyncio.run(lease())
    assert run_cli(retry_argv(envelope.run_id), repo, clock) == maintenance.EXIT_INCOMPLETE
    clock.advance(seconds=120)  # the lease is now stale, but the row is not quarantined
    assert run_cli(retry_argv(envelope.run_id), repo, clock) == maintenance.EXIT_INCOMPLETE
    assert asyncio.run(_states(pg_engine)) == [("leased", 1)]
    assert claim.attempts == 1


def test_no_automatic_requeue_without_the_command(repo, clock, envelope, quarantined, pg_engine):
    clock.advance(days=1)
    assert asyncio.run(repo.claim_batch(limit=10, lease_seconds=60)) == ()
    assert asyncio.run(_states(pg_engine)) == [("quarantined", 1)]


async def _states(engine):
    from sqlalchemy import text
    async with engine.connect() as conn:
        return [tuple(r) for r in await conn.execute(text("SELECT state, attempts FROM evaluation_outbox"))]


def test_production_resource_path_wires_metrics_into_retention(monkeypatch, events):
    captured = {}

    class SpyRetentionService:
        def __init__(self, repo, store, clock, **kwargs):
            captured.update(kwargs)

        async def run_once(self, **_kwargs):
            from services.api.app.evaluation.delivery_models import CleanupReport
            return CleanupReport()

    monkeypatch.setattr(maintenance, "RetentionService", SpyRetentionService)
    ns = run_namespace(1)
    repo = FakeRepo(lambda: T0, events, candidates=[ns], expired_runs=0)
    store = FakeStore(events, {})
    code = maintenance.main(["cleanup", "--dry-run", "--limit", "5"],
                            open_resources=resources_for(repo, store, lambda: T0))
    assert code == maintenance.EXIT_OK
    assert "metrics" in captured
    from services.api.app.evaluation.delivery_metrics import DeliveryMetrics
    assert isinstance(captured["metrics"], DeliveryMetrics)


@pytest.mark.parametrize("argv", [["cleanup", "--dry-run", "--limit", "1"], retry_argv(uuid4())])
def test_configuration_failure_exits_2_before_any_resource(capsys, monkeypatch, argv):
    for name, value in {"DATABASE_URL": "postgresql+asyncpg://test:test@localhost/test",
                        "NEO4J_PASSWORD": "synthetic-password",
                        "S3_BUCKET_NAME": "synthetic-documents", "JWT_SECRET_KEY": "synthetic-jwt-secret"}.items():
        monkeypatch.setenv(name, value)  # importing the settings module builds module-level settings
    import services.api.app.config as config_module

    def broken_settings():
        raise ValueError("DATABASE_URL=postgresql://secret@db leaked text")
    monkeypatch.setattr(config_module, "Settings", broken_settings)
    opened = []
    monkeypatch.setattr(maintenance, "open_configured_resources",
                        lambda config: opened.append(config))  # must never be reached
    assert maintenance.main(argv) == maintenance.EXIT_CONFIGURATION
    out, err = capsys.readouterr()
    assert opened == [] and "secret" not in out + err and "leaked" not in out + err
