"""EBS mutations are bounded by ownership and completed snapshots."""

import json

import pytest

from scripts.staging_qdrant.aws_storage import (
    RecoveryError,
    delete_owned_snapshots,
    ensure_fresh_volumes,
    replacement_from_snapshots,
    snapshot_retained,
)
from scripts.staging_qdrant.inventory import UnsafeInventory, classify


CLUSTER = "rag-platform-cluster"


def volume_row(ordinal, *, attempt=None):
    tags = [
        {"Key": "kubernetes.io/created-for/pvc/name", "Value": f"qdrant-storage-qdrant-{ordinal}"},
        {"Key": "kubernetes.io/created-for/pvc/namespace", "Value": "default"},
        {"Key": "ebs.csi.aws.com/cluster-name", "Value": CLUSTER},
    ]
    if attempt:
        tags += [
            {"Key": "qdrant-volume-origin", "Value": "fresh"},
            {"Key": "recovery-attempt", "Value": attempt},
        ]
    return {
        "VolumeId": f"vol-{ordinal}", "State": "available", "AvailabilityZone": "us-east-1b",
        "Size": 50, "Attachments": [], "Tags": tags,
    }


class FakeAws:
    def __init__(self):
        self.calls = []
        self.snapshots = {}
        self.volumes = {}
        self.next_snapshot_state = "completed"
        self.next_volume_id = 100

    def __call__(self, args):
        self.calls.append(args)
        op = args[1]
        if op == "describe-snapshots":
            if "--snapshot-ids" in args:
                ids = args[args.index("--snapshot-ids") + 1:]
                return {"Snapshots": [self.snapshots[i] for i in ids]}
            return {"Snapshots": list(self.snapshots.values())}
        if op == "create-snapshot":
            volume_id = args[args.index("--volume-id") + 1]
            tags = json.loads(args[args.index("--tag-specifications") + 1])[0]["Tags"]
            snapshot_id = f"snap-{len(self.snapshots)}"
            self.snapshots[snapshot_id] = {
                "SnapshotId": snapshot_id, "VolumeId": volume_id,
                "State": self.next_snapshot_state, "Tags": tags,
            }
            return {"SnapshotId": snapshot_id}
        if op == "delete-snapshot":
            self.snapshots.pop(args[args.index("--snapshot-id") + 1])
            return {}
        if op == "create-volume":
            tags = json.loads(args[args.index("--tag-specifications") + 1])[0]["Tags"]
            volume_id = f"vol-{self.next_volume_id}"
            self.next_volume_id += 1
            self.volumes[volume_id] = {
                "VolumeId": volume_id, "State": "available", "AvailabilityZone": "us-east-1b",
                "Size": 50, "Attachments": [], "Tags": tags,
            }
            return {"VolumeId": volume_id}
        if op == "describe-volumes":
            if "--filters" in args:
                return {"Volumes": [row for row in self.volumes.values()
                        if {"Key": "recovery-attempt", "Value": args[3].split("Values=", 1)[1]} in row["Tags"]
                        and {"Key": "qdrant-volume-origin", "Value": "restored"} in row["Tags"]]}
            ids = args[args.index("--volume-ids") + 1:]
            return {"Volumes": [self.volumes[i] for i in ids]}
        raise AssertionError(f"unexpected fake operation: {op}")


def retained_inventory():
    return classify([{"Volumes": [volume_row(i) for i in range(3)]}], CLUSTER)


def test_snapshot_retained_records_three_completed_owned_snapshots():
    aws = FakeAws()
    result = snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0.1)
    assert result == {0: "snap-0", 1: "snap-1", 2: "snap-2"}
    assert [call[1] for call in aws.calls].count("create-snapshot") == 3
    assert all(aws.snapshots[sid]["State"] == "completed" for sid in result.values())
    assert all(any(tag == {"Key": "recovery-attempt", "Value": "attempt-1"}
                   for tag in aws.snapshots[sid]["Tags"]) for sid in result.values())


@pytest.mark.parametrize("state", ["pending", "error"])
def test_incomplete_or_failed_snapshot_never_returns_a_mountable_set(state):
    aws = FakeAws()
    aws.next_snapshot_state = state
    with pytest.raises(RecoveryError, match="snapshot"):
        snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0)
    assert not any(call[1] == "delete-snapshot" for call in aws.calls)


def test_partial_retry_does_not_create_duplicate_snapshots():
    aws = FakeAws()
    aws.next_snapshot_state = "error"
    with pytest.raises(RecoveryError):
        snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0)
    before = sum(call[1] == "create-snapshot" for call in aws.calls)
    with pytest.raises(RecoveryError):
        snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0)
    assert sum(call[1] == "create-snapshot" for call in aws.calls) == before


def test_cleanup_refuses_snapshot_from_another_attempt():
    aws = FakeAws()
    ids = snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0.1)
    with pytest.raises(RecoveryError, match="unowned"):
        delete_owned_snapshots(aws, ids, "attempt-2")
    assert len(aws.snapshots) == 3


def test_fresh_create_is_encrypted_and_tags_every_ordinal():
    aws = FakeAws()
    volumes = ensure_fresh_volumes(aws, classify([{"Volumes": []}], CLUSTER), "attempt-1")
    assert [v.ordinal for v in volumes] == [0, 1, 2]
    create_calls = [call for call in aws.calls if call[1] == "create-volume"]
    assert len(create_calls) == 3
    assert all("--encrypted" in call and "--availability-zone" in call for call in create_calls)
    assert all(v.tags["recovery-attempt"] == "attempt-1" for v in volumes)


def test_fresh_retry_creates_only_missing_ordinals():
    aws = FakeAws()
    inventory = classify([{"Volumes": [volume_row(0, attempt="attempt-1")]}], CLUSTER)
    volumes = ensure_fresh_volumes(aws, inventory, "attempt-1")
    assert [v.ordinal for v in volumes] == [0, 1, 2]
    assert len([call for call in aws.calls if call[1] == "create-volume"]) == 2


def test_foreign_fresh_attempt_cannot_be_extended():
    aws = FakeAws()
    inventory = classify([{"Volumes": [volume_row(0, attempt="attempt-1")]}], CLUSTER)
    with pytest.raises(UnsafeInventory):
        ensure_fresh_volumes(aws, inventory, "attempt-2")
    assert not aws.calls


def test_replacements_use_snapshots_without_deleting_originals():
    aws = FakeAws()
    ids = snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0.1)
    replacements = replacement_from_snapshots(aws, ids, "attempt-1")
    assert set(replacements) == {0, 1, 2}
    assert all("--snapshot-id" in call for call in aws.calls if call[1] == "create-volume")
    assert not any(call[1] == "delete-volume" for call in aws.calls)


def test_replacement_retry_reuses_exact_attempt_volumes():
    aws = FakeAws()
    ids = snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0.1)
    first = replacement_from_snapshots(aws, ids, "attempt-1")
    created = sum(call[1] == "create-volume" for call in aws.calls)
    second = replacement_from_snapshots(aws, ids, "attempt-1")
    assert second == first
    assert sum(call[1] == "create-volume" for call in aws.calls) == created


def test_replacement_partial_retry_creates_only_missing_ordinals():
    aws = FakeAws()
    ids = snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0.1)
    first = replacement_from_snapshots(aws, ids, "attempt-1")
    del aws.volumes[first[2]]
    retried = replacement_from_snapshots(aws, ids, "attempt-1")
    assert retried[0] == first[0] and retried[1] == first[1]
    assert retried[2] != first[2]


def test_duplicate_replacement_ordinal_fails_closed():
    aws = FakeAws()
    ids = snapshot_retained(aws, retained_inventory(), "attempt-1", timeout_seconds=0.1)
    first = replacement_from_snapshots(aws, ids, "attempt-1")
    duplicate = dict(aws.volumes[first[0]], VolumeId="vol-duplicate")
    aws.volumes["vol-duplicate"] = duplicate
    with pytest.raises(RecoveryError, match="ambiguous"):
        replacement_from_snapshots(aws, ids, "attempt-1")
    assert len([call for call in aws.calls if call[1] == "create-volume"]) == 3
