# Staging Qdrant recovery: simplified specification

Status: supersedes the scope of `staging-qdrant-recovery-design.md` and `docs/superpowers/plans/2026-09-30-staging-qdrant-recovery.md` (2026-09-30). Where they disagree, this document wins. Code that already exceeds this scope may stay, but nothing in it is a requirement.

## Requirement

When staging is brought up, the API must query the retained `rag_collection` EBS data if it exists, or a new persistent Qdrant if it does not. A failure must never cost us the retained data. Staging data can be re-ingested from `eval/datasets/`, so the goal is "don't lose it by accident", not "survive every conceivable interruption".

## Flow

1. **Discover.** Find the Qdrant volumes by exact tags: `kubernetes.io/created-for/pvc/name` = `qdrant-storage-qdrant-{0,1,2}`, namespace `default`, `ebs.csi.aws.com/cluster-name` = the cluster.
   - **Zero volumes:** create three encrypted 50-GiB gp3 volumes in `us-east-1b`, bind them, install Qdrant at the pinned fresh version, create `rag_collection` (1024, Cosine), verify the schema.
   - **Exactly three:** go to step 2.
   - **One or two, duplicates, wrong size/AZ, foreign attachment:** stop and report. Never fill in the gap.
2. **Protect, then mount.** Snapshot all three volumes and wait until every snapshot is `completed`. Only then bind, determine the on-disk Qdrant version, install that exact version, and verify: three peers, green collection, 1024/Cosine, nonzero point count, and a search that returns a point.
3. **On any failure after the first mount:** stop and exit nonzero. Leave the original volumes and the snapshots untouched. Print the attempt ID, volume and snapshot IDs, and the manual restore commands. Restore is a documented operator procedure, not automation, because most post-mount failures are configuration problems (version, chart, networking) that a restore does not fix.
4. **Re-run safety.** A healthy, verified three-replica Qdrant is reused: no new volumes, no new snapshots.

## Invariants (these are the whole safety case)

- Volumes are matched by exact tag and ordinal, never by list order or name substring.
- No snapshot means no mount of a retained volume.
- No code path calls `delete-volume`.
- PVs are `Retain`; claims bind statically by `volumeName`.
- The Qdrant version is established from the disk and never guessed. The original EKS Qdrant was installed from the chart without a version pin (`scripts/bootstrap_cluster.sh` stage 8), so the retained data's version is unknown until read. Never start an older image than the one that wrote the data; never use `latest`. If the version cannot be determined or has no reviewed image/chart pair, stop before starting Qdrant.
- The API switches to `qdrant.default.svc.cluster.local` only after direct Qdrant verification passes.
- A staging API request with cache bypass must succeed and leave retrieval evidence before the run is reported healthy.
- Credentials, JWTs and document payloads are never printed.

## Explicitly out of scope

Resuming across operator machines; automated restore; automated snapshot deletion (keep snapshots until an operator removes them, since the cost is pennies); zero-downtime switching; multi-AZ placement; backup policy; Qdrant version migration.

## Acceptance

- Local: `pytest tests/cloud_env/test_staging_qdrant_*.py tests/local_env/test_staging_cluster_prerequisite.py` and `bash -n scripts/deploy_staging_evidence.sh` pass; `python3 scripts/staging_qdrant_recovery.py --help` runs from the repo root and from `scripts/`.
- Live, read-only first: `check` reports `retained` with the three expected volume IDs, all `available`, 50 GiB, `us-east-1b`.
- Live apply: three completed snapshots exist before any pod mounts a volume; Qdrant reports three peers and a nonzero point count; one authenticated API request returns an answer with retrieval evidence; all three volume IDs are unchanged.
- Report: test results, cluster state, volume and snapshot IDs, point count, and whether snapshots remain.

## Known risks to watch in the live run

- The version probe reads `raft_state.json`; if that file lacks per-peer version metadata for the retained data, apply stops before starting Qdrant. That is a correct failure, and the fix is to add the discovered version to the reviewed image/chart table.
- The API client is `qdrant-client==1.7.3`; a fresh-branch server at a much newer version is untested against it. The API verification step is the gate.
- Volumes are KMS-encrypted; the node/EBS CSI role needs access to the key for attach to succeed.
