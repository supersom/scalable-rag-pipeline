# Kubernetes Setup — scalable-rag-pipeline

Snapshot of the EKS deployment as of 2026-09-16, compiled from `infra/terraform/*.tf`,
`infra/karpenter/*.yaml`, `deploy/**`, `DEVLOG.md`, `docs/diagrams/infra_topology.md`,
`docs/diagrams/current_architecture.md`, and today's
`docs/deployment-handoff-2026-09-16.md` / `docs/deployment-resume-2026-09-16.md`.

## Cluster

EKS v1.35, `rag-platform-cluster`, single region (us-east-1), one VPC
(`10.0.0.0/16`) across 3 AZs with public/private/database subnet tiers.
IRSA/OIDC is enabled and is the trust anchor for every pod-level AWS
permission in the cluster.

## Compute — two-tier node model

### 1. Managed system node group (Terraform-owned, `eks.tf`)

`m6i.large`, min 2 / desired 2 / max 5, tainted `CriticalAddonsOnly`. Exists
solely to host cluster-critical controllers — CoreDNS, the Karpenter
controller itself, the EBS CSI controller — so they're never competing for
capacity with, or evicted alongside, workload pods.

### 2. Karpenter-provisioned nodes

**Not** Terraform-managed — applied via `kubectl` from `infra/karpenter/`;
Terraform only creates the IAM roles/EKS access entry Karpenter needs.

All workload capacity comes from here, split into two NodePools sharing one
`EC2NodeClass` (AL2023, 100GB gp3 root volume — bumped from the 20GB default
after it caused `DiskPressure` evictions with the 15GB Ray GPU image):

- **`cpu` NodePool** — `m6i`/`c6i`/`r6i`, spot **and** on-demand, no floor on
  size. Backs Ray CPU heads, the ingestion worker, and general CPU workloads.
- **`gpu` NodePool** — pinned tight: `g5` family, `2xlarge` size only,
  **on-demand only**, tainted `nvidia.com/gpu=true:NoSchedule`. This is
  deliberately narrow because the account's On-Demand G-family vCPU quota is
  exactly 16 — two `g5.2xlarge` (8 vCPU each) is the ceiling. That means
  **zero surge room**: a rolling RayService update can't bring up new GPU
  workers while old ones still hold the two nodes, so every model rollout
  requires a brief inference outage (documented repeatedly in DEVLOG, most
  recently during today's recovery).

Both NodePools consolidate empty/underutilized nodes after 30s — aggressive
enough that it has repeatedly evicted stateful/Ray-head pods mid-recovery
(Qdrant, embed-service head) before a proper `do-not-disrupt` pod-template
annotation was ever codified. It's currently only applied ad hoc, by hand,
during incidents — still open in `BACKLOG.md`.

## Workloads

| Component | Shape | Notes |
|---|---|---|
| `api` | Deployment (Helm chart, `deploy/helm/api`) | FastAPI + LangGraph agent, all in-process — no per-node microservice split |
| `qdrant` | StatefulSet (Helm) | 3 replicas, gp3 PVCs |
| `neo4j-cluster` | StatefulSet (Helm, community edition) | 1 replica — Neo4j Community has no read replicas, so this is a real scaling ceiling, not just unconfigured |
| `kuberay-operator` | Deployment | Manages the RayCluster/RayService CRDs |
| `embed-service`, `llm-service` | RayService (head + GPU worker) | `enableInTreeAutoscaling: true`; vLLM (Llama-3-8B-Instruct) and BGE-M3 embeddings respectively, each on one `g5.2xlarge` |
| `ingestion-worker` | Deployment (SQS consumer) + ephemeral Ray Data jobs on a dedicated CPU `ingestion-ray` RayCluster | Kept off the GPU RayServices entirely — CPU-only, calls LLM/embed over HTTP |
| `ui` | Deployment + ClusterIP service (`ui-service`) | Streamlit, deployed today (`deploy/k8s/ui.yaml`, `scripts/deploy_ui.sh`); internal-only, no ingress — reached via `kubectl port-forward` |
| Redis, Postgres | *Not pods* | ElastiCache and Aurora Serverless v2/RDS — AWS-managed, despite being drawn as StatefulSets in the architecture diagram for narrative symmetry |
| `sandbox-service` | *Not deployed* | Tool code references it (`SANDBOX_URL` hardcodes K8s DNS), but no Deployment/Service manifest exists anywhere — dead tool path, flagged in `BACKLOG.md` |

No ingress controller is installed (`deploy/ingress/{kong,nginx}.yaml` exist
but aren't applied) — nothing is exposed outside the cluster; everything is
reached via `kubectl exec`/`port-forward`.

## Storage & networking

`gp3` StorageClass via the EBS CSI addon (Terraform-managed) backs all PVCs.
Networking is a single shared NAT Gateway (not per-AZ) plus an S3 Gateway
endpoint and four interface endpoints (`ecr.dkr`, `ecr.api`, `sts`,
`secretsmanager`) that keep image pulls, IRSA token exchange, and secret
fetches off the NAT entirely.

## IAM/IRSA

Three IRSA roles: Karpenter controller, EBS CSI controller, and
`rag-ingestion-role` (scoped to exactly the ingestion service accounts for
S3/SQS). A fourth, `rag-ray-models-role`, was added **today** — GPU pods
sync HF weights from S3 (`rag-platform-models-prod-7649`) at startup, and it
turned out node-role credentials don't propagate to pods under IMDSv2
hop-limit-1, so a dedicated IRSA role trusted to `default:ray-models` was
required.

## Current live state (as of 2026-09-16)

The cluster was fully scaled to zero yesterday for cost
(`docs/deployment-handoff-2026-09-16.md`) and brought back up today
(`docs/deployment-resume-2026-09-16.md`). Along the way three Ray Serve
image bugs were fixed in sequence — Starlette middleware incompatibility,
missing AWS CLI in the image, and a Ray 2.9/Starlette 0.35 route-prefix
mismatch (`/embeddings` vs `/embed/embeddings`) — landing on image tag
`34fe28e-fastapi109-awscli-cachefix-routefix`. As of the last check: API
2/2, Qdrant 3/3, Neo4j 1/1, both RayServices `Running` and verified
end-to-end (1024-dim embeddings, LLM completions both confirmed), UI
deployed and reachable in-cluster. Several nodes are still temporarily
annotated `karpenter.sh/do-not-disrupt=true` to protect warm image caches —
those need to be removed once things stabilize (noted in the resume doc
itself).

## Open structural gaps

- No `karpenter.sh/do-not-disrupt` annotation baked into pod templates
  (Qdrant, Ray heads, ingestion worker) — only applied by hand during
  incidents.
- GPU On-Demand quota (16 vCPU) exactly matches the two `g5.2xlarge` nodes
  in steady state — no headroom for a zero-downtime RayService rollout.
- No ingress controller installed — nothing reachable outside the cluster.
- `sandbox-service` tool has no deployment — the registered `sandbox` tool
  is dead in every environment.
