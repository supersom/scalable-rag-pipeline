"""Concrete, fail-closed AWS/Kubernetes adapter for staging Qdrant recovery."""

from __future__ import annotations

import json
import os
import subprocess
import time
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from uuid import uuid4

from .aws_storage import (RecoveryError, aws_json, delete_owned_snapshots,
                          ensure_fresh_volumes, replacement_from_snapshots, snapshot_retained)
from .inventory import CLUSTER_TAG, Inventory, Volume, classify, tags_of
from .kubernetes import (CHART_FOR_IMAGE, bind_volumes, install_qdrant, probe_storage_version,
                         pvc_name, pv_name, read_raft_via_probe, verify_bindings)
from .verify import verify_cluster, verify_staging_api


def _run(args, *, input=None, timeout=720, **_kwargs):
    try:
        return subprocess.run(args, input=input, text=True, capture_output=True, timeout=timeout,
                              check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryError(f"command unavailable or timed out: {args[0]}") from exc


def _json(args, *, missing_ok=False):
    result = _run(args)
    if result.returncode:
        if missing_ok and "NotFound" in result.stderr:
            return None
        raise RecoveryError(f"cannot inspect {args[0]} resource")
    try:
        return json.loads(result.stdout)
    except ValueError as exc:
        raise RecoveryError("resource query returned invalid JSON") from exc


@contextmanager
def _port_forward(namespace, resource, local, remote):
    process = subprocess.Popen(
        ["kubectl", "port-forward", "--namespace", namespace, resource, f"{local}:{remote}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RecoveryError("port-forward exited before verification")
            try:
                try:
                    urlopen(f"http://127.0.0.1:{local}/", timeout=1).close()
                except HTTPError:
                    pass  # any HTTP status, e.g. the API's 404 at "/", proves the tunnel is up
                if process.poll() is not None:
                    raise RecoveryError("port-forward exited before verification")
                break
            except RecoveryError:
                raise
            except Exception:
                time.sleep(.25)
        else:
            raise RecoveryError("port-forward did not become ready")
        yield f"http://127.0.0.1:{local}"
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


class ClusterRuntime:
    def __init__(self, *, region="us-east-1", cluster="rag-platform-cluster",
                 namespace="staging-evidence"):
        self.region, self.cluster, self.namespace = region, cluster, namespace
        self.aws = lambda args: aws_json(args, region=region)

    def preflight(self):
        if self.region != "us-east-1":
            raise RecoveryError("recovery is pinned to us-east-1")
        cluster = self.aws(["eks", "describe-cluster", "--name", self.cluster]).get("cluster", {})
        if cluster.get("status") != "ACTIVE":
            raise RecoveryError("EKS cluster is not active")
        account = self.aws(["sts", "get-caller-identity"]).get("Account")
        if not account or cluster.get("arn", "").split(":")[4:5] != [account]:
            raise RecoveryError("EKS cluster belongs to a different AWS account")
        if self.aws(["eks", "describe-addon", "--cluster-name", self.cluster,
                     "--addon-name", "aws-ebs-csi-driver"]).get("addon", {}).get("status") != "ACTIVE":
            raise RecoveryError("EBS CSI add-on is not active")
        nodes = _json(["kubectl", "get", "nodes", "-o", "json"])
        if not any(n.get("metadata", {}).get("labels", {}).get("topology.kubernetes.io/zone") == "us-east-1b"
                   and any(c.get("type") == "Ready" and c.get("status") == "True"
                           for c in n.get("status", {}).get("conditions", []))
                   for n in nodes.get("items", [])):
            self._require_karpenter_capacity_in_zone()
        self._node_instance_ids = {n.get("spec", {}).get("providerID", "").rsplit("/", 1)[-1]
                                   for n in nodes.get("items", [])}
        self._check_all_bindings()

    def _require_karpenter_capacity_in_zone(self):
        """No node in us-east-1b yet: accept only if Karpenter can launch one there."""
        pools = _json(["kubectl", "get", "nodepools.karpenter.sh", "-o", "json"]).get("items", [])
        for pool in pools:
            template = pool.get("spec", {}).get("template", {}).get("spec", {})
            if template.get("taints"):
                continue
            zone_ok = True
            for req in template.get("requirements", []):
                if req.get("key") != "topology.kubernetes.io/zone":
                    continue
                values = req.get("values", [])
                if req.get("operator") == "In" and "us-east-1b" not in values:
                    zone_ok = False
                if req.get("operator") == "NotIn" and "us-east-1b" in values:
                    zone_ok = False
            class_name = template.get("nodeClassRef", {}).get("name")
            if not zone_ok or not class_name:
                continue
            node_class = _json(["kubectl", "get", "ec2nodeclasses.karpenter.k8s.aws", class_name,
                                "-o", "json"], missing_ok=True)
            for term in (node_class or {}).get("spec", {}).get("subnetSelectorTerms", []):
                tags = term.get("tags")
                if not tags or set(term) != {"tags"}:
                    continue
                filters = [f"Name=tag:{k},Values={v}" for k, v in sorted(tags.items())]
                filters.append("Name=availability-zone,Values=us-east-1b")
                subnets = self.aws(["ec2", "describe-subnets", "--filters", *filters]).get("Subnets", [])
                if any(sn.get("State") == "available" and sn.get("AvailableIpAddressCount", 0) >= 20
                       for sn in subnets):
                    return
        raise RecoveryError("no Ready node in us-east-1b and Karpenter cannot provision one there")

    def _check_all_bindings(self):
        pvs = _json(["kubectl", "get", "pv", "-o", "json"]).get("items", [])
        for pv in pvs:
            name = pv.get("metadata", {}).get("name", "")
            claim = pv.get("spec", {}).get("claimRef", {})
            if (name.startswith("qdrant-ebs-qdrant-") and name not in {pv_name(i) for i in range(3)}
                    or claim.get("name", "").startswith("qdrant-storage-qdrant-")
                    and (name not in {pv_name(i) for i in range(3)}
                         or claim.get("namespace") != "default")):
                raise RecoveryError("unexpected Qdrant persistent-volume binding")

    def _volume_pages(self, filters):
        pages, token = [], None
        while True:
            args = ["ec2", "describe-volumes", "--filters", *filters]
            if token:
                args += ["--next-token", token]
            page = self.aws(args)
            pages.append(page)
            token = page.get("NextToken")
            if not token:
                return pages

    def inventory(self):
        pages = self._volume_pages(["Name=tag-key,Values=kubernetes.io/created-for/pvc/name"])
        restored_pages = self._volume_pages([
            f"Name=tag:{CLUSTER_TAG},Values={self.cluster}",
            "Name=tag:qdrant-volume-origin,Values=restored",
        ])
        restored = [row for page in restored_pages for row in page.get("Volumes", [])]
        if restored:
            active = [row for row in restored if tags_of(row).get("qdrant-active") == "true"]
            if len(active) != 3 or len(restored) != 3:
                raise RecoveryError("unfinalized or ambiguous restored Qdrant volume set")
            volumes = {}
            attempts = set()
            for row in active:
                tags = tags_of(row)
                attempts.add(tags.get("recovery-attempt"))
                try:
                    ordinal = int(tags["qdrant-recovery-ordinal"])
                except (KeyError, ValueError) as exc:
                    raise RecoveryError("restored Qdrant ordinal is malformed") from exc
                if (ordinal not in {0, 1, 2} or ordinal in volumes
                        or row.get("AvailabilityZone") != "us-east-1b" or row.get("Size") != 50
                        or row.get("State") not in {"available", "in-use"}):
                    raise RecoveryError("restored Qdrant volume set is unsafe")
                attachments = tuple(a.get("InstanceId") for a in row.get("Attachments", []))
                if any(instance not in getattr(self, "_node_instance_ids", set()) for instance in attachments):
                    raise RecoveryError("restored Qdrant volume attached outside this EKS cluster")
                pv, pvc = self._resource("pv", pv_name(ordinal)), self._resource("pvc", pvc_name(ordinal))
                if pv or pvc:
                    if (not pv or not pvc or pv.get("spec", {}).get("csi", {}).get("volumeHandle") != row.get("VolumeId")
                            or pvc.get("spec", {}).get("volumeName") != pv_name(ordinal)):
                        raise RecoveryError("restored volume lacks exact Qdrant binding")
                elif attachments or row.get("State") != "available":
                    raise RecoveryError("unbound restored volume is still attached")
                volumes[ordinal] = Volume(ordinal, row["VolumeId"], row["State"], row["AvailabilityZone"],
                                          row["Size"], tags, attachments)
            if set(volumes) != {0, 1, 2} or len(attempts) != 1 or None in attempts:
                raise RecoveryError("restored Qdrant volume set has mixed ownership")
            return Inventory("retained", tuple(volumes[i] for i in range(3)))
        # An attached disk is permitted only when its exact PV and PVC already
        # point to it; a foreign attachment never becomes a recovery source.
        allowed = set()
        for page in pages:
            for row in page.get("Volumes", []):
                if row.get("Attachments"):
                    tags = tags_of(row)
                    if tags.get(CLUSTER_TAG) != self.cluster:
                        continue
                    name = tags.get("kubernetes.io/created-for/pvc/name", "")
                    if name not in {pvc_name(i) for i in range(3)}:
                        continue
                    ordinal = int(name.rsplit("-", 1)[-1])
                    pv = self._resource("pv", pv_name(ordinal))
                    pvc = self._resource("pvc", pvc_name(ordinal))
                    if (not pv or not pvc or pv.get("spec", {}).get("csi", {}).get("volumeHandle") != row.get("VolumeId")
                            or pvc.get("spec", {}).get("volumeName") != pv_name(ordinal)):
                        raise RecoveryError("attached volume lacks exact Qdrant binding")
                    if any(a.get("InstanceId") not in self._node_instance_ids for a in row["Attachments"]):
                        raise RecoveryError("Qdrant volume attached outside this EKS cluster")
                    allowed.update(a.get("InstanceId") for a in row["Attachments"])
        result = classify(pages, self.cluster, allowed_attachment_ids=frozenset(allowed))
        if result.volumes:
            verify_bindings(self._resource, result.volumes)
        elif any(self._resource("pv", pv_name(i)) or self._resource("pvc", pvc_name(i)) for i in range(3)):
            raise RecoveryError("Qdrant bindings exist without discoverable volumes")
        return result

    def _resource(self, kind, name):
        args = ["kubectl", "get", kind, name]
        if kind == "pvc":
            args += ["--namespace", "default"]
        return _json(args + ["-o", "json"], missing_ok=True)

    def existing_ready(self, inventory):
        if len(inventory.volumes) != 3:
            return False
        if any(v.state != "in-use" for v in inventory.volumes):
            return False
        sts = _json(
            ["kubectl", "get", "statefulset", "qdrant", "--namespace", "default", "-o", "json"], missing_ok=True)
        if not sts or sts.get("status", {}).get("readyReplicas") != 3:
            raise RecoveryError("attached Qdrant volumes are not in a healthy three-replica release")
        self.verify_qdrant(retained=inventory.kind == "retained")
        return True

    def _journal_name(self, attempt_id):
        return "qdrant-recovery-" + attempt_id

    def attempt_exists(self, attempt_id):
        return _json(["kubectl", "get", "configmap", self._journal_name(attempt_id),
                      "--namespace", self.namespace, "-o", "json"], missing_ok=True) is not None

    def previous_api_revision(self):
        result = _run(["helm", "history", "api-staging", "--namespace", self.namespace,
                       "--output", "json"])
        if result.returncode:
            return None
        try:
            rows = json.loads(result.stdout)
            return max(row["revision"] for row in rows if row.get("status") == "deployed")
        except (ValueError, KeyError) as exc:
            raise RecoveryError("cannot determine previous API revision") from exc

    def _journals(self):
        data = _json(["kubectl", "get", "configmap", "--namespace", self.namespace,
                      "-l", "app=staging-qdrant-recovery", "-o", "json"])
        result = []
        for item in data.get("items", []):
            try:
                result.append(json.loads(item["data"]["attempt.json"]))
            except (KeyError, ValueError) as exc:
                raise RecoveryError("malformed Qdrant recovery journal") from exc
        return result

    def pending_attempt(self, inventory):
        journals = [j for j in self._journals() if j.get("status") != "verified"]
        if len(journals) > 1:
            raise RecoveryError("multiple unfinished Qdrant recovery attempts")
        if not journals:
            return None
        attempt = journals[0]
        known = attempt.get("volumes", {})
        observed = {str(v.ordinal): v.volume_id for v in inventory.volumes}
        if any(observed.get(ordinal) != volume_id for ordinal, volume_id in known.items()):
            raise RecoveryError("unfinished recovery journal differs from mounted volumes")
        return attempt

    def select_attempt(self, inventory):
        attempt = self.pending_attempt(inventory)
        if attempt:
            if attempt.get("branch") not in ({"retained"} if inventory.kind == "retained" else {"fresh"}):
                raise RecoveryError("unfinished recovery branch does not match inventory")
            known = attempt.get("volumes", {})
            if any(known.get(str(v.ordinal)) != v.volume_id for v in inventory.volumes):
                raise RecoveryError("recovery journal volume IDs changed")
            return attempt["attempt_id"]
        if inventory.attempt_id:
            return inventory.attempt_id
        return str(uuid4())

    def save_attempt(self, attempt):
        attempt = dict(attempt)
        attempt.setdefault("status", "pending")
        manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
            "name": self._journal_name(attempt["attempt_id"]), "namespace": self.namespace,
            "labels": {"app": "staging-qdrant-recovery"}},
            "data": {"attempt.json": json.dumps(attempt, sort_keys=True)}}
        result = _run(["kubectl", "apply", "-f", "-"], input=json.dumps(manifest))
        if result.returncode:
            raise RecoveryError("cannot persist Qdrant recovery attempt")

    def load_attempt(self, attempt_id):
        if not attempt_id or not all(c.isalnum() or c == "-" for c in attempt_id):
            raise RecoveryError("invalid recovery attempt ID")
        item = _json(["kubectl", "get", "configmap", self._journal_name(attempt_id),
                      "--namespace", self.namespace, "-o", "json"])
        try:
            attempt = json.loads(item["data"]["attempt.json"])
        except (KeyError, ValueError) as exc:
            raise RecoveryError("recovery journal is invalid") from exc
        if attempt.get("attempt_id") != attempt_id:
            raise RecoveryError("recovery journal attempt mismatch")
        return attempt

    def snapshot(self, inventory, attempt_id):
        return snapshot_retained(self.aws, inventory, attempt_id)

    def fresh(self, inventory, attempt_id):
        return ensure_fresh_volumes(self.aws, inventory, attempt_id, cluster=self.cluster)

    def bind(self, volumes):
        pv_data = _json(["kubectl", "get", "pv", "-o", "json"])
        selected = {v.volume_id for v in volumes}
        for pv in pv_data.get("items", []):
            if pv.get("spec", {}).get("csi", {}).get("volumeHandle") in selected and pv.get("metadata", {}).get("name") not in {pv_name(v.ordinal) for v in volumes}:
                raise RecoveryError("selected EBS volume already has a different PV")
        def apply(resource):
            if _run(["kubectl", "apply", "-f", "-"], input=json.dumps(resource)).returncode:
                raise RecoveryError("Qdrant static binding apply failed")
        bind_volumes(self._resource, apply, tuple(volumes))

    def probe(self, volumes):
        return probe_storage_version(lambda ordinal, volume: read_raft_via_probe(_run, volume), tuple(volumes))

    def install(self, version):
        install_qdrant(_run, version, CHART_FOR_IMAGE[version])
        if _run(["kubectl", "rollout", "status", "statefulset/qdrant", "--namespace", "default",
                 "--timeout=10m"], timeout=660).returncode:
            raise RecoveryError("Qdrant StatefulSet did not become ready")

    def ensure_collection(self, branch):
        if branch != "fresh":
            return
        with _port_forward("default", "service/qdrant", 16334, 6333) as base:
            try:
                _request(base, "GET", "/collections/rag_collection")
                return
            except RecoveryError:
                pass
            _request(base, "PUT", "/collections/rag_collection", {
                "vectors": {"size": 1024, "distance": "Cosine"}, "shard_number": 3,
                "replication_factor": 1,
            })

    def verify_qdrant(self, retained):
        with _port_forward("default", "service/qdrant", 16334, 6333) as base:
            report = verify_cluster(lambda method, path, payload=None: _request(base, method, path, payload),
                                    retained=retained)
        return {"point_count": report.point_count}

    def verify_api(self, retained):
        from scripts import smoke_cloud_env
        smoke_cloud_env.NAMESPACE = self.namespace
        try:
            token = smoke_cloud_env._read_api_runtime()["token"]
        except Exception as exc:
            raise RecoveryError("could not mint staging API verification token") from exc
        with _port_forward(self.namespace, "deployment/api-staging", 18081, 8000) as base:
            def post(payload, headers):
                response = _request_raw(base, "POST", "/api/v1/chat/stream", payload, headers)
                try:
                    events = [json.loads(line) for line in response["body"].splitlines() if line]
                except ValueError as exc:
                    raise RecoveryError("staging API returned malformed stream") from exc
                return {"status": response["status"], "headers": response["headers"], "events": events}
            def evidence(run_id):
                try:
                    summary = smoke_cloud_env._read_archived_evidence(run_id)
                except Exception as exc:
                    raise RecoveryError("correlated staging evidence is unavailable") from exc
                return {"safe_run": {"run_id": run_id, "retrieval": [{
                    "collection": r.get("collection"), "vector_status": r.get("vector_status"),
                    "items": [True] if r.get("item_count", 0) > 0 else [],
                } for r in summary.get("retrieval", [])]}}
            bypass = os.getenv("LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED", "false").lower() == "true"
            verify_staging_api(post, evidence, token=token, retained=retained,
                               cache_bypass_enabled=bypass)

    def delete_snapshots(self, snapshots, attempt_id):
        delete_owned_snapshots(self.aws, snapshots, attempt_id)

    def restore_api_host(self, attempt):
        previous = attempt.get("previous_api_revision")
        if previous is None:
            result = _run(["helm", "uninstall", "api-staging", "--namespace", self.namespace])
            if result.returncode:
                raise RecoveryError("new API release could not be removed; snapshots retained")
            return
        if not isinstance(previous, int) or previous < 1:
            raise RecoveryError("previous API revision invalid; snapshots retained")
        result = _run(["helm", "rollback", "api-staging", str(previous), "--namespace",
                       self.namespace, "--wait", "--timeout", "5m"], timeout=360)
        if result.returncode:
            raise RecoveryError("API rollback failed; snapshots retained")

    def stop_qdrant(self):
        sts = _json(["kubectl", "get", "statefulset", "qdrant", "--namespace", "default", "-o", "json"])
        if sts.get("metadata", {}).get("name") != "qdrant":
            raise RecoveryError("unexpected Qdrant StatefulSet")
        if _run(["kubectl", "scale", "statefulset/qdrant", "--namespace", "default", "--replicas=0"]).returncode:
            raise RecoveryError("could not stop Qdrant")
        if _run(["kubectl", "rollout", "status", "statefulset/qdrant", "--namespace", "default",
                 "--timeout=10m"], timeout=660).returncode:
            raise RecoveryError("Qdrant did not stop")

    def replacement_volumes(self, snapshots, attempt_id):
        return replacement_from_snapshots(self.aws, snapshots, attempt_id, cluster=self.cluster)

    def promote_replacements(self, attempt):
        replacements = attempt.get("replacements", {})
        if set(replacements) != {"0", "1", "2"}:
            raise RecoveryError("replacement promotion requires three volumes")
        for ordinal in range(3):
            volume_id = replacements[str(ordinal)]
            pv = self._resource("pv", pv_name(ordinal))
            if not pv or pv.get("spec", {}).get("csi", {}).get("volumeHandle") != volume_id:
                raise RecoveryError("replacement promotion binding mismatch")
            rows = self.aws(["ec2", "describe-volumes", "--volume-ids", volume_id]).get("Volumes", [])
            if len(rows) != 1:
                raise RecoveryError("replacement promotion volume missing")
            tags = tags_of(rows[0])
            if (tags.get("recovery-attempt") != attempt["attempt_id"]
                    or tags.get("qdrant-recovery-ordinal") != str(ordinal)
                    or tags.get(CLUSTER_TAG) != self.cluster):
                raise RecoveryError("replacement promotion ownership mismatch")
        for volume_id in replacements.values():
            self.aws(["ec2", "create-tags", "--resources", volume_id, "--tags",
                      "Key=qdrant-active,Value=true"])

    def rebind_preserving_originals(self, replacements):
        if set(replacements) != {0, 1, 2}:
            raise RecoveryError("replacement set is incomplete")
        old_handles = []
        for ordinal, volume_id in sorted(replacements.items()):
            pv = self._resource("pv", pv_name(ordinal))
            pvc = self._resource("pvc", pvc_name(ordinal))
            if pv and pv.get("spec", {}).get("csi", {}).get("volumeHandle") == volume_id:
                if pvc and pvc.get("spec", {}).get("volumeName") != pv_name(ordinal):
                    raise RecoveryError("partially rebound PVC conflicts with replacement")
                continue
            if not pv or not pvc:
                raise RecoveryError("original Qdrant binding is incomplete")
            handle = pv.get("spec", {}).get("csi", {}).get("volumeHandle")
            if (pv.get("spec", {}).get("persistentVolumeReclaimPolicy") != "Retain"
                    or pv.get("spec", {}).get("csi", {}).get("driver") != "ebs.csi.aws.com"
                    or pvc.get("spec", {}).get("volumeName") != pv_name(ordinal)
                    or not handle or handle == volume_id):
                raise RecoveryError("cannot safely replace original Qdrant binding")
            old_handles.append(handle)
        deadline = time.monotonic() + 600
        while old_handles:
            rows = self.aws(["ec2", "describe-volumes", "--volume-ids", *old_handles]).get("Volumes", [])
            if len(rows) != len(old_handles):
                raise RecoveryError("original EBS volume disappeared before rebind")
            if all(row.get("State") == "available" and not row.get("Attachments") for row in rows):
                break
            if time.monotonic() >= deadline:
                raise RecoveryError("original EBS volumes did not detach; bindings preserved")
            time.sleep(5)
        for ordinal, volume_id in sorted(replacements.items()):
            current = self._resource("pv", pv_name(ordinal))
            if current and current.get("spec", {}).get("csi", {}).get("volumeHandle") == volume_id:
                continue
            for kind, name, ns in (("pvc", pvc_name(ordinal), ["--namespace", "default"]),
                                   ("pv", pv_name(ordinal), [])):
                item = _json(["kubectl", "get", kind, name, *ns, "-o", "json"], missing_ok=True)
                if not item:
                    continue
                if kind == "pv" and (item.get("spec", {}).get("persistentVolumeReclaimPolicy") != "Retain"
                                     or item.get("spec", {}).get("csi", {}).get("driver") != "ebs.csi.aws.com"):
                    raise RecoveryError("cannot safely replace non-retained PV")
                if _run(["kubectl", "delete", kind, name, *ns, "--wait=true"]).returncode:
                    raise RecoveryError("could not remove old Qdrant binding")
        volumes = []
        for ordinal, volume_id in sorted(replacements.items()):
            rows = self.aws(["ec2", "describe-volumes", "--volume-ids", volume_id]).get("Volumes", [])
            if len(rows) != 1 or rows[0].get("VolumeId") != volume_id:
                raise RecoveryError("replacement EBS volume is missing")
            row = rows[0]
            volumes.append(Volume(ordinal, volume_id, row["State"], row["AvailabilityZone"],
                                  row["Size"], tags_of(row), tuple(a.get("InstanceId") for a in row.get("Attachments", []))))
        self.bind(tuple(volumes))


def _request_raw(base, method, path, payload=None, headers=None):
    data = json.dumps(payload).encode() if payload is not None else None
    request = Request(base + path, data=data, method=method,
                      headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urlopen(request, timeout=90) as response:
            return {"status": response.status, "headers": {k.lower(): v for k, v in response.headers.items()},
                    "body": response.read().decode()}
    except Exception as exc:
        raise RecoveryError("Qdrant or staging API request failed") from exc


def _request(base, method, path, payload=None):
    raw = _request_raw(base, method, path, payload)
    try:
        return json.loads(raw["body"])
    except ValueError as exc:
        raise RecoveryError("Qdrant returned invalid JSON") from exc
