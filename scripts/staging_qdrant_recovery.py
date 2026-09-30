#!/usr/bin/env python3
"""Guarded bring-up of persistent staging Qdrant in the shared EKS cluster."""

from __future__ import annotations

import argparse
import json
import sys
from uuid import uuid4

from scripts.staging_qdrant.aws_storage import RecoveryError


QDRANT_HOST = "qdrant.default.svc.cluster.local"


def run_recovery(runtime, mode: str, *, attempt_id: str | None = None) -> dict:
    """Execute one phase; snapshots are deleted only by a verified finalize."""
    if mode == "check":
        runtime.preflight()
        inventory = runtime.inventory()
        return {"status": "checked", "branch": inventory.kind,
                "volumes": {str(v.ordinal): v.volume_id for v in inventory.volumes}}

    if mode == "apply":
        runtime.preflight()
        inventory = runtime.inventory()
        if runtime.existing_ready(inventory):
            pending = runtime.pending_attempt(inventory)
            result = {"status": "ready", "branch": "reuse", "host": QDRANT_HOST,
                      "volumes": {str(v.ordinal): v.volume_id for v in inventory.volumes}}
            if pending:
                result["attempt_id"] = pending["attempt_id"]
                result["snapshots"] = pending.get("snapshots", {})
            return result
        selected_attempt = runtime.select_attempt(inventory)
        previous = runtime.load_attempt(selected_attempt) if runtime.attempt_exists(selected_attempt) else None
        if previous is None:
            previous = {"attempt_id": selected_attempt,
                        "branch": "retained" if inventory.kind == "retained" else "fresh",
                        "volumes": {str(v.ordinal): v.volume_id for v in inventory.volumes},
                        "snapshots": {}, "version": None,
                        "previous_api_revision": runtime.previous_api_revision()}
            runtime.save_attempt(previous)
        if inventory.kind == "retained":
            snapshots = runtime.snapshot(inventory, selected_attempt)
            volumes = inventory.volumes
            version = None
            branch = "retained"
        elif inventory.kind in {"fresh", "resume_fresh"}:
            snapshots = {}
            volumes = runtime.fresh(inventory, selected_attempt)
            version = "v1.19.1"
            branch = "fresh"
        else:
            raise RecoveryError("unsupported inventory branch")
        attempt = {**previous, "branch": branch,
                   "volumes": {str(v.ordinal): v.volume_id for v in volumes},
                   "snapshots": {str(i): snapshot_id for i, snapshot_id in snapshots.items()},
                   "version": version}
        runtime.save_attempt(attempt)
        runtime.bind(volumes)
        if branch == "retained":
            version = runtime.probe(volumes)
            attempt["version"] = version
            runtime.save_attempt(attempt)
        runtime.install(version)
        runtime.ensure_collection(branch)
        report = runtime.verify_qdrant(retained=branch == "retained")
        return {"status": "ready_for_api", "branch": branch, "attempt_id": selected_attempt,
                "host": QDRANT_HOST, "volumes": attempt["volumes"],
                "snapshots": attempt["snapshots"], "point_count": report["point_count"]}

    if mode == "finalize":
        if not attempt_id:
            raise RecoveryError("finalize requires an attempt ID")
        attempt = runtime.load_attempt(attempt_id)
        try:
            runtime.verify_api(retained=attempt["branch"] in {"retained", "restored"})
        except Exception:
            runtime.restore_api_host(attempt)
            raise
        if attempt["branch"] == "restored":
            runtime.promote_replacements(attempt)
        if attempt.get("snapshots"):
            runtime.delete_snapshots({int(i): sid for i, sid in attempt["snapshots"].items()}, attempt_id)
        attempt["status"] = "verified"
        runtime.save_attempt(attempt)
        return {"status": "verified", "branch": attempt["branch"],
                "attempt_id": attempt_id, "host": QDRANT_HOST}

    if mode == "restore":
        if not attempt_id:
            raise RecoveryError("restore requires an attempt ID")
        attempt = runtime.load_attempt(attempt_id)
        snapshots = {int(i): sid for i, sid in attempt.get("snapshots", {}).items()}
        if set(snapshots) != {0, 1, 2}:
            raise RecoveryError("restore requires a complete protected snapshot set")
        if attempt.get("version") not in {"v1.7.3", "v1.19.1"}:
            raise RecoveryError("restore requires a verified pinned Qdrant version")
        runtime.stop_qdrant()
        replacements = runtime.replacement_volumes(snapshots, attempt_id)
        runtime.rebind_preserving_originals(replacements)
        runtime.install(attempt["version"])
        report = runtime.verify_qdrant(retained=True)
        attempt["branch"] = "restored"
        attempt["replacements"] = {str(i): volume_id for i, volume_id in replacements.items()}
        runtime.save_attempt(attempt)
        return {"status": "ready_for_api", "branch": "restored", "attempt_id": attempt_id,
                "host": QDRANT_HOST, "volumes": attempt["replacements"],
                "snapshots": attempt["snapshots"], "point_count": report["point_count"]}

    if mode == "rollback-api":
        if not attempt_id:
            raise RecoveryError("rollback-api requires an attempt ID")
        attempt = runtime.load_attempt(attempt_id)
        runtime.restore_api_host(attempt)
        return {"status": "api_rolled_back", "attempt_id": attempt_id}

    raise RecoveryError("unknown recovery mode")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("check", "apply", "finalize", "restore", "rollback-api"))
    parser.add_argument("--region", default="us-east-1")
    parser.add_argument("--cluster", default="rag-platform-cluster")
    parser.add_argument("--namespace", default="staging-evidence")
    parser.add_argument("--attempt-id")
    args = parser.parse_args(argv)
    from scripts.staging_qdrant.runtime import ClusterRuntime

    try:
        result = run_recovery(
            ClusterRuntime(region=args.region, cluster=args.cluster, namespace=args.namespace),
            args.mode, attempt_id=args.attempt_id,
        )
    except (RecoveryError, ValueError) as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
