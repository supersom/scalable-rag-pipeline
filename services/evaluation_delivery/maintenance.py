"""Operator maintenance commands for evaluation evidence: retention cleanup and explicit retry.

    python -m services.evaluation_delivery.maintenance cleanup --dry-run|--apply --limit N [--namespace NS ...]
    python -m services.evaluation_delivery.maintenance retry --run-id UUID --export-version 1.0 --run-schema-version 1.0

``cleanup`` expires metadata past retention and removes orphaned or expired content versions through
``RetentionService``; without ``--namespace`` it discovers candidates from the coordination table.
``retry`` requeues one quarantined record with its stored bytes/hash; it never revives expired
metadata and is the only way a quarantined record returns to delivery. Output is a single line of
fixed keys with counts or a status: never keys, run IDs, payload bytes, or exception messages.

Exit codes: 0 done, 1 runtime failure, 2 invalid arguments or configuration, 3 incomplete (cleanup
failures, or the retry was not requeued).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

from services.api.app.evaluation.delivery_models import EventIdentity
from services.api.app.evaluation.retention import RUN_NAMESPACE, RetentionService

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_RUNTIME_FAILURE = 1
EXIT_CONFIGURATION = 2
EXIT_INCOMPLETE = 3

_REPORT_KEYS = ("runs_expired", "namespaces_examined", "namespaces_eligible", "namespaces_cleaned",
                "versions_examined", "versions_deleted", "failures")


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _positive(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def _namespace(value: str) -> str:
    if not RUN_NAMESPACE.fullmatch(value):
        raise argparse.ArgumentTypeError("must be one exact run namespace")
    return value


def _uuid(value: str) -> UUID:
    try:
        return UUID(value)
    except ValueError:
        raise argparse.ArgumentTypeError("must be a UUID") from None


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):
        # argparse messages can echo operator input; report a fixed line instead.
        raise _UsageError()


class _UsageError(Exception):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m services.evaluation_delivery.maintenance")
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_Parser)
    cleanup = commands.add_parser("cleanup", help="expire evidence and clean orphaned content")
    mode = cleanup.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="read-only: report what apply would do")
    mode.add_argument("--apply", action="store_true", help="expire and delete")
    cleanup.add_argument("--limit", type=_positive, required=True, help="maximum runs/namespaces per cycle")
    cleanup.add_argument("--namespace", type=_namespace, action="append", dest="namespaces",
                         help="explicit run namespace to clean instead of discovery (repeatable)")
    retry = commands.add_parser("retry", help="requeue one quarantined, unexpired record")
    retry.add_argument("--run-id", type=_uuid, required=True)
    retry.add_argument("--export-version", choices=("1.0",), required=True)
    retry.add_argument("--run-schema-version", choices=("1.0",), required=True)
    return parser


@asynccontextmanager
async def open_resources():
    """Engine, repository and (when content storage is configured) payload store; always released."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from services.api.app.config import Settings
    from services.api.app.evaluation.delivery_config import DeliveryConfig
    from services.api.app.evaluation.outbox import OutboxRepository
    from services.api.app.evaluation.payloads import S3PayloadStore

    config = DeliveryConfig.from_settings(Settings())
    engine = create_async_engine(config.database_url, pool_size=1, pool_pre_ping=True)
    store = None
    try:
        repo = OutboxRepository(
            async_sessionmaker(engine, expire_on_commit=False), utc_now, max_attempts=config.producer_attempts,
            metadata_retention_days=config.metadata_retention_days,
            content_retention_days=config.content_retention_days, orphan_grace_seconds=config.orphan_grace_seconds)
        # Without a configured bucket there is nothing to enumerate: metadata expiry only, never an
        # empty listing that would record cleanup as complete.
        if config.bucket and config.kms_key_id:
            store = S3PayloadStore.from_config(config, clock=utc_now)
        yield repo, store, utc_now
    finally:
        if store is not None:
            await store.close(time.monotonic() + config.shutdown_drain_seconds)
        await engine.dispose()


async def _cleanup(args, resources) -> int:
    async with resources() as (repo, store, clock):
        report = await RetentionService(repo, store, clock).run_once(
            apply=args.apply, limit=args.limit, namespaces=tuple(args.namespaces) if args.namespaces else None)
    counts = " ".join(f"{key}={getattr(report, key)}" for key in _REPORT_KEYS)
    print(f"cleanup mode={'apply' if args.apply else 'dry-run'} {counts}")
    return EXIT_INCOMPLETE if report.failures else EXIT_OK


async def _retry(args, resources) -> int:
    identity = EventIdentity(export_version=args.export_version, run_schema_version=args.run_schema_version,
                             run_id=args.run_id)
    async with resources() as (repo, _store, _clock):
        requeued = await repo.retry_quarantined(identity)
    print(f"retry status={'requeued' if requeued else 'not_requeued'}")
    return EXIT_OK if requeued else EXIT_INCOMPLETE


def main(argv: list[str] | None = None, *, open_resources=open_resources) -> int:
    """Only fixed messages are logged: configuration and driver errors can echo URLs or credentials."""
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    try:
        args = _parser().parse_args(argv)
    except _UsageError:
        logger.error("evidence maintenance arguments invalid")
        return EXIT_CONFIGURATION
    command = _cleanup if args.command == "cleanup" else _retry
    try:
        return asyncio.run(command(args, open_resources))
    except Exception:
        logger.error("evidence maintenance %s failed", args.command)
        return EXIT_RUNTIME_FAILURE


if __name__ == "__main__":
    sys.exit(main())
