"""Evidence schema migrations: offline file contracts plus opt-in live PostgreSQL runs."""
import asyncio
import hashlib
import shutil

import pytest
from sqlalchemy import inspect, text

from services.api.app.evaluation import migrate as migrate_module
from services.api.app.evaluation.migrate import (
    MIGRATIONS_DIR, TRACKING_TABLE, MigrationChecksumMismatch, MigrationError, discover, migrate,
)
from services.api.app.evaluation.storage_models import EVIDENCE_METADATA

EVIDENCE_TABLES = {"evaluation_runs", "evaluation_outbox", "evaluation_payload_refs", "evaluation_upload_intents"}


# --- Offline contracts ---------------------------------------------------------------

def test_discovers_ordered_versions_with_sha256_checksums():
    migrations = discover(MIGRATIONS_DIR)
    assert [m.version for m in migrations] == [1]
    first = migrations[0]
    assert first.name == "001_evidence_outbox.sql"
    assert first.checksum == hashlib.sha256((MIGRATIONS_DIR / first.name).read_bytes()).hexdigest()


def test_rejects_malformed_and_duplicate_migration_files(tmp_path):
    (tmp_path / "001_a.sql").write_text("SELECT 1;")
    (tmp_path / "001_b.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover(tmp_path)
    shutil.rmtree(tmp_path)
    tmp_path.mkdir()
    (tmp_path / "first.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError):
        discover(tmp_path)


def test_migration_sql_is_additive_and_never_touches_chat_history():
    sql = (MIGRATIONS_DIR / "001_evidence_outbox.sql").read_text().lower()
    for forbidden in ("drop ", "truncate", "chat_history", "delete from"):
        assert forbidden not in sql
    for table in EVIDENCE_TABLES:
        assert f"create table {table}" in sql
    assert "for update skip locked" not in sql  # claims belong to the repository, not DDL


def test_evidence_metadata_is_independent_of_chat_history_base(terminal_event):
    from services.api.app.memory.postgres import Base as ChatBase
    assert EVIDENCE_METADATA is not ChatBase.metadata
    assert set(EVIDENCE_METADATA.tables) == EVIDENCE_TABLES
    assert not set(EVIDENCE_METADATA.tables) & set(ChatBase.metadata.tables)


def test_cli_reports_applied_versions_without_echoing_url(monkeypatch, capsys):
    calls = []

    async def fake_migrate(engine, directory=MIGRATIONS_DIR):
        calls.append(engine)
        return (1,)

    monkeypatch.setattr(migrate_module, "migrate", fake_migrate)
    url = "postgresql+asyncpg://secret-user:secret-pass@127.0.0.1:1/db"
    assert migrate_module.main([], database_url=url) == 0
    out = capsys.readouterr()
    assert "1" in out.out and "secret" not in out.out + out.err
    assert len(calls) == 1


def test_cli_failure_is_safe_and_nonzero(monkeypatch, capsys):
    async def failing(engine, directory=MIGRATIONS_DIR):
        raise MigrationChecksumMismatch(1)

    monkeypatch.setattr(migrate_module, "migrate", failing)
    assert migrate_module.main([], database_url="postgresql+asyncpg://u:secret-pass@127.0.0.1:1/db") == 1
    out = capsys.readouterr()
    assert "secret" not in out.out + out.err
    assert "checksum" in out.err


# --- Live PostgreSQL (RUN_EVAL_POSTGRES_TESTS=1) --------------------------------------

async def _tables(engine):
    async with engine.connect() as conn:
        return set(await conn.run_sync(lambda c: inspect(c).get_table_names()))


async def _tracking(engine):
    async with engine.connect() as conn:
        rows = await conn.execute(text(f"SELECT version, name, checksum FROM {TRACKING_TABLE} ORDER BY version"))
        return [tuple(r) for r in rows]


def test_migrate_applies_once_and_reapplication_is_noop(pg_engine):
    async def scenario():
        assert await migrate(pg_engine) == (1,)
        assert await migrate(pg_engine) == ()
        assert await _tables(pg_engine) == EVIDENCE_TABLES | {TRACKING_TABLE}
        tracked = await _tracking(pg_engine)
        assert tracked == [(1, "001_evidence_outbox.sql", discover(MIGRATIONS_DIR)[0].checksum)]
    asyncio.run(scenario())


def test_orm_metadata_matches_migrated_columns(pg_engine):
    async def scenario():
        await migrate(pg_engine)
        async with pg_engine.connect() as conn:
            def columns(sync_conn):
                inspector = inspect(sync_conn)
                return {t: {c["name"] for c in inspector.get_columns(t)} for t in EVIDENCE_TABLES}
            actual = await conn.run_sync(columns)
        expected = {t: {c.name for c in EVIDENCE_METADATA.tables[t].columns} for t in EVIDENCE_TABLES}
        assert actual == expected
    asyncio.run(scenario())


def test_changed_migration_file_is_a_detected_conflict(pg_engine, tmp_path):
    shutil.copy(MIGRATIONS_DIR / "001_evidence_outbox.sql", tmp_path / "001_evidence_outbox.sql")

    async def scenario():
        assert await migrate(pg_engine, directory=tmp_path) == (1,)
        before = await _tracking(pg_engine)
        with open(tmp_path / "001_evidence_outbox.sql", "a") as handle:
            handle.write("\n-- edited after application\n")
        with pytest.raises(MigrationChecksumMismatch):
            await migrate(pg_engine, directory=tmp_path)
        assert await _tracking(pg_engine) == before
    asyncio.run(scenario())


def test_applied_version_missing_from_directory_is_rejected(pg_engine, tmp_path):
    async def scenario():
        await migrate(pg_engine)
        with pytest.raises(MigrationError):
            await migrate(pg_engine, directory=tmp_path)
    asyncio.run(scenario())


def test_failed_migration_rolls_back_every_pending_version(pg_engine, tmp_path):
    shutil.copy(MIGRATIONS_DIR / "001_evidence_outbox.sql", tmp_path / "001_evidence_outbox.sql")
    (tmp_path / "002_broken.sql").write_text("CREATE TABLE evaluation_extra (id integer);\nSELEC broken;\n")

    async def scenario():
        with pytest.raises(MigrationError):
            await migrate(pg_engine, directory=tmp_path)
        assert await _tables(pg_engine) == set()
    asyncio.run(scenario())


def test_concurrent_startups_apply_exactly_once(pg_engine):
    async def scenario():
        results = await asyncio.gather(*(migrate(pg_engine) for _ in range(4)))
        assert sorted(results) == [(), (), (), (1,)]
        assert len(await _tracking(pg_engine)) == 1
    asyncio.run(scenario())
