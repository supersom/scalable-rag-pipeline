"""Concrete adapter refuses ambiguous cloud state without mutation."""

import json
from types import SimpleNamespace

import pytest

from scripts.staging_qdrant.aws_storage import RecoveryError
from scripts.staging_qdrant.inventory import Inventory, Volume
from scripts.staging_qdrant import runtime as module


def test_journal_round_trip_uses_namespaced_configmap(monkeypatch):
    calls = []
    stored = {}
    def fake_run(args, *, input=None, **kwargs):
        calls.append(args)
        stored.update(json.loads(input))
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(module, "_run", fake_run)
    monkeypatch.setattr(module, "_json", lambda args, **kwargs: stored)
    adapter = module.ClusterRuntime()
    attempt = {"attempt_id": "abc-123", "branch": "retained", "volumes": {"0": "vol-1"}}
    adapter.save_attempt(attempt)
    assert stored["metadata"]["namespace"] == "staging-evidence"
    assert adapter.load_attempt("abc-123")["volumes"] == {"0": "vol-1"}
    assert calls[0][:3] == ["kubectl", "apply", "-f"]


def test_pending_journal_rejects_changed_volume_id(monkeypatch):
    attempt = {"attempt_id": "abc", "status": "pending", "volumes": {"0": "vol-original"}}
    monkeypatch.setattr(module, "_json", lambda args, **kwargs: {
        "items": [{"data": {"attempt.json": json.dumps(attempt)}}]})
    adapter = module.ClusterRuntime()
    observed = Inventory("retained", (Volume(0, "vol-other", "available", "us-east-1b", 50, {}, ()),))
    with pytest.raises(RecoveryError, match="journal differs"):
        adapter.pending_attempt(observed)


def test_unfinalized_replacement_blocks_normal_apply(monkeypatch):
    def fake_pages(self, filters):
        if any("qdrant-volume-origin" in value for value in filters):
            return [{"Volumes": [{"VolumeId": "vol-new", "Tags": [
                {"Key": "ebs.csi.aws.com/cluster-name", "Value": "rag-platform-cluster"},
                {"Key": "qdrant-volume-origin", "Value": "restored"},
            ]}]}]
        return [{"Volumes": []}]
    monkeypatch.setattr(module.ClusterRuntime, "_volume_pages", fake_pages)
    adapter = module.ClusterRuntime()
    with pytest.raises(RecoveryError, match="unfinalized"):
        adapter.inventory()


def test_complete_active_replacements_can_be_rebound_after_cluster_recreation(monkeypatch):
    def fake_pages(self, filters):
        if any("qdrant-volume-origin" in value for value in filters):
            return [{"Volumes": [{
                "VolumeId": f"vol-new-{i}", "State": "available", "Size": 50,
                "AvailabilityZone": "us-east-1b", "Attachments": [], "Tags": [
                    {"Key": "ebs.csi.aws.com/cluster-name", "Value": "rag-platform-cluster"},
                    {"Key": "qdrant-volume-origin", "Value": "restored"},
                    {"Key": "qdrant-recovery-ordinal", "Value": str(i)},
                    {"Key": "recovery-attempt", "Value": "abc"},
                    {"Key": "qdrant-active", "Value": "true"},
                ],
            } for i in range(3)]}]
        return [{"Volumes": []}]
    monkeypatch.setattr(module.ClusterRuntime, "_volume_pages", fake_pages)
    monkeypatch.setattr(module.ClusterRuntime, "_resource", lambda *args: None)
    inventory = module.ClusterRuntime().inventory()
    assert inventory.kind == "retained"
    assert [v.volume_id for v in inventory.volumes] == [f"vol-new-{i}" for i in range(3)]
