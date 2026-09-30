# Staging Qdrant Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every staging bring-up recover the three retained Qdrant EBS volumes, or provision a persistent three-replica replacement when none exists, without losing the last recoverable copy.

**Architecture:** A small Python recovery command separates inventory, AWS volume/snapshot operations, Kubernetes binding/deployment, and verification. The existing staging shell deploy invokes `check` before mutation and `apply` before switching its API host. Exact replica tags, a pinned Qdrant version, `Retain` PVs, and explicit verification are hard gates.

**Tech Stack:** Python 3.12 standard library, AWS CLI, kubectl, Helm, pytest, Qdrant REST API, EBS CSI.

**Spec:** `docs/evaluation_implementation/staging-qdrant-recovery-design.md`

## Global Constraints

- Retained ordinals are exactly `qdrant-storage-qdrant-{0,1,2}` in the `default` namespace of `rag-platform-cluster`; volume IDs are discovered by exact tags, not hard-coded.
- All three volumes must be in `us-east-1b`; PVs use `ebs.csi.aws.com`, `ReadWriteOnce`, 50 GiB, `Retain`, and `us-east-1b` node affinity.
- The collection is `rag_collection` with one unnamed 1024-dimensional Cosine vector; retained-data verification requires a nonzero point count.
- All Qdrant images/charts are pinned. Never boot an unknown or older image against retained disks; no automatic version migration or downgrade.
- A partial or ambiguous retained set fails closed. An incomplete fresh set may resume only by its script-owned attempt ID.
- Snapshot all three retained volumes and wait for completion before any pod mounts them. Delete only this attempt's snapshots after successful Qdrant **and staging API** verification. On a post-mount failure, retain originals and snapshots.
- Never delete an EBS volume. Preserve unrelated worktree changes, especially the already-modified `scripts/deploy_staging_evidence.sh`.

## File map

- `scripts/staging_qdrant/inventory.py`: exact-tag discovery and fail-closed state classification.
- `scripts/staging_qdrant/aws_storage.py`: AWS CLI JSON boundary, snapshot/fresh-volume lifecycle and snapshot-derived replacements.
- `scripts/staging_qdrant/kubernetes.py`: static PV/PVC rendering, read-only version probe, pinned Helm installation and Kubernetes status.
- `scripts/staging_qdrant/verify.py`: direct Qdrant peer/collection checks and API retrieval check.
- `scripts/staging_qdrant_recovery.py`: `check`/`apply` orchestration, attempt ownership, final cleanup.
- `scripts/deploy_staging_evidence.sh`: call recovery, replace the staging API's Qdrant host, verify after rollout; preserve current dotenv and Terraform preflight edits.
- `deploy/helm/qdrant/values.staging-recovery.yaml`: three replicas, explicit storage settings and conservative resource requests; image/chart version supplied by the verified pin.
- `tests/cloud_env/test_staging_qdrant_*.py`: isolated contract tests with fake AWS/Kubernetes adapters; no test writes to AWS.
- `docs/evaluation_implementation/local-environment.md`: operator-facing staging recovery and failure commands.

## Review Focus

1. A volume tagged for another cluster or ordinal must never bind to an expected claim; Task 1 tests exact tag equality and duplicates.
2. An AWS paginated result must not hide a fourth matching volume; Task 1 tests pagination before classification.
3. An interrupted fresh create after one volume must not become a mixed old/fresh cluster; Task 2 tests same-attempt resumption and a foreign partial set.
4. A cached API answer must not masquerade as recovered retrieval; Task 4 tests a unique query with cache bypass and checks a retrieval span/result.
5. A healthy Qdrant REST endpoint with missing shards/zero retained points must not trigger snapshot deletion; Task 4 tests that failure and Task 5 tests cleanup order.

---

### Task 1: Exact inventory and read-only preflight

**Files:** Create `scripts/staging_qdrant/__init__.py`, `scripts/staging_qdrant/inventory.py`, `tests/cloud_env/test_staging_qdrant_inventory.py`.

**Interfaces:** Produce `Volume(ordinal: int, volume_id: str, state: str, zone: str, size_gib: int, tags: dict[str,str], attachments: tuple[str,...])`, `Inventory(kind: Literal['retained','fresh','resume_fresh'], volumes: tuple[Volume,...], attempt_id: str | None)`, and `classify(raw_pages: list[dict], cluster: str) -> Inventory`. `UnsafeInventory` is a `ValueError`; later tasks do not mutate after it is raised. Kubernetes binding and health are checked separately, not inferred from AWS inventory.

- [ ] **Step 1: Write tests first.** Include these cases in `tests/cloud_env/test_staging_qdrant_inventory.py`:

```python
def test_exact_three_old_volumes_map_by_ordinal_not_list_order():
    rows = [old_volume(2), old_volume(0), old_volume(1)]
    assert [v.ordinal for v in classify([{"Volumes": rows}], "rag-platform-cluster").volumes] == [0, 1, 2]

def test_partial_or_duplicate_old_set_fails_closed():
    with pytest.raises(UnsafeInventory):
        classify([{"Volumes": [old_volume(0), old_volume(1)]}], "rag-platform-cluster")
    with pytest.raises(UnsafeInventory):
        classify([{"Volumes": [old_volume(0), old_volume(0), old_volume(1), old_volume(2)]}], "rag-platform-cluster")

def test_second_page_duplicate_is_not_ignored():
    pages = [{"Volumes": [old_volume(0), old_volume(1), old_volume(2)], "NextToken": "next"},
             {"Volumes": [old_volume(2)]}]
    with pytest.raises(UnsafeInventory):
        classify(pages, "rag-platform-cluster")
```

`old_volume(i)` is a fixture returning AWS-shaped metadata with the exact `kubernetes.io/created-for/pvc/name`, namespace, cluster, 50-GiB and `us-east-1b` values. Add tests for wrong cluster, wrong AZ/size, foreign attachment, no volumes (`fresh`), and an existing binding whose PVC points at the wrong volume.

- [ ] **Step 2: Verify red.** Run `pytest tests/cloud_env/test_staging_qdrant_inventory.py -q`; expect import/function failures for `inventory.py`, not fixture errors.
- [ ] **Step 3: Implement the pure classifier.** Use one explicit ordinal regex and validate the entire candidate set before choosing a branch:

```python
PVC_NAME = re.compile(r"^qdrant-storage-qdrant-([012])$")

def tags_of(row: dict) -> dict[str, str]:
    return {tag["Key"]: tag["Value"] for tag in row.get("Tags", [])}

def ordinal_of(row: dict, cluster: str) -> int | None:
    tags = tags_of(row)
    match = PVC_NAME.fullmatch(tags.get("kubernetes.io/created-for/pvc/name", ""))
    if match is None:
        return None
    if (tags.get("kubernetes.io/created-for/pvc/namespace") != "default"
            or tags.get("ebs.csi.aws.com/cluster-name") != cluster):
        raise UnsafeInventory("Qdrant volume ownership mismatch")
    return int(match.group(1))
```

Reject duplicate ordinal, malformed matching tag, missing page, wrong size/zone, unexpected attachment, and mixed `recovery-attempt` values. Do not print raw AWS JSON in errors.

- [ ] **Step 4: Verify green and commit.** Run `pytest tests/cloud_env/test_staging_qdrant_inventory.py -q`, then `git add scripts/staging_qdrant/__init__.py scripts/staging_qdrant/inventory.py tests/cloud_env/test_staging_qdrant_inventory.py` and `git commit -m 'feat: classify retained Qdrant volumes safely'`.

### Task 2: Snapshot and fresh-volume lifecycle

**Files:** Create `scripts/staging_qdrant/aws_storage.py`, `tests/cloud_env/test_staging_qdrant_aws_storage.py`.

**Interfaces:** Consume `Inventory`; produce `snapshot_retained(aws_json, inventory, attempt_id) -> dict[int,str]`, `ensure_fresh_volumes(aws_json, inventory, attempt_id) -> tuple[Volume,...]`, `replacement_from_snapshots(aws_json, snapshot_ids, attempt_id) -> dict[int,str]`, and `delete_owned_snapshots(aws_json, snapshot_ids, attempt_id) -> None`. `aws_json(args: list[str]) -> dict` calls AWS CLI with `--region us-east-1 --output json` and is injectable for tests. Replacement volumes get new IDs and a `restored-from-snapshot` tag; originals are never deleted or retagged.

- [ ] **Step 1: Write tests first.** Test that all three snapshot IDs are recorded and completed before the caller can bind, a pending/error/timeout snapshot prevents binding, only tagged attempt-owned snapshots are deleted, fresh volumes are encrypted and tagged by ordinal, and one script-owned fresh volume allows completion of only ordinals 1 and 2. A foreign partial set raises `UnsafeInventory` without `create-volume`.

```python
def test_snapshot_failure_never_reaches_binding(fake_aws):
    fake_aws.queue("create-snapshot", ids=("snap-0", "snap-1", "snap-2"))
    fake_aws.queue("describe-snapshots", states=("completed", "error", "completed"))
    with pytest.raises(RecoveryError, match="snapshot"):
        snapshot_retained(fake_aws, old_inventory(), "attempt-1")
    assert "delete-snapshot" not in fake_aws.operations
```

- [ ] **Step 2: Verify red.** Run `pytest tests/cloud_env/test_staging_qdrant_aws_storage.py -q`; expect missing-module/function failures.
- [ ] **Step 3: Implement bounded AWS operations.** Issue `aws ec2 create-snapshot` with the exact selected `--volume-id` and attempt-owned `--tag-specifications` once per ordinal, then poll `describe-snapshots` with a monotonic deadline. Tag every snapshot with the cluster, ordinal, all-three-volume-set fingerprint, and recovery attempt ID; AWS tags are the durable retry source of truth across operator machines. An attempt-specific local JSON journal is only a cache and is written by atomic replace before proceeding. On retry, discover tagged AWS snapshots and reuse only an exact matching completed set. Use `aws ec2 create-volume --availability-zone us-east-1b --size 50 --volume-type gp3 --encrypted` plus attempt-owned tags for fresh volumes; use `--snapshot-id` and the same AZ for replacements. Never call `delete-volume`.

```python
def delete_owned_snapshots(aws_json, snapshot_ids: dict[int, str], attempt_id: str) -> None:
    for ordinal, snapshot_id in sorted(snapshot_ids.items()):
        row = aws_json(["ec2", "describe-snapshots", "--snapshot-ids", snapshot_id])["Snapshots"][0]
        tags = tags_of(row)
        if tags.get("recovery-attempt") != attempt_id or tags.get("qdrant-ordinal") != str(ordinal):
            raise RecoveryError("refusing to delete unowned snapshot")
        aws_json(["ec2", "delete-snapshot", "--snapshot-id", snapshot_id])
```

- [ ] **Step 4: Verify green and commit.** Run `pytest tests/cloud_env/test_staging_qdrant_aws_storage.py -q`, then stage these two files and commit `feat: protect Qdrant volumes during recovery`.

### Task 3: Static bindings, version gate, and three-node Qdrant

**Files:** Create `scripts/staging_qdrant/kubernetes.py`, `deploy/helm/qdrant/values.staging-recovery.yaml`, `tests/cloud_env/test_staging_qdrant_kubernetes.py`.

**Interfaces:** Produce `render_pv(volume: Volume) -> dict`, `render_pvc(volume: Volume) -> dict`, `verify_bindings(kubectl_json, volumes) -> None`, `probe_storage_version(kubectl_json, volumes) -> str`, and `install_qdrant(run, image_version: str, chart_version: str) -> None`. The probe mounts each volume read-only *after snapshots complete*, inspects Qdrant's persisted peer metadata/version, and fails closed if the three peers disagree or no version can be established. `run` and `kubectl_json` are injectable command boundaries.

- [ ] **Step 1: Write tests first.** Assert one-to-one ordinal volume handles; PV `Retain`, CSI driver, `ReadWriteOnce`, 50-GiB capacity and `us-east-1b` affinity; PVC `volumeName` and `default` namespace; no Helm call when version metadata is absent/conflicting; exactly three replicas with stable `qdrant-0/1/2` names; and no `latest` image/chart tag.

```python
def test_pv_is_bound_to_exact_ordinal_and_retained():
    pv = render_pv(old_inventory().volumes[1])
    assert pv["spec"]["csi"] == {"driver": "ebs.csi.aws.com", "volumeHandle": "vol-1"}
    assert pv["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert pv["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]["matchExpressions"][0]["values"] == ["us-east-1b"]
```

- [ ] **Step 2: Verify red.** Run `pytest tests/cloud_env/test_staging_qdrant_kubernetes.py -q`; expect missing-module/function failures.
- [ ] **Step 3: Implement static resource rendering and deployment.** Use JSON piped to `kubectl apply -f -`; existing resources must be inspected and matched before apply, never overwritten if their volume handle or claim differs. The PV claim reference is `default/qdrant-storage-qdrant-i`, PVC `spec.volumeName` is its exact PV name, and `storageClassName: ""` prevents dynamic replacement. Pin a chart version compatible with the inspected server version, render `helm template` locally, and assert the chart still creates `qdrant-0/1/2` plus `/qdrant/storage` mounts before calling `helm upgrade --install qdrant qdrant/qdrant --namespace default --version "$QDRANT_CHART_VERSION" --set image.tag="$QDRANT_IMAGE_TAG" --wait`. Reject any chart/image mismatch rather than selecting the repository's latest version.

```yaml
# deploy/helm/qdrant/values.staging-recovery.yaml
replicaCount: 3
persistence:
  size: 50Gi
  storageClassName: ""
resources:
  requests: {cpu: "500m", memory: "1Gi"}
  limits: {cpu: "2", memory: "4Gi"}
```

The fresh branch uses a pinned Qdrant 1.19.1 image/chart pair; the retained branch uses the version established by the read-only probe and stops if no matching chart is available. Do not upgrade the retained collection in this task. The API's pinned `qdrant-client==1.7.3` is not assumed compatible with 1.19.1: Task 4 must prove the configured client can search the fresh branch before calling it ready. If incompatible, update the API client pin and its compatibility tests in a separate commit before switching the API; preserve a working local Qdrant path. If no compatible client version supports both deployments, stop and document the version decision rather than declaring the fresh branch ready.

- [ ] **Step 4: Verify green and commit.** Run `pytest tests/cloud_env/test_staging_qdrant_kubernetes.py -q` and `helm template qdrant qdrant/qdrant --version 1.19.1 -f deploy/helm/qdrant/values.staging-recovery.yaml >/dev/null`; then stage these three files and commit `feat: bind persistent Qdrant replicas by ordinal`.

### Task 4: Data and API verification

**Files:** Create `scripts/staging_qdrant/verify.py`, `tests/cloud_env/test_staging_qdrant_verify.py`; conditionally modify `services/api/requirements.txt` and the API Qdrant-client tests only if the fresh-server compatibility gate requires it.

**Interfaces:** Produce `verify_cluster(qdrant_get, *, retained: bool) -> VerificationReport` and `verify_staging_api(api_post, token: str) -> None`. `VerificationReport` contains `ok: bool`, peer count, collection status, vector size/distance, point count, and a non-content query success flag. The staging verifier uses a unique session/query and `X-Load-Test-Bypass-Cache: 1`; it rejects an NDJSON `error` event or absent answer.

- [ ] **Step 1: Write tests first.** Cover three peers but zero retained points, wrong vector size/distance, missing shard, one unavailable peer, API HTTP 200 with a terminal `error` event, cached answer without a retrieval result, and a good cluster/API response.

```python
def test_stream_200_with_error_event_is_not_verified():
    events = [{"type": "routing", "action": "retrieve"},
              {"type": "error", "content": "An internal error occurred."}]
    with pytest.raises(RecoveryError, match="staging retrieval"):
        verify_staging_api(fake_stream(events), token="test-token")
```

- [ ] **Step 2: Verify red.** Run `pytest tests/cloud_env/test_staging_qdrant_verify.py -q`; expect missing-module/function failures.
- [ ] **Step 3: Implement bounded verification.** Check `/cluster` for three peers and `/collections/rag_collection` for healthy status, unnamed 1024/Cosine vectors and a positive `points_count` on retained storage. Request a small scroll and a vector query using a nonzero one-hot vector of the configured dimension to validate indexed access, without logging payloads. The API probe must use a unique query/session, the bypass header, and validate an actual retrieval operation via correlated trace/evidence or Qdrant access records rather than interpreting a successful answer as proof. Require the cache-bypass flag to be enabled; otherwise fail closed. Do not log JWTs, document payloads, or provider keys.
- [ ] **Step 4: Verify green and commit.** Run `pytest tests/cloud_env/test_staging_qdrant_verify.py -q`, stage the two files, and commit `feat: gate staging Qdrant recovery on data and API checks`.

### Task 5: Orchestration, staging handoff, and failure cleanup

**Files:** Create `scripts/staging_qdrant_recovery.py`, `tests/cloud_env/test_staging_qdrant_recovery.py`; modify `scripts/deploy_staging_evidence.sh` at its Qdrant stage and `QDRANT_HOST` value; update `docs/evaluation_implementation/local-environment.md`.

**Interfaces:** CLI `python3 scripts/staging_qdrant_recovery.py check|apply|finalize|restore --region us-east-1 --cluster rag-platform-cluster`; `run_recovery(runtime, mode: str) -> int` implements the CLI state machine using an injectable runtime adapter. The adapter exposes `load_attempt(attempt_id)`, `stop_qdrant()`, `rebind_preserving_originals(replacements)`, `aws_json(args)`, and `qdrant_get(path)` in addition to inventory, command, API, and journal operations. Apply returns nonzero on any unverified phase and prints machine-readable `status`, `branch`, and selected service host. `restore` requires an attempt ID, stops Qdrant, creates and binds replacement volumes from that attempt's completed snapshots, and runs verification without deleting originals or snapshots. `deploy_staging_evidence.sh` invokes apply before API Helm upgrade, then invokes final verification/snapshot cleanup after rollout.

- [ ] **Step 1: Write tests first.** Fake AWS/Kubernetes/Helm processes to assert the order `inventory → snapshot-complete → PV/PVC → version gate → Qdrant ready → direct verification → API switch → API verification → delete owned snapshots`. Assert no cleanup on post-mount failure, no snapshot on healthy reuse/fresh branch, no API switch for partial set, and preservation of current dotenv/Terraform preflight behavior. Add an interrupted-at-each-phase test using a durable attempt journal. Test `restore` creates three new volumes from the recorded snapshot IDs, preserves all original volume IDs, and retains snapshots when replacement verification fails.

```python
def test_snapshot_deletion_requires_successful_api_probe(fake_runtime):
    fake_runtime.api_result = {"events": [{"type": "error"}]}
    assert run_recovery(fake_runtime, mode="apply") != 0
    assert "delete-snapshot" not in fake_runtime.operations
    assert "switch-api" in fake_runtime.operations
    assert "restore-api-host" in fake_runtime.operations
```

- [ ] **Step 2: Verify red.** Run `pytest tests/cloud_env/test_staging_qdrant_recovery.py -q`; expect missing CLI/ordering failures.
- [ ] **Step 3: Implement the state machine and integration.** Replace the staging script's ephemeral-Qdrant apply/rollout block with the explicit `apply` invocation below; preserve the existing ephemeral deployment until verification and set the API Helm value to `qdrant.default.svc.cluster.local`. Make the cleanup a separate verified CLI phase after API rollout, not an unconditional shell `trap`. When API verification fails, restore its previous Helm host and retain snapshots. Print the attempt ID and safe volume/snapshot IDs, never credentials.

```bash
python3 "${SCRIPT_DIR}/staging_qdrant_recovery.py" apply \
  --region "$REGION" --cluster "$CLUSTER_NAME"
# API Helm upgrade uses QDRANT_HOST=qdrant.default.svc.cluster.local.
python3 "${SCRIPT_DIR}/staging_qdrant_recovery.py" finalize \
  --region "$REGION" --cluster "$CLUSTER_NAME" --namespace "$NAMESPACE"
```

`finalize` verifies API retrieval and only then deletes attempt-owned snapshots; a failed run exits nonzero and retains them. Document `check`, `apply`, `finalize`, partial-volume failure, and snapshot-derived replacement commands without recording secret values.

```python
def restore(runtime, attempt_id: str) -> int:
    attempt = runtime.load_attempt(attempt_id)
    runtime.stop_qdrant()
    replacements = replacement_from_snapshots(runtime.aws_json, attempt.snapshots, attempt_id)
    runtime.rebind_preserving_originals(replacements)
    report = verify_cluster(runtime.qdrant_get, retained=True)
    if not report.ok:
        raise RecoveryError("replacement verification failed; snapshots retained")
    return 0  # finalize performs API verification and owned-snapshot deletion
```

- [ ] **Step 4: Verify green and commit.** Run `pytest tests/cloud_env/test_staging_qdrant_*.py tests/local_env/test_staging_cluster_prerequisite.py -q`, `bash -n scripts/deploy_staging_evidence.sh`, and `git diff --check`; stage only Task 5 files and commit `feat: recover persistent Qdrant during staging deploy`.

### Task 6: Local and live acceptance gates

**Files:** Test `tests/cloud_env/test_staging_qdrant_*.py`, `tests/cloud_env/test_cloud_smoke.py`; document observations in `docs/evaluation_implementation/local-environment.md` only if the prior task did not already cover them.

- [ ] **Step 1: Run local suite.** Run `pytest -q` and record every failure or skip by name; run the focused cloud-env tests and shell syntax check again. Do not treat tests that require a live AWS cluster as ordinary unit tests.
- [ ] **Step 2: Read-only AWS preflight.** Run `python3 scripts/staging_qdrant_recovery.py check --region us-east-1 --cluster rag-platform-cluster`, `aws ec2 describe-volumes` for the exact discovered IDs, and `kubectl get pv,pvc,statefulset -A`. Confirm three available 50-GiB volumes in `us-east-1b`, exact ordinal tags, no conflicting bindings, EBS CSI healthy, and a compatible pinned Qdrant image/chart. Stop if any check is ambiguous.
- [ ] **Step 3: Apply with an observable checkpoint.** Run `scripts/deploy_staging_evidence.sh` once; its internal recovery `apply` phase must print the attempt ID and three completed snapshot IDs before the API host switch. At that checkpoint, record PV/PVC mappings, peer versions, health, collection status, point count, and Qdrant query result. If any phase fails, stop and preserve snapshots; do not run a fresh initializer.
- [ ] **Step 4: Switch and finalize.** Let the same deploy invocation switch the API only after direct Qdrant checks pass. Its scripted `finalize` phase checks a non-cached authenticated request against the recovered host and deletes only this attempt's temporary snapshots after success. Verify snapshot absence and that all three EBS volumes remain; run one manual request before a bounded Locust test.
- [ ] **Step 5: Report state.** Record exact test results, cluster status, three EBS IDs, point count, and whether temporary snapshots were deleted. If verification fails, report the attempt ID and retained snapshots, not a success claim. Do not delete any volume or unrelated snapshot.
