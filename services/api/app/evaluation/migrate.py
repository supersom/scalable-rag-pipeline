"""Version-tracked, transactional SQL migrations for evidence tables.

All pending files are applied in one transaction under a database-wide advisory lock, so
concurrent startups serialize and a failure leaves nothing half-applied. Each applied file's
SHA-256 is recorded; an edited or missing applied file is a conflict, never silently skipped.

CLI: ``python -m services.api.app.evaluation.migrate`` (uses ``DATABASE_URL``).
"""
from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
TRACKING_TABLE = "evaluation_schema_migrations"
# Fixed application-wide key: sha256(b"scalable-rag:evaluation-evidence-migrations")[:8] as signed int64.
ADVISORY_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"scalable-rag:evaluation-evidence-migrations").digest()[:8], "big", signed=True)
_FILENAME = re.compile(r"^(\d{3})_[a-z0-9_]+\.sql$")


class MigrationError(RuntimeError):
    pass


class MigrationChecksumMismatch(MigrationError):
    def __init__(self, version: int) -> None:
        super().__init__(f"applied migration {version:03d} checksum differs from its file")
        self.version = version


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    checksum: str
    sql: str


def discover(directory: Path = MIGRATIONS_DIR) -> tuple[Migration, ...]:
    found: dict[int, Migration] = {}
    for path in sorted(Path(directory).glob("*.sql")):
        match = _FILENAME.match(path.name)
        if not match:
            raise MigrationError(f"malformed migration filename: {path.name}")
        version = int(match.group(1))
        if version in found:
            raise MigrationError(f"duplicate migration version {version:03d}")
        raw = path.read_bytes()
        found[version] = Migration(version, path.name, hashlib.sha256(raw).hexdigest(), raw.decode("utf-8"))
    return tuple(found[v] for v in sorted(found))


async def migrate(engine: AsyncEngine, directory: Path = MIGRATIONS_DIR) -> tuple[int, ...]:
    """Apply pending migrations; return the versions applied by this call."""
    migrations = discover(directory)
    by_version = {m.version: m for m in migrations}
    async with engine.begin() as conn:
        await conn.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
        # Created under the lock so concurrent first startups cannot race on the catalog.
        await conn.execute(text(
            f"CREATE TABLE IF NOT EXISTS {TRACKING_TABLE} ("
            " version integer PRIMARY KEY, name text NOT NULL,"
            " checksum char(64) NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())"))
        applied = {row.version: row.checksum for row in
                   await conn.execute(text(f"SELECT version, checksum FROM {TRACKING_TABLE}"))}
        for version, checksum in applied.items():
            if version not in by_version:
                raise MigrationError(f"applied migration {version:03d} has no file")
            if by_version[version].checksum != checksum:
                raise MigrationChecksumMismatch(version)
        pending = [m for m in migrations if m.version not in applied]
        if pending:
            raw = await conn.get_raw_connection()
            driver = raw.driver_connection  # asyncpg: simple-query protocol runs multi-statement files
            for migration in pending:
                try:
                    await driver.execute(migration.sql)
                except Exception as exc:
                    raise MigrationError(
                        f"migration {migration.version:03d} failed: {type(exc).__name__}") from exc
                await conn.execute(
                    text(f"INSERT INTO {TRACKING_TABLE} (version, name, checksum) VALUES (:v, :n, :c)"),
                    {"v": migration.version, "n": migration.name, "c": migration.checksum})
    return tuple(m.version for m in pending)


async def _run(database_url: str) -> tuple[int, ...]:
    engine = create_async_engine(database_url)
    try:
        return await migrate(engine)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None, *, database_url: str | None = None) -> int:
    if database_url is None:
        from services.api.app.config import settings
        database_url = settings.DATABASE_URL
    try:
        applied = asyncio.run(_run(database_url))
    except MigrationError as exc:
        print(f"evidence migration failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # connection/driver failures: report the class only, never the URL
        print(f"evidence migration failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print("applied evidence migrations: " + (", ".join(f"{v:03d}" for v in applied) or "none"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
