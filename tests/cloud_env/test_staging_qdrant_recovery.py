"""The deploy state machine never trades its last recovery point for readiness."""

import pytest

from scripts.staging_qdrant.aws_storage import RecoveryError
from scripts.staging_qdrant.inventory import UnsafeInventory, Volume, classify
from scripts.staging_qdrant_recovery import run_recovery


def volume(ordinal, *, attempt=None):
    tags = [
        {"Key": "kubernetes.io/created-for/pvc/name", "Value": f"qdrant-storage-qdrant-{ordinal}"},
        {"Key": "kubernetes.io/created-for/pvc/namespace", "Value": "default"},
        {"Key": "ebs.csi.aws.com/cluster-name", "Value": "rag-platform-cluster"},
    ]
    if attempt:
        tags.extend([
            {"Key": "qdrant-volume-origin", "Value": "fresh"},
            {"Key": "recovery-attempt", "Value": attempt},
        ])
    return {"VolumeId": f"vol-{ordinal}", "State": "available", "Size": 50,
            "AvailabilityZone": "us-east-1b", "Attachments": [], "Tags": tags}


class FakeRuntime:
    def __init__(self, kind="retained"):
        rows = [] if kind == "fresh" else [volume(i) for i in range(3)]
        self.inventory_result = classify([{"Volumes": rows}], "rag-platform-cluster")
        self.operations = []
        self.saved = None
        self.ready = False
        self.fail_direct = False
        self.fail_api = False
        self.fail_restore = False

    def preflight(self):
        self.operations.append("preflight")

    def inventory(self):
        self.operations.append("inventory")
        return self.inventory_result

    def existing_ready(self, inventory):
        self.operations.append("existing-ready")
        return self.ready

    def pending_attempt(self, inventory):
        return self.saved if self.saved and self.saved.get("status") != "verified" else None

    def select_attempt(self, inventory):
        self.operations.append("select-attempt")
        return "attempt-1"

    def attempt_exists(self, attempt_id):
        return self.saved is not None

    def previous_api_revision(self):
        return 1

    def snapshot(self, inventory, attempt_id):
        self.operations.append("snapshot-complete")
        return {0: "snap-0", 1: "snap-1", 2: "snap-2"}

    def fresh(self, inventory, attempt_id):
        self.operations.append("fresh-volume-set")
        return tuple(Volume(i, f"vol-{i}", "available", "us-east-1b", 50, {}, ())
                     for i in range(3))

    def bind(self, volumes):
        self.operations.append("pv-pvc")

    def probe(self, volumes):
        self.operations.append("version-gate")
        return "v1.7.3"

    def install(self, version):
        self.operations.append("qdrant-ready")

    def ensure_collection(self, branch):
        self.operations.append("collection-ready")

    def verify_qdrant(self, retained):
        self.operations.append("direct-verify")
        if self.fail_direct or self.fail_restore:
            raise RecoveryError("direct verification failed")
        return {"point_count": 7 if retained else 0}

    def save_attempt(self, attempt):
        self.operations.append("save-attempt")
        self.saved = attempt

    def load_attempt(self, attempt_id):
        self.operations.append("load-attempt")
        assert attempt_id == "attempt-1"
        return self.saved

    def verify_api(self, retained):
        self.operations.append("api-verify")
        if self.fail_api:
            raise RecoveryError("staging retrieval failed")

    def delete_snapshots(self, snapshots, attempt_id):
        self.operations.append("delete-snapshot")

    def restore_api_host(self, attempt):
        self.operations.append("restore-api-host")

    def stop_qdrant(self):
        self.operations.append("stop-qdrant")

    def replacement_volumes(self, snapshots, attempt_id):
        self.operations.append("replacement-volume-set")
        return {0: "vol-new-0", 1: "vol-new-1", 2: "vol-new-2"}

    def rebind_preserving_originals(self, replacements):
        self.operations.append("rebind-replacements")

    def promote_replacements(self, attempt):
        self.operations.append("promote-replacements")


def test_retained_apply_snapshots_before_any_mount_and_finalizes_after_api():
    runtime = FakeRuntime()
    applied = run_recovery(runtime, "apply")
    assert applied["branch"] == "retained"
    assert applied["attempt_id"] == "attempt-1"
    assert runtime.operations.index("snapshot-complete") < runtime.operations.index("pv-pvc")
    assert runtime.operations.index("version-gate") < runtime.operations.index("qdrant-ready")
    assert "delete-snapshot" not in runtime.operations
    run_recovery(runtime, "finalize", attempt_id="attempt-1")
    assert runtime.operations.index("direct-verify") < runtime.operations.index("api-verify")
    assert runtime.operations.index("api-verify") < runtime.operations.index("delete-snapshot")


def test_failed_direct_verification_never_switches_or_deletes():
    runtime = FakeRuntime()
    runtime.fail_direct = True
    with pytest.raises(RecoveryError):
        run_recovery(runtime, "apply")
    assert "delete-snapshot" not in runtime.operations
    assert "api-verify" not in runtime.operations


@pytest.mark.parametrize("phase", ["snapshot", "bind", "probe", "install", "ensure_collection", "verify_qdrant"])
def test_interrupted_phase_keeps_journal_and_never_deletes_snapshot(phase):
    runtime = FakeRuntime()
    original = getattr(runtime, phase)
    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RecoveryError(f"interrupted at {phase}")
    setattr(runtime, phase, interrupted)
    with pytest.raises(RecoveryError, match="interrupted"):
        run_recovery(runtime, "apply")
    assert runtime.saved is not None
    assert runtime.saved["attempt_id"] == "attempt-1"
    assert "delete-snapshot" not in runtime.operations
    assert "api-verify" not in runtime.operations


def test_failed_api_probe_restores_previous_host_and_retains_snapshots():
    runtime = FakeRuntime()
    run_recovery(runtime, "apply")
    runtime.fail_api = True
    with pytest.raises(RecoveryError):
        run_recovery(runtime, "finalize", attempt_id="attempt-1")
    assert "restore-api-host" in runtime.operations
    assert "delete-snapshot" not in runtime.operations


def test_fresh_branch_creates_persistent_set_without_snapshots():
    runtime = FakeRuntime("fresh")
    result = run_recovery(runtime, "apply")
    assert result["branch"] == "fresh"
    assert "fresh-volume-set" in runtime.operations
    assert "snapshot-complete" not in runtime.operations
    assert "collection-ready" in runtime.operations


def test_healthy_existing_cluster_is_reused_without_new_snapshot():
    runtime = FakeRuntime()
    runtime.ready = True
    result = run_recovery(runtime, "apply")
    assert result["branch"] == "reuse"
    assert "snapshot-complete" not in runtime.operations
    assert "pv-pvc" not in runtime.operations


def test_interrupted_after_direct_verification_keeps_attempt_for_finalize():
    runtime = FakeRuntime()
    run_recovery(runtime, "apply")
    runtime.ready = True
    resumed = run_recovery(runtime, "apply")
    assert resumed["attempt_id"] == "attempt-1"
    assert resumed["snapshots"] == {"0": "snap-0", "1": "snap-1", "2": "snap-2"}
    assert runtime.operations.count("snapshot-complete") == 1


def test_restored_volumes_are_promoted_only_after_api_verification():
    runtime = FakeRuntime()
    run_recovery(runtime, "apply")
    run_recovery(runtime, "restore", attempt_id="attempt-1")
    run_recovery(runtime, "finalize", attempt_id="attempt-1")
    assert runtime.operations.index("api-verify") < runtime.operations.index("promote-replacements")
    assert runtime.operations.index("promote-replacements") < runtime.operations.index("delete-snapshot")


def test_delivery_failure_rolls_back_api_without_deleting_snapshots():
    runtime = FakeRuntime()
    run_recovery(runtime, "apply")
    run_recovery(runtime, "rollback-api", attempt_id="attempt-1")
    assert "restore-api-host" in runtime.operations
    assert "delete-snapshot" not in runtime.operations


def test_partial_old_set_stops_before_storage_mutation():
    runtime = FakeRuntime()
    def bad_inventory():
        runtime.operations.append("inventory")
        return classify([{"Volumes": [volume(0), volume(1)]}], "rag-platform-cluster")
    runtime.inventory = bad_inventory
    with pytest.raises(UnsafeInventory):
        run_recovery(runtime, "apply")
    assert "snapshot-complete" not in runtime.operations
    assert "fresh-volume-set" not in runtime.operations


def test_restore_uses_three_snapshots_and_keeps_them_on_replacement_failure():
    runtime = FakeRuntime()
    run_recovery(runtime, "apply")
    runtime.fail_restore = True
    with pytest.raises(RecoveryError):
        run_recovery(runtime, "restore", attempt_id="attempt-1")
    assert runtime.operations.index("stop-qdrant") < runtime.operations.index("replacement-volume-set")
    assert runtime.operations.index("replacement-volume-set") < runtime.operations.index("rebind-replacements")
    assert "delete-snapshot" not in runtime.operations
