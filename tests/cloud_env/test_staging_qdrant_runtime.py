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


def _capacity_runtime(monkeypatch, *, requirements, subnets, taints=None):
    pool = {"spec": {"template": {"spec": {
        "requirements": requirements, "nodeClassRef": {"name": "default"},
        **({"taints": taints} if taints else {})}}}}
    node_class = {"spec": {"subnetSelectorTerms": [{"tags": {"karpenter.sh/discovery": "c"}}]}}
    def fake_json(args, **kwargs):
        return {"items": [pool]} if "nodepools.karpenter.sh" in args else node_class
    monkeypatch.setattr(module, "_json", fake_json)
    adapter = module.ClusterRuntime()
    adapter.aws = lambda args: {"Subnets": subnets}
    return adapter


GOOD_SUBNET = [{"State": "available", "AvailableIpAddressCount": 200}]


def test_karpenter_capacity_accepted_when_zone_unrestricted_and_subnet_usable(monkeypatch):
    _capacity_runtime(monkeypatch, requirements=[], subnets=GOOD_SUBNET)._require_karpenter_capacity_in_zone()


@pytest.mark.parametrize("requirements,subnets,taints", [
    ([{"key": "topology.kubernetes.io/zone", "operator": "In", "values": ["us-east-1a"]}], GOOD_SUBNET, None),
    ([{"key": "topology.kubernetes.io/zone", "operator": "NotIn", "values": ["us-east-1b"]}], GOOD_SUBNET, None),
    ([], [], None),
    ([], [{"State": "available", "AvailableIpAddressCount": 3}], None),
    ([], GOOD_SUBNET, [{"key": "nvidia.com/gpu", "effect": "NoSchedule"}]),
])
def test_karpenter_capacity_rejected_otherwise(monkeypatch, requirements, subnets, taints):
    adapter = _capacity_runtime(monkeypatch, requirements=requirements, subnets=subnets, taints=taints)
    with pytest.raises(RecoveryError, match="Karpenter cannot provision"):
        adapter._require_karpenter_capacity_in_zone()


def test_port_forward_ready_when_root_path_returns_http_error(monkeypatch):
    from urllib.error import HTTPError

    class Proc:
        def poll(self):
            return None
        def terminate(self):
            pass
        def wait(self, timeout=None):
            return 0

    def not_found(url, timeout=None):
        raise HTTPError(url, 404, "Not Found", {}, None)

    monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(module, "urlopen", not_found)
    with module._port_forward("ns", "deployment/api", 18999, 8000) as base:
        assert base == "http://127.0.0.1:18999"
