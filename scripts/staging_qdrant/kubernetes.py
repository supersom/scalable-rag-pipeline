"""Static EBS bindings and pinned Qdrant installation gates."""

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

from .aws_storage import RecoveryError
from .inventory import Volume


VALUES_FILE = Path(__file__).resolve().parents[2] / "deploy/helm/qdrant/values.staging-recovery.yaml"
CHART_FOR_IMAGE = {"v1.7.3": "0.7.5", "v1.19.1": "1.19.1"}


def pv_name(ordinal: int) -> str:
    return f"qdrant-ebs-qdrant-{ordinal}"


def pvc_name(ordinal: int) -> str:
    return f"qdrant-storage-qdrant-{ordinal}"


def render_pv(volume: Volume) -> dict:
    return {
        "apiVersion": "v1", "kind": "PersistentVolume",
        "metadata": {"name": pv_name(volume.ordinal)},
        "spec": {
            "capacity": {"storage": "50Gi"},
            "accessModes": ["ReadWriteOnce"],
            "persistentVolumeReclaimPolicy": "Retain",
            "storageClassName": "",
            "claimRef": {"namespace": "default", "name": pvc_name(volume.ordinal)},
            "csi": {"driver": "ebs.csi.aws.com", "volumeHandle": volume.volume_id},
            "nodeAffinity": {"required": {"nodeSelectorTerms": [{"matchExpressions": [{
                "key": "topology.kubernetes.io/zone", "operator": "In", "values": ["us-east-1b"],
            }]}]}},
        },
    }


def render_pvc(volume: Volume) -> dict:
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": pvc_name(volume.ordinal), "namespace": "default"},
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": "50Gi"}},
            "storageClassName": "",
            "volumeName": pv_name(volume.ordinal),
        },
    }


def verify_bindings(get_resource: Callable[[str, str], dict | None], volumes: tuple[Volume, ...]) -> None:
    """Reject a conflicting existing named PV/PVC before any apply."""
    for volume in volumes:
        pv = get_resource("pv", pv_name(volume.ordinal))
        pvc = get_resource("pvc", pvc_name(volume.ordinal))
        expected_pv, expected_pvc = render_pv(volume), render_pvc(volume)
        if pv:
            spec = pv.get("spec", {})
            wanted = expected_pv["spec"]
            if (spec.get("csi") != wanted["csi"] or spec.get("claimRef", {}).get("name") != wanted["claimRef"]["name"]
                    or spec.get("claimRef", {}).get("namespace") != "default"
                    or spec.get("persistentVolumeReclaimPolicy") != "Retain"
                    or spec.get("storageClassName", "") != ""):
                raise RecoveryError("existing Qdrant PV binding conflicts with selected volume")
        if pvc:
            spec = pvc.get("spec", {})
            if spec.get("volumeName") != expected_pvc["spec"]["volumeName"] or spec.get("storageClassName", "") != "":
                raise RecoveryError("existing Qdrant PVC binding conflicts with selected volume")


def bind_volumes(
    get_resource: Callable[[str, str], dict | None],
    apply_resource: Callable[[dict], None],
    volumes: tuple[Volume, ...],
) -> None:
    if {v.ordinal for v in volumes} != {0, 1, 2}:
        raise RecoveryError("three ordinal volumes are required before binding")
    verify_bindings(get_resource, volumes)
    for volume in volumes:
        if get_resource("pv", pv_name(volume.ordinal)) is None:
            apply_resource(render_pv(volume))
    for volume in volumes:
        if get_resource("pvc", pvc_name(volume.ordinal)) is None:
            apply_resource(render_pvc(volume))


def _version_from_raft(raw: str) -> str:
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        raise RecoveryError("Qdrant storage version cannot be read") from exc
    metadata = data.get("peer_metadata_by_id") if isinstance(data, dict) else None
    if not isinstance(metadata, dict) or not metadata:
        raise RecoveryError("Qdrant storage version metadata is absent")
    versions = set()
    for peer in metadata.values():
        version = peer.get("version") if isinstance(peer, dict) else None
        if isinstance(version, str):
            # Qdrant 1.19.1 writes "1.19.1"; verified against the retained staging disks.
            match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
            if match is None:
                raise RecoveryError("Qdrant storage version metadata is invalid")
            versions.add("v" + ".".join(match.groups()))
            continue
        if not isinstance(version, dict):
            raise RecoveryError("Qdrant storage version metadata is incomplete")
        parts = tuple(version.get(key) for key in ("major", "minor", "patch"))
        if any(not isinstance(part, int) or part < 0 for part in parts):
            raise RecoveryError("Qdrant storage version metadata is invalid")
        versions.add("v" + ".".join(str(part) for part in parts))
    if len(versions) != 1:
        raise RecoveryError("Qdrant peer versions disagree on disk")
    return versions.pop()


def probe_storage_version(
    read_raft: Callable[[int, Volume], str], volumes: tuple[Volume, ...],
) -> str:
    """Require a single known server version across all three read-only probes."""
    if {v.ordinal for v in volumes} != {0, 1, 2}:
        raise RecoveryError("storage version probe requires three disks")
    versions = {_version_from_raft(read_raft(v.ordinal, v)) for v in volumes}
    if len(versions) != 1:
        raise RecoveryError("Qdrant storage versions disagree")
    version = versions.pop()
    if version not in CHART_FOR_IMAGE:
        raise RecoveryError("Qdrant storage version has no reviewed image/chart pair")
    return version


def _render_is_safe(rendered: str, image_version: str) -> bool:
    statefulsets = [doc for doc in re.split(r"(?m)^---\s*$", rendered)
                    if re.search(r"(?m)^kind: StatefulSet\s*$", doc)]
    if len(statefulsets) != 1:
        return False
    doc = statefulsets[0]
    required = [
        r"(?m)^  name: qdrant\s*$",
        r"(?m)^  replicas: 3\s*$",
        rf"(?m)^\s+image: [\"']?docker\.io/qdrant/qdrant:{re.escape(image_version)}[\"']?\s*$",
        r"(?m)^\s+name: qdrant-storage\s*$",
        r"(?m)^\s+mountPath: /qdrant/storage\s*$",
        r"(?m)^  volumeClaimTemplates:\s*$",
    ]
    return all(re.search(pattern, doc) for pattern in required)


def install_qdrant(
    run: Callable[[list[str]], subprocess.CompletedProcess],
    image_version: str, chart_version: str,
) -> None:
    if image_version == "latest" or chart_version == "latest" or image_version not in CHART_FOR_IMAGE:
        raise RecoveryError("Qdrant image version is not a reviewed pin")
    if CHART_FOR_IMAGE[image_version] != chart_version:
        raise RecoveryError("Qdrant chart is not paired with the pinned image")
    common = [
        "qdrant", "qdrant/qdrant", "--namespace", "default", "--version", chart_version,
        "-f", str(VALUES_FILE), "--set", f"image.tag={image_version}",
    ]
    preview = run(["helm", "template", *common])
    if preview.returncode or not _render_is_safe(preview.stdout, image_version):
        raise RecoveryError("Qdrant Helm render violates storage or replica contract")
    result = run(["helm", "upgrade", "--install", *common, "--wait", "--timeout", "10m"])
    if result.returncode:
        raise RecoveryError("Qdrant Helm deployment failed; retained disks and snapshots preserved")


def read_raft_via_probe(run: Callable[..., subprocess.CompletedProcess], volume: Volume) -> str:
    """Mount one bound claim read-only in a pinned diagnostic pod, then remove it."""
    name = f"qdrant-version-probe-{volume.ordinal}"
    manifest = {
        "apiVersion": "v1", "kind": "Pod",
        "metadata": {"name": name, "namespace": "default", "labels": {"app": "qdrant-version-probe"}},
        "spec": {
            "restartPolicy": "Never",
            "nodeSelector": {"topology.kubernetes.io/zone": "us-east-1b"},
            "containers": [{
                "name": "reader", "image": "busybox:1.36.1", "command": ["sleep", "3600"],
                "securityContext": {"readOnlyRootFilesystem": True, "allowPrivilegeEscalation": False},
                "volumeMounts": [{"name": "storage", "mountPath": "/qdrant/storage", "readOnly": True}],
            }],
            "volumes": [{"name": "storage", "persistentVolumeClaim": {
                "claimName": pvc_name(volume.ordinal), "readOnly": True,
            }}],
        },
    }
    apply = run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest), text=True, capture_output=True)
    if apply.returncode:
        raise RecoveryError("read-only Qdrant version probe pod could not be created")
    try:
        ready = run(["kubectl", "wait", "--namespace", "default", "--for=condition=Ready",
                     f"pod/{name}", "--timeout=5m"], text=True, capture_output=True)
        if ready.returncode:
            raise RecoveryError("read-only Qdrant version probe pod did not become ready")
        data = run(["kubectl", "exec", "--namespace", "default", name, "--",
                    "cat", "/qdrant/storage/raft_state.json"], text=True, capture_output=True)
        if data.returncode:
            raise RecoveryError("Qdrant raft state could not be read without writes")
        return data.stdout
    finally:
        run(["kubectl", "delete", "pod", name, "--namespace", "default", "--ignore-not-found=true",
             "--wait=true"], text=True, capture_output=True)
