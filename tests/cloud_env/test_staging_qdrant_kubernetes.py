"""Static Qdrant bindings and pinned-version gates."""

import json
from subprocess import CompletedProcess

import pytest

from scripts.staging_qdrant import kubernetes

from scripts.staging_qdrant.aws_storage import RecoveryError
from scripts.staging_qdrant.inventory import Volume
from scripts.staging_qdrant.kubernetes import (
    install_qdrant, probe_storage_version, render_pv, render_pvc, verify_bindings,
)


def volume(ordinal):
    return Volume(ordinal, f"vol-{ordinal}", "available", "us-east-1b", 50, {}, ())


def test_pv_is_bound_to_exact_ordinal_and_retained():
    pv = render_pv(volume(1))
    assert pv["spec"]["csi"] == {"driver": "ebs.csi.aws.com", "volumeHandle": "vol-1"}
    assert pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert pv["spec"]["claimRef"]["namespace"] == "default"
    assert pv["spec"]["claimRef"]["name"] == "qdrant-storage-qdrant-1"
    assert pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["us-east-1b"]


def test_pvc_selects_only_its_static_pv():
    pvc = render_pvc(volume(2))
    assert pvc["metadata"] == {"name": "qdrant-storage-qdrant-2", "namespace": "default"}
    assert pvc["spec"]["volumeName"] == "qdrant-ebs-qdrant-2"
    assert pvc["spec"]["storageClassName"] == ""
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]


def test_existing_binding_to_different_volume_fails_before_apply():
    resources = {
        ("pv", "qdrant-ebs-qdrant-0"): {
            "spec": {"csi": {"driver": "ebs.csi.aws.com", "volumeHandle": "vol-other"},
                     "claimRef": {"namespace": "default", "name": "qdrant-storage-qdrant-0"},
                     "persistentVolumeReclaimPolicy": "Retain"}},
    }
    with pytest.raises(RecoveryError, match="binding"):
        verify_bindings(lambda kind, name: resources.get((kind, name)), (volume(0),))


def test_peer_version_probe_requires_agreement_and_known_versions():
    def peer(version):
        return json.dumps({"peer_metadata_by_id": {"1": {"version": {
            "major": version[0], "minor": version[1], "patch": version[2],
        }}}})

    assert probe_storage_version(lambda ordinal, _: peer((1, 7, 3)), tuple(volume(i) for i in range(3))) == "v1.7.3"
    with pytest.raises(RecoveryError, match="version"):
        probe_storage_version(lambda ordinal, _: peer((1, 7, 3)) if ordinal < 2 else peer((1, 8, 0)),
                              tuple(volume(i) for i in range(3)))
    with pytest.raises(RecoveryError, match="version"):
        probe_storage_version(lambda ordinal, _: "{}", tuple(volume(i) for i in range(3)))


def test_chart_image_pair_must_be_pinned_and_compatible():
    calls = []

    def run(args):
        calls.append(args)
        return CompletedProcess(args, 0, stdout="", stderr="")

    with pytest.raises(RecoveryError, match="chart"):
        install_qdrant(run, image_version="v1.7.3", chart_version="1.19.1")
    with pytest.raises(RecoveryError, match="version"):
        install_qdrant(run, image_version="latest", chart_version="0.7.5")
    assert calls == []


def test_helm_render_must_show_three_stable_pods_and_storage_mount():
    calls = []
    invalid = "kind: StatefulSet\nmetadata:\n  name: qdrant\nspec:\n  replicas: 1\n"

    def run(args):
        calls.append(args)
        return CompletedProcess(args, 0, stdout=invalid, stderr="")

    with pytest.raises(RecoveryError, match="render"):
        install_qdrant(run, image_version="v1.7.3", chart_version="0.7.5")
    assert any(args[:2] == ["helm", "template"] for args in calls)
    assert not any(args[:2] == ["helm", "upgrade"] for args in calls)


def test_string_peer_versions_from_real_raft_state_are_accepted():
    raw = json.dumps({"peer_metadata_by_id": {str(i): {"version": "1.19.1"} for i in range(3)}})
    volumes = tuple(Volume(i, f"vol-{i}", "available", "us-east-1b", 50, {}, ()) for i in range(3))
    assert probe_storage_version(lambda ordinal, volume: raw, volumes) == "v1.19.1"


@pytest.mark.parametrize("version", ["latest", "1.19", ""])
def test_malformed_string_peer_version_fails_closed(version):
    raw = json.dumps({"peer_metadata_by_id": {"1": {"version": version}}})
    with pytest.raises(RecoveryError):
        kubernetes._version_from_raft(raw)
