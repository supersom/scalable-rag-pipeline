# Staging Qdrant recovery and persistent bring-up

## Purpose and current state

The staging evidence API must be able to query the retained `rag_collection` after
the EKS cluster is recreated. Today `deploy/k8s/staging-qdrant.yaml` runs a
single, ephemeral Qdrant in `staging-evidence`; it has no PVC and therefore
cannot see the three retained Qdrant EBS volumes. The original Qdrant release
used three replicas and 50-GiB EBS-backed claims in `default`. The retained
volumes are tagged for `qdrant-storage-qdrant-{0,1,2}` and reside in
`us-east-1b`.

Success means that a staging bring-up either reconnects the complete retained
dataset or creates a new, persistent three-replica Qdrant when **none** of the
retained volumes exists. It must never silently replace a partially missing
dataset with empty replicas. The staging API switches to the recovered service
only after verification. Re-running deployment must be safe and must not create
new volumes or snapshots for a healthy existing cluster.

## Ownership and interfaces

- A dedicated, version-controlled recovery command owns Qdrant volume
  discovery, snapshot lifecycle, PV/PVC binding, cluster deployment, and
  verification. `scripts/deploy_staging_evidence.sh` invokes it before deploying
  or switching the staging API. The command has explicit `check` and `apply`
  modes and prints the selected branch and volume-to-ordinal mapping.
- The restored Qdrant service lives in `default`, matching the original
  StatefulSet and peer DNS identity. The staging API uses the explicit host
  `qdrant.default.svc.cluster.local`, not the ephemeral service of the same name
  in `staging-evidence`. The ephemeral staging Qdrant is left untouched until
  the recovered service passes all checks; it is not a recovery source.
- Discovery uses the exact cluster, namespace, and PVC-name tags for ordinals
  0, 1, and 2, not a broad `Name` substring or list order. The command rejects
  duplicate matches, unexpected size/AZ, an attachment to another instance,
  a mismatched existing PV/PVC binding, and a partial set. Volume IDs are
  recorded in the operation report but not hard-coded into the script.
- The target Qdrant image and Helm chart are pinned; recovery never uses a
  moving `latest` tag. Before an existing disk is started, the implementation
  must establish a compatible Qdrant version from the prior deployment or a
  read-only inspection. If compatibility cannot be established, it stops
  without starting Qdrant on the retained disks. Any version migration is a
  separate, explicitly reviewed operation; downgrades are prohibited.

## Bring-up state machine

1. **Preflight:** Verify account, region, cluster, EBS CSI driver, a usable
   `us-east-1b` subnet and scheduler capacity, and the exact volume set. Check
   the current PV/PVC/StatefulSet bindings before any AWS mutation. An already
   healthy, verified three-replica deployment is reused without snapshots.
2. **All three retained volumes found:** Require all to be `available` before
   first binding. Take and tag one EBS snapshot per ordinal, wait until all
   three are `completed`, and record their IDs before any Qdrant pod can mount
   the originals. If a snapshot fails, stop; do not bind the disks.
3. **No retained volumes found:** Create three encrypted, 50-GiB EBS volumes in
   `us-east-1b`, with stable cluster/replica tags and a fresh-provisioning
   attempt ID. Each becomes a statically bound PV/PVC with reclaim policy
   `Retain`. Do not take empty-volume snapshots. A retry may complete missing
   ordinals only when every discovered volume belongs to the same incomplete,
   script-owned fresh attempt and no retained dataset volume is present.
4. **Partial or ambiguous retained set:** Fail closed with the observed ordinal
   mapping. Do not provision missing old ordinals, start Qdrant, or switch the
   API. An interrupted recovery attempt may resume only when its exact three
   volume IDs and completed, tagged snapshots still match; otherwise stop for
   inspection rather than create duplicate snapshots.
5. **Bind and start:** Bind ordinal `i` only to its matching volume. Use the
   EBS CSI driver, `ReadWriteOnce`, 50-GiB capacity, `Retain`, and node affinity
   to `us-east-1b`. Restore the three-replica StatefulSet and stable peer
   addresses in `default`. The storage mount points at Qdrant's data directory;
   no init container formats, empties, or recreates the filesystem.
6. **Verify before routing:** Require three ready peers, healthy collection
   status, the expected unnamed 1024-dimensional Cosine vector configuration,
   a nonzero point count for the retained-data branch, and successful Qdrant
   scroll/query access. For the new-volume branch, verify an
   empty collection with the correct schema and require ingestion before a
   data-dependent load test. Do not treat mere pod readiness as proof of data
   recovery.
7. **Switch, verify, and clean up:** After direct Qdrant verification,
   configure the staging API to use `qdrant.default.svc.cluster.local`. Confirm
   that staging retrieval succeeds through that host with the configured
   embedding provider. If it fails, restore the prior API host and retain the
   recovery snapshots. Only after this end-to-end check, delete the temporary
   snapshots tagged with this recovery attempt. Do not delete older or
   unrelated snapshots. Snapshot deletion failure is reported but does not
   undo a healthy deployment.

## Failure and rollback

Failure before any mount leaves the original disks unchanged. Stop and report
the incomplete operation; snapshots created for that attempt may be removed
only after verifying that no pod mounted or wrote to the disks. Failure after a
mount stops Qdrant and retains both the original EBS volumes and the completed
snapshots. It does **not** automatically re-run a possibly incompatible image
against another copy. After diagnosing the cause, the recovery command can
create replacement EBS volumes from the tagged snapshots, rebind the matching
PV/PVCs without deleting the originals, and repeat the full data and API
verification. Only a verified replacement permits deletion of its source
snapshots. If replacement verification fails, keep the snapshots; deleting them
would discard the pre-attempt recovery point.

All created PVs use `Retain`, and the recovery command never deletes an EBS
volume. The normal cluster teardown must preserve both volume data and the
ordinal tags needed for later discovery. The temporary deployment snapshots
are not an ongoing backup policy; separately managed backups are outside this
change.

## Validation and rollout

- Unit/contract tests cover complete, absent, partial, duplicate, and already
  bound volume sets; exact ordinal binding and AZ; no mutation on failed
  preflight; snapshot completion before mount; retry idempotency; and cleanup
  limited to snapshots created by the successful attempt.
- Failure tests cover snapshot creation failure, pod readiness without a
  collection, wrong vector configuration, zero retained points, failed API
  retrieval, failed snapshot-derived replacement, and interrupted reruns. Each
  must leave the only recoverable copy intact.
- Validate rendered Kubernetes resources and shell/Python scripts locally
  before AWS changes. On AWS, record the preflight mapping, snapshot IDs,
  PV/PVC-to-volume bindings, peer health, collection configuration/point count,
  API retrieval result, and final snapshot/volume inventory.
- Keep the existing ephemeral staging Qdrant available until the recovered
  service passes verification; then switch only the staging API. Do not run
  Locust until a single authenticated streaming request completes without an
  terminal error event.

## References

- [Kubernetes Persistent Volumes](https://kubernetes.io/docs/concepts/storage/persistent-volumes/)
- [AWS EBS snapshot restore](https://docs.aws.amazon.com/ebs/latest/userguide/ebs-restoring-volume.html)
- [Qdrant distributed deployment](https://qdrant.tech/documentation/scaling/distributed_deployment/)
- [Qdrant version upgrades](https://qdrant.tech/documentation/operations/upgrades/)
