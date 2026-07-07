# Kubernetes 1.30 → 1.35 Upgrade Plan

**Date drafted:** 2026-06-19
**Cluster:** `rag-platform-cluster` (EKS, us-east-1)
**Context:** Cluster is not currently running. This is a fresh recreation at the target version, not an in-place upgrade. The one-minor-version-at-a-time constraint does not apply.

---

## Current state

| Component | Version |
|---|---|
| Kubernetes | 1.30 (extended support, ending ~July 2026) |
| Terraform EKS module | `terraform-aws-modules/eks/aws ~> 19.0` |
| Karpenter NodePool API | `karpenter.sh/v1` |
| Karpenter controller | 1.0.8 (in `bootstrap_cluster.sh:87`) |
| KubeRay operator | 1.0.0 (in `bootstrap_cluster.sh:123`) |
| AMI family | AL2023 (`al2023@latest`) |

---

## Phase 1 — Verify component compatibility ✅ RESOLVED 2026-07-06

All blockers verified and resolved; code changes applied.

**1.0 EKS 1.35 availability — confirmed.** `aws eks describe-cluster-versions` shows
1.33–1.36 in STANDARD_SUPPORT, 1.30–1.32 in EXTENDED_SUPPORT. 1.36 exists but the plan
stays on 1.35 (one version of headroom below bleeding edge; still ~2 years of standard
support).

**1.1 Karpenter 1.0.8 → 1.13.0.** Compatibility matrix
([karpenter.sh](https://karpenter.sh/docs/upgrading/compatibility/)): K8s 1.35 needs
Karpenter ≥ 1.9; 1.0.x tops out at K8s ~1.32. Pinned the latest stable **1.13.0**
(also covers a future 1.36 hop). Both NodePool manifests already use `karpenter.sh/v1`
/ `karpenter.k8s.aws/v1` — no manifest changes needed. Pin updated in
`bootstrap_cluster.sh` stage 2.

**1.2 KubeRay 1.0.0 → 1.2.2 — NOT latest, deliberately.** The Ray-version constraint
dominates the K8s one: KubeRay **1.3+ requires Ray ≥ 2.38**, and the ray-serve /
ingestion images run **Ray 2.9.0** (per `rayVersion` in the RayService manifests).
KubeRay 1.1/1.2 support Ray 2.8+, so **1.2.2** is the ceiling without rebuilding the
~28 GB GPU image stack (vLLM 0.4.2 + Ray 2.9 patches). Manifests already use
`ray.io/v1` (v1alpha1 was dropped in KubeRay 1.1 — not used here). Bonus: 1.2.2 should
contain the `serveService.spec.ports` fix worked around in bootstrap (backlog item) —
verify at deploy and restore the stanza if confirmed. Pin updated in
`bootstrap_cluster.sh` stage 7. A KubeRay ≥ 1.3 upgrade is a separate project coupled
to a Ray/vLLM image upgrade.

**1.3 EBS CSI / core add-ons — no change needed.** `aws_eks_addon.ebs_csi` has no
`addon_version` pin, so it resolves the default version for the cluster's K8s version
at create time.

**1.4 NVIDIA device plugin v0.15.0 → v0.17.1** (latest official release) in
`deploy/k8s/nvidia-device-plugin.yaml`. The plugin uses stable kubelet APIs; the bump
is precautionary currency, not a hard requirement.

**1.5 Terraform module target: v20, not v21.** v21 renames the core inputs
(`cluster_name` → `name`, `cluster_version` → `kubernetes_version`, …), requires AWS
provider v6, and moves the ecosystem toward EKS Pod Identity — while this stack uses
IRSA throughout (`enable_irsa`, `module.eks.oidc_provider*` in ebs-csi/karpenter/
ingestion IAM). v20 gets us off extended support with the minimal blast radius; the
v21 + Pod Identity migration is a separate, optional pass.

**API version scan (manifests)**
All existing manifests use stable API versions — no deprecated core API versions were found. This step is a formality but worth re-running if manifests change before the upgrade executes:
```bash
# kubent can scan files without a live cluster
kubent --filename deploy/ --filename infra/ --target-version 1.35
```

---

## Phase 2 — Upgrade the Terraform EKS module (v19 → v20)

### The auth mode breaking change

v19 managed cluster auth via `manage_aws_auth_configmap = true` + `aws_auth_roles`. v20 defaults to EKS access entries (API mode) and removes `aws_auth_roles`. The current `karpenter.tf` uses CONFIG_MAP mode specifically because `aws_eks_access_entry` required API mode — since we're creating fresh, switch to API mode now and do it cleanly.

**In `infra/terraform/eks.tf`:**

```hcl
module "eks" {
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"          # was ~> 19.0

  cluster_version = "1.35"     # was "1.30"

  # Remove:
  # manage_aws_auth_configmap = true
  # aws_auth_roles = [...]

  # Add:
  authentication_mode = "API"
  access_entries = {
    karpenter_nodes = {
      principal_arn = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:role/KarpenterNodeRole-${var.cluster_name}"
      type          = "EC2_LINUX"
    }
  }
  ...
}
```

Delete the now-stale CONFIG_MAP comment in `infra/terraform/karpenter.tf`.

### Other v20 changes

Read [UPGRADE-20.0.md](https://github.com/terraform-aws-modules/terraform-aws-eks/blob/master/docs/UPGRADE-20.0.md) for any variable renames affecting the managed node group inputs. A dedicated migration module exists (`clowdhaus/terraform-aws-eks-migrate-v19-to-v20`) but is only needed for in-place upgrades of existing clusters — skip it here.

---

## Phase 3 — Apply Terraform

```bash
cd infra/terraform

terraform init -upgrade          # pulls the new module version

terraform plan -out=upgrade.tfplan   # read the full plan before applying

terraform apply upgrade.tfplan
```

Expected time: 15–20 min for EKS control plane + system node group. Confirm before proceeding:

```bash
aws eks describe-cluster --name rag-platform-cluster --query 'cluster.status'
# expected: "ACTIVE"

kubectl get nodes   # 2× m6i.large system nodes should be Ready
```

---

## Phase 4 — Bootstrap stages 1–10

These stages don't require image tags or DB secrets:

```bash
./scripts/bootstrap_cluster.sh --start 1 --end 10
```

| Stage | What | Notes |
|---|---|---|
| 1 | kubeconfig | — |
| 2 | Karpenter controller | Use version confirmed in Phase 1 |
| 3 | Karpenter NodePools | GPU + CPU NodePools applied |
| 4 | gp3 StorageClass | — |
| 5 | NVIDIA device plugin | Use version confirmed in Phase 1 |
| 6 | Helm repos | — |
| 7 | KubeRay operator | Use version confirmed in Phase 1 |
| 8 | Qdrant | Fresh install |
| 9 | Neo4j | Fresh install |
| 10 | Qdrant collection schema | Creates `rag_collection` + `semantic_cache` at 1024-dim |

Validate after stage 10:
```bash
kubectl get nodes -L karpenter.sh/nodepool   # GPU nodes should appear after stage 3
kubectl get pods -A                           # everything Running or Completed
kubectl port-forward svc/qdrant 6333:6333 &
curl -s http://localhost:6333/collections | jq '.result.collections'
```

---

## Phase 5 — Bootstrap stages 11–14

Requires image tags and infrastructure outputs from Terraform:

```bash
export API_IMAGE_TAG=<latest ECR tag for services/api>
export RAY_IMAGE_TAG=<latest ECR tag for services/ray-serve>
export DB_SECRET_ARN=$(terraform -chdir=infra/terraform output -raw db_secret_arn)
export DB_ENDPOINT=$(terraform -chdir=infra/terraform output -raw db_endpoint)
export REDIS_URL=rediss://$(terraform -chdir=infra/terraform output -raw redis_primary_endpoint)
export S3_BUCKET=$(terraform -chdir=infra/terraform output -raw s3_documents_bucket_name)
export JWT_SECRET_KEY=$(openssl rand -hex 32)

./scripts/bootstrap_cluster.sh --start 11 --end 14
```

| Stage | What | Pre-check |
|---|---|---|
| 11 | `app-env-secret` (ExternalSecret) | All env vars above must be set |
| 12 | API Helm deploy | `API_IMAGE_TAG` set; image exists in ECR |
| 13 | Ray Serve (LLM + embed) | `RAY_IMAGE_TAG` set; model weights in S3 |
| 14 | Queue-backed ingestion worker | SQS queue exists (Terraform output) |

If images were built against old base images, rebuild before this phase. The K8s version change does not affect image internals.

---

## Phase 6 — Validate end-to-end

```bash
# Ray health — covers CRD state, ray status, and live HTTP probes
./scripts/check_ray_health.sh

# API health
kubectl get pods -l app=rag-api
kubectl port-forward svc/api-service 8000:80 &
curl http://localhost:8000/health/liveness
curl http://localhost:8000/health/readiness

# Smoke ingest (TXT — no heavy deps, confirms vectors land in Qdrant)
python scripts/ingest_local.py data/docs/ --no-graph
curl -s http://localhost:6333/collections/rag_collection | jq '.result.points_count'
# should be > 0
```

---

## Summary of code changes

All applied 2026-07-06:

| File | Change |
|---|---|
| `infra/terraform/eks.tf` | `version = "~> 20.0"`, `cluster_version = "1.35"`, replaced `manage_aws_auth_configmap`/`aws_auth_roles` with `authentication_mode = "API"` + `access_entries` (node role referenced directly via `aws_iam_role.karpenter_node.arn`), added `enable_cluster_creator_admin_permissions = true` (v20 defaults it false — without it kubectl is locked out of the fresh cluster) |
| `infra/terraform/karpenter.tf` | CONFIG_MAP comment replaced with a pointer to the access entry |
| `infra/terraform/ebs-csi.tf` | No change — add-on version unpinned, resolves per cluster version |
| `scripts/bootstrap_cluster.sh` (stage 2) | Karpenter `1.0.8` → `1.13.0` |
| `scripts/bootstrap_cluster.sh` (stage 7) | KubeRay `1.0.0` → `1.2.2` (ceiling while images run Ray 2.9.0) |
| `deploy/k8s/nvidia-device-plugin.yaml` | `v0.15.0` → `v0.17.1` |

---

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Karpenter / KubeRay version gaps | Resolve in Phase 1 before any code changes |
| GPU node downtime during Ray Serve bring-up | Plan during low-traffic window; cold start is ~5–7 min per node |
| EKS module v20 variable renames causing plan failures | Read UPGRADE-20.0.md before applying; use `terraform plan` to catch drift |
| 1.30 extended support cost still accruing until cluster is up | Recreating at 1.35 eliminates it permanently; no interim fix |
