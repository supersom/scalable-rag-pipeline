"""Guarded EBS snapshot and volume operations for staging Qdrant."""

import hashlib
import json
import subprocess
import time
from collections.abc import Callable

from .inventory import (
    CLUSTER_TAG,
    NAMESPACE_TAG,
    PVC_TAG,
    Inventory,
    UnsafeInventory,
    Volume,
    tags_of,
)


class RecoveryError(RuntimeError):
    """An operation stopped without declaring the Qdrant data safe to use."""


def aws_json(args: list[str], *, region: str = "us-east-1") -> dict:
    """Run one AWS CLI JSON operation, without exposing CLI stderr in errors."""
    result = subprocess.run(
        ["aws", *args, "--region", region, "--output", "json"],
        text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RecoveryError(f"AWS {args[0]} {args[1]} failed; inspect AWS access separately")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RecoveryError("AWS returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise RecoveryError("AWS returned an unexpected response")
    return payload


def _tags(values: dict[str, str], resource_type: str) -> str:
    return json.dumps([{"ResourceType": resource_type, "Tags": [
        {"Key": key, "Value": value} for key, value in sorted(values.items())
    ]}], separators=(",", ":"))


def _fingerprint(inventory: Inventory) -> str:
    volume_ids = [v.volume_id for v in inventory.volumes]
    return hashlib.sha256("|".join(volume_ids).encode()).hexdigest()


def _all_snapshots(aws_call: Callable[[list[str]], dict], attempt_id: str) -> list[dict]:
    rows: list[dict] = []
    token = None
    while True:
        args = ["ec2", "describe-snapshots", "--filters", f"Name=tag:recovery-attempt,Values={attempt_id}"]
        if token:
            args += ["--next-token", token]
        result = aws_call(args)
        rows.extend(result.get("Snapshots", []))
        token = result.get("NextToken")
        if not token:
            return rows


def _snapshot_map(rows: list[dict], inventory: Inventory, attempt_id: str) -> dict[int, str]:
    fingerprint = _fingerprint(inventory)
    volume_ids = {v.ordinal: v.volume_id for v in inventory.volumes}
    mapping: dict[int, str] = {}
    for row in rows:
        tags = tags_of(row)
        if tags.get("recovery-attempt") != attempt_id:
            continue
        try:
            ordinal = int(tags["qdrant-ordinal"])
        except (KeyError, ValueError) as exc:
            raise RecoveryError("snapshot has malformed ownership tags") from exc
        if (ordinal not in volume_ids or ordinal in mapping
                or tags.get("volume-set-sha256") != fingerprint
                or row.get("VolumeId") != volume_ids[ordinal]):
            raise RecoveryError("snapshot set does not match retained volumes")
        snapshot_id = row.get("SnapshotId")
        if not isinstance(snapshot_id, str) or not snapshot_id:
            raise RecoveryError("snapshot ID is missing")
        mapping[ordinal] = snapshot_id
    return mapping


def snapshot_retained(
    aws_call: Callable[[list[str]], dict], inventory: Inventory, attempt_id: str,
    *, timeout_seconds: float = 3600, poll_interval_seconds: float = 5,
) -> dict[int, str]:
    """Return only an exact, completed three-snapshot set for retained disks."""
    if inventory.kind != "retained" or len(inventory.volumes) != 3:
        raise UnsafeInventory("only a complete retained set may be snapshotted")
    if any(v.state != "available" or v.attachments for v in inventory.volumes):
        raise UnsafeInventory("retained disks must be detached before first snapshot")
    existing = _snapshot_map(_all_snapshots(aws_call, attempt_id), inventory, attempt_id)
    if existing and set(existing) != {0, 1, 2}:
        raise RecoveryError("incomplete snapshot attempt; inspect before retry")
    mapping = existing
    if not mapping:
        fingerprint = _fingerprint(inventory)
        mapping = {}
        for volume in inventory.volumes:
            tags = {
                "recovery-attempt": attempt_id,
                "qdrant-ordinal": str(volume.ordinal),
                "qdrant-cluster": volume.tags[CLUSTER_TAG],
                "volume-set-sha256": fingerprint,
            }
            result = aws_call([
                "ec2", "create-snapshot", "--volume-id", volume.volume_id,
                "--description", f"staging Qdrant recovery {attempt_id} ordinal {volume.ordinal}",
                "--tag-specifications", _tags(tags, "snapshot"),
            ])
            snapshot_id = result.get("SnapshotId")
            if not isinstance(snapshot_id, str) or not snapshot_id:
                raise RecoveryError("snapshot creation returned no ID")
            mapping[volume.ordinal] = snapshot_id
    deadline = time.monotonic() + timeout_seconds
    while True:
        result = aws_call(["ec2", "describe-snapshots", "--snapshot-ids", *mapping.values()])
        rows = result.get("Snapshots", [])
        confirmed = _snapshot_map(rows, inventory, attempt_id)
        if confirmed != mapping:
            raise RecoveryError("snapshot ownership changed during verification")
        states = {row.get("State") for row in rows}
        if states == {"completed"} and len(rows) == 3:
            return mapping
        if any(state not in {"pending", "completed"} for state in states):
            raise RecoveryError("snapshot failed; original volumes preserved")
        if time.monotonic() >= deadline:
            raise RecoveryError("snapshot completion timed out; original volumes preserved")
        time.sleep(poll_interval_seconds)


def _wait_volume(
    aws_call: Callable[[list[str]], dict], volume_id: str,
    *, timeout_seconds: float = 600, poll_interval_seconds: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    while True:
        rows = aws_call(["ec2", "describe-volumes", "--volume-ids", volume_id]).get("Volumes", [])
        if len(rows) != 1 or rows[0].get("VolumeId") != volume_id:
            raise RecoveryError("created volume could not be verified")
        row = rows[0]
        if row.get("State") == "available":
            return row
        if row.get("State") != "creating" or time.monotonic() >= deadline:
            raise RecoveryError("created volume did not become available")
        time.sleep(poll_interval_seconds)


def _fresh_tags(ordinal: int, attempt_id: str, cluster: str) -> dict[str, str]:
    return {
        PVC_TAG: f"qdrant-storage-qdrant-{ordinal}",
        NAMESPACE_TAG: "default",
        CLUSTER_TAG: cluster,
        "qdrant-volume-origin": "fresh",
        "recovery-attempt": attempt_id,
    }


def ensure_fresh_volumes(
    aws_call: Callable[[list[str]], dict], inventory: Inventory, attempt_id: str,
    *, cluster: str = "rag-platform-cluster",
) -> tuple[Volume, ...]:
    if inventory.kind not in {"fresh", "resume_fresh"}:
        raise UnsafeInventory("retained volumes cannot be extended with fresh disks")
    if inventory.attempt_id and inventory.attempt_id != attempt_id:
        raise UnsafeInventory("fresh volume belongs to another attempt")
    volumes = {v.ordinal: v for v in inventory.volumes}
    for ordinal in sorted({0, 1, 2} - set(volumes)):
        result = aws_call([
            "ec2", "create-volume", "--availability-zone", "us-east-1b", "--size", "50",
            "--volume-type", "gp3", "--encrypted",
            "--tag-specifications", _tags(_fresh_tags(ordinal, attempt_id, cluster), "volume"),
        ])
        volume_id = result.get("VolumeId")
        if not isinstance(volume_id, str) or not volume_id:
            raise RecoveryError("fresh volume creation returned no ID")
        row = _wait_volume(aws_call, volume_id)
        tags = tags_of(row)
        if tags != _fresh_tags(ordinal, attempt_id, cluster):
            raise RecoveryError("fresh volume tags do not match requested ordinal")
        volumes[ordinal] = Volume(
            ordinal, volume_id, row["State"], row["AvailabilityZone"], row["Size"], tags, (),
        )
    return tuple(volumes[i] for i in range(3))


def replacement_from_snapshots(
    aws_call: Callable[[list[str]], dict], snapshot_ids: dict[int, str], attempt_id: str,
) -> dict[int, str]:
    if set(snapshot_ids) != {0, 1, 2}:
        raise RecoveryError("replacement requires three snapshots")
    rows = aws_call(["ec2", "describe-snapshots", "--snapshot-ids", *snapshot_ids.values()]).get("Snapshots", [])
    by_id = {row.get("SnapshotId"): row for row in rows}
    for ordinal, snapshot_id in snapshot_ids.items():
        row = by_id.get(snapshot_id)
        tags = tags_of(row) if row else {}
        if (not row or row.get("State") != "completed"
                or tags.get("recovery-attempt") != attempt_id
                or tags.get("qdrant-ordinal") != str(ordinal)):
            raise RecoveryError("replacement source snapshot is not verified")
    replacements: dict[int, str] = {}
    for ordinal, snapshot_id in sorted(snapshot_ids.items()):
        tags = {
            "qdrant-recovery-ordinal": str(ordinal),
            "qdrant-volume-origin": "restored",
            "recovery-attempt": attempt_id,
            "restored-from-snapshot": snapshot_id,
        }
        result = aws_call([
            "ec2", "create-volume", "--snapshot-id", snapshot_id,
            "--availability-zone", "us-east-1b", "--volume-type", "gp3", "--encrypted",
            "--tag-specifications", _tags(tags, "volume"),
        ])
        volume_id = result.get("VolumeId")
        if not isinstance(volume_id, str) or not volume_id:
            raise RecoveryError("replacement volume creation returned no ID")
        _wait_volume(aws_call, volume_id)
        replacements[ordinal] = volume_id
    return replacements


def delete_owned_snapshots(
    aws_call: Callable[[list[str]], dict], snapshot_ids: dict[int, str], attempt_id: str,
) -> None:
    if set(snapshot_ids) != {0, 1, 2}:
        raise RecoveryError("refusing to delete incomplete snapshot set")
    rows = aws_call(["ec2", "describe-snapshots", "--snapshot-ids", *snapshot_ids.values()]).get("Snapshots", [])
    by_id = {row.get("SnapshotId"): row for row in rows}
    for ordinal, snapshot_id in snapshot_ids.items():
        row = by_id.get(snapshot_id)
        tags = tags_of(row) if row else {}
        if (not row or row.get("State") != "completed"
                or tags.get("recovery-attempt") != attempt_id
                or tags.get("qdrant-ordinal") != str(ordinal)):
            raise RecoveryError("refusing to delete unowned snapshot")
    for ordinal, snapshot_id in sorted(snapshot_ids.items()):
        aws_call(["ec2", "delete-snapshot", "--snapshot-id", snapshot_id])
