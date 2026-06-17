# DEVLOG — scalable-rag-pipeline

Chronological record of design decisions, architectural trade-offs, and non-obvious insights.
Focus: *why*, not *what* (git log has the what).

---

## 2026-06-17 • vLLM engine init: EngineArgs vs AsyncEngineArgs

**Problem:** `AsyncLLMEngine.from_engine_args()` in vLLM 0.4.2 accesses fields (`engine_use_ray`, `disable_log_requests`, `disable_log_stats`, `max_log_len`) that exist on `AsyncEngineArgs` but NOT on the base `EngineArgs` dataclass. Our code was passing `EngineArgs`, causing successive `AttributeError` crashes on each retry.

**Fix:** Import and use `AsyncEngineArgs` from `vllm.engine.arg_utils` instead of `EngineArgs`. `AsyncEngineArgs` is a subclass that adds the async-specific fields `from_engine_args()` expects. Also removed the `cpu_offload_gb` parameter — it was added in vLLM ≥0.5 and our image pins 0.4.2.

**Why not upgrade vLLM?** Ray 2.9.0 (our base image) pins transitive deps that conflict with vLLM ≥0.5's torch version. Upgrading requires bumping the Ray base image AND `rayVersion` in RayService manifests AND confirming KubeRay operator compatibility — a coordinated change. Pinning 0.4.2 with the correct entry point is the lower-risk fix.

---

## 2026-06-17 • RayService: enableInTreeAutoscaling and serveService

**enableInTreeAutoscaling:** Without this field in `rayClusterConfig`, the Ray autoscaler monitor runs inside the head pod but has no way to signal KubeRay to scale the worker group. GPU worker pods were never created despite `min_replicas: 1` in the serve config. Added `enableInTreeAutoscaling: true` to both RayService manifests — now Karpenter provisions GPU nodes automatically when serve demand arrives.

**serveService:** KubeRay only creates the port-8000 Kubernetes Service (the one the API points at) if a `serveService` block is present in the RayService spec. Without it there is no in-cluster endpoint for the API to reach Ray Serve. Added to both manifests with `name: llm-service` / `name: embed-service` matching the defaults in `services/api/app/config.py` so no secret patching is needed.

**FailedToUpdateService:** This KubeRay state occurs when the controller's old-to-new cluster traffic cutover fails — typically because the old cluster was already deleted (manually) before the controller got to delete it. The state is not terminal; the controller retries with exponential backoff. Workaround: annotate the RayService with a dummy key (`kubectl annotate rayservice ... force-reconcile=<ts>`) to trigger immediate reconciliation. Avoid rapid `delete + apply` cycles — prefer `kubectl apply` only so the controller's internal state machine can track the rollout.

---

## 2026-06-17 • Session checkpoint — EKS cluster operational, GPU serving image built

**What's running:** API 2/2, Qdrant 3/3, Neo4j 1/1, Ray head + CPU worker. All core services healthy on 4 EKS nodes. 10 commits landed on main covering Terraform fixes, Karpenter v1 migration, API image, GPU serving rework, device plugin, gp3 StorageClass, Neo4j Helm deploy.

**In flight:** `docker push` of `services/ray-serve:f25f720` to ECR — large image (~15GB), still uploading CUDA/torch layers.

**Next steps once push completes:**
1. `kubectl apply -f deploy/ray/ray-serve-llm.yaml && kubectl apply -f deploy/ray/ray-serve-embed.yaml`
2. Karpenter provisions g5.2xlarge (8 vCPU, 1×A10G, 100GB volume)
3. LLM head pulls ECR image, vLLM loads Llama-3-8B
4. End-to-end chat validation via API

---

## 2026-06-17 • EKS node disk pressure — root volume sizing and Ray head image fix

**Problem:** Karpenter provisioned nodes with the default AL2023 root volume (20GB). Pulling `rayproject/ray:2.9.0-py310-gpu` (~15GB uncompressed) filled the node's ephemeral storage, triggering `DiskPressure` taints and evicting all pods on the node. This caused a cascade: Qdrant evicted, API crashed, Ray head evicted, CPU worker stuck Pending.

**Fix 1 — EC2NodeClass root volume:** Added `blockDeviceMappings: /dev/xvda, 100Gi gp3 encrypted` to `infra/karpenter/provisioner-cpu.yaml`. This applies to both the `cpu` NodePool and the `gpu` NodePool (both reference the shared `default` EC2NodeClass). New nodes will have 100GB root volumes; existing small-volume nodes get replaced naturally as Karpenter evicts them.

**Fix 2 — Ray cluster head image:** `ray-cluster.yaml` was using `rayproject/ray:2.9.0-py310-gpu` for the head node. The head does no GPU work (just cluster coordination). Changed to `rayproject/ray:2.9.0-py310` (CPU variant, ~2GB vs 15GB). Eliminates disk pressure on the head node and reduces cold-start time. GPU workers retain the GPU image.

**Root cause pattern:** Default EKS node volumes are too small for ML workloads. For any cluster running large model images, set `blockDeviceMappings` in the EC2NodeClass early — don't wait for disk pressure events.

---

## 2026-06-17 • Neo4j deployed via Helm + gp3 StorageClass

**Decision:** Deploy Neo4j community edition via the official `neo4j/neo4j` Helm chart (v2026.5.0). The API was passing its readiness check falsely — the Neo4j driver object was being constructed without actually handshaking, so graph queries would have failed at runtime.

**gp3 StorageClass:** Added `deploy/k8s/storageclass-gp3.yaml` using the EBS CSI provisioner (`ebs.csi.aws.com`). EKS ships with only `gp2` by default; `gp3` offers better baseline IOPS (3000 vs 100-3000 burst) at lower cost. All new persistent volumes (Neo4j, and Qdrant on next reinstall) should use `gp3`. Existing Qdrant PVCs remain on `gp2` — no migration needed for a dev cluster with no production data.

**values.yaml rewrite:** The repo's original `values.yaml` was written for an older chart version — `neo4j.core.numberOfServers` and `volumes.data.mode: "default"` are not valid in the 2026.x chart. Updated to the current schema: `neo4j.password`, `neo4j.resources`, `volumes.data.mode: "dynamic"` with `dynamic.storageClassName: "gp3"`.

**LoadBalancer disabled:** The chart creates a public-facing LoadBalancer service by default. With `password: "password"` that's a security risk. Disabled via `services.neo4j.enabled: false` — API accesses Neo4j via the internal ClusterIP `neo4j-cluster:7687`.

**Verified:** `cypher-shell -a bolt://neo4j-cluster:7687 "RETURN 1 AS ok"` returns `1` from within the cluster.

---

## 2026-06-17 • Aurora rag_db creation + DATABASE_URL fix

**Fix:** Aurora cluster was created without `database_name = "rag_db"` in Terraform, defaulting to `postgres`. The `app-env-secret` had `DATABASE_URL` pointing to the `postgres` database with `changeme` password. Fixed by: (1) running a one-shot `kubectl run` pod with `postgres:15` image to create the `rag_db` database, (2) patching `app-env-secret` with the real Aurora master password from Secrets Manager, (3) restarting the API deployment. API is now 2/2 Running and Ready.

**Note on Secrets Manager password:** Aurora `ManageMasterUserPassword=true` means the password is auto-rotated and stored in Secrets Manager (`rds!cluster-*`). Never store it in `.env` or Terraform vars — always retrieve with `aws secretsmanager get-secret-value`.

---

## 2026-06-16 • GPU in-cluster serving rework (option B: Llama-3-8B)

**Decision:** Fix the broken K8s RayService GPU-serving path using a **custom Ray image** for code distribution (not `runtime_env.working_dir`). Rationale: GPU workers scale from zero on churning spot nodes — baking vllm/torch/app-code into the image makes a fresh worker serving-ready in seconds vs pip-installing ~3GB on every cold start. Also matches the existing API ECR workflow, and for KubeRay `working_dir` would require hosting a remote code zip anyway.

**File changes (not yet built/applied — bash was down during this work):**
- `vllm_engine.py` / `embedding_engine.py`: added an env-driven `app` entrypoint (`build_app()`) so the RayService `import_path: ...:app` resolves (previously referenced a non-existent `app` symbol — only `_app`/`llm_app` existed). Config now read from env vars (MODEL_ID, LLM_MAX_MODEL_LEN, etc.) so the manifest actually configures the deployment (before, `.bind()` with no args silently used the 70B default).
- `vllm_engine.py`: default model changed 70B → `NousResearch/Meta-Llama-3-8B-Instruct` (ungated mirror, no HF token needed; fits one A10G 24GB at fp16, TP=1).
- `services/models/Dockerfile` (new): custom Ray GPU image `FROM rayproject/ray:2.9.0-py310-gpu` + vllm 0.4.2 + sentence-transformers + app code. **Version matrix flagged for validation** — Llama-3 needs vLLM ≥0.4.0 which is newer than Ray 2.9.0's era; if conflicts arise, bump Ray base AND rayVersion in manifests in lockstep.
- `ray-serve-llm.yaml` / `ray-serve-embed.yaml`: point head+worker at the custom ECR image, drop the runtime_env `pip` (baked in), wire config via env_vars, fixed LLM deployment name (`VLLMDeployment`→`LLMDeployment` to match the class), removed dead `user_config`.
- `deploy/k8s/nvidia-device-plugin.yaml` (new): device plugin DaemonSet (tolerates GPU taint, targets `instance-category in [g,p]`) — required for GPU nodes to advertise `nvidia.com/gpu`.

**Sizing:** worker requests cpu 4 / mem 32Gi / 1 GPU → lands on g5.2xlarge (8 vCPU, 32GB, 1× A10G), within the 16-vCPU on-demand (or 8-vCPU spot) G quota.

**Pending (needs bash):** build+push the ray-serve image, `kubectl apply` the device plugin + RayServices, validate the vLLM/Ray version combo and 8B GPU fit on first run.

---

## 2026-06-16 • EBS CSI driver, app-env-secret, API image fix

**EBS CSI driver:** Added `infra/terraform/ebs-csi.tf` with an IRSA role and `aws_eks_addon` resource. Installing the addon immediately unblocked all three Qdrant PVCs that had been Pending for 75 minutes. Root cause: EKS 1.23+ requires the EBS CSI driver — the in-tree `kubernetes.io/aws-ebs` provisioner is non-functional without it.

**app-env-secret:** Created Kubernetes Secret `app-env-secret` from values in `.env` (LLM_API_KEY, NEO4J_PASSWORD) and Terraform outputs (REDIS_URL, S3_BUCKET_NAME). DATABASE_URL uses Aurora endpoint with `changeme` password and `postgres` DB (Aurora default — no explicit `database_name` was set in Terraform; local `.env` says `rag_db` which doesn't exist on Aurora yet).

**API image fix (chain):**
1. `langchain==0.1.5` + `langgraph==0.0.21` crash on Python 3.12 — `langsmith` calls `ForwardRef._evaluate()` without the now-required `recursive_guard` kwarg. Bumped to `langchain==0.3.7` + `langgraph==0.2.56` (only import is `from langgraph.graph import StateGraph, END`, unchanged).
2. `langchain 0.3.7` requires `pydantic>=2.7.4`; bumped `pydantic 2.6.0→2.9.2`, `pydantic-settings 2.1.0→2.5.2`.
3. Removed `sentence-transformers`/`transformers` from API requirements — they (and torch + full CUDA stack, ~7GB) are only used by `app/models/` (Ray Serve, runs in `rayproject/ray` image). The FastAPI app calls remote LLM/embed endpoints via httpx. Image dropped from ~7GB to 669MB.
4. `imagePullPolicy: IfNotPresent` + reused tag `f25f720` would serve the cached broken image — pinned the deployment to the new digest (`@sha256:558da54b...`) to force the new image.

**Redis TLS fix:** ElastiCache `rag-redis-prod` has `TransitEncryptionEnabled=true`, but `app-env-secret` had `REDIS_URL=redis://...` (plaintext). The readiness probe's `await redis.ping()` hung against the TLS-only endpoint, exceeding the 1s probe timeout → pod never became Ready. Patched secret to `rediss://...` (redis-py `from_url` auto-enables SSL for that scheme). API then rolled out 2/2 Ready.

**Outcome:** API 2/2 Running & Ready, Qdrant 3/3 Running, kuberay-operator Running. Remaining: Ray cluster (heads churning, CPU worker pinned to c6i can't schedule).

---

## 2026-06-15 • Terraform fixes for initial AWS deploy

**Decision:** Bumped EKS to 1.30 and Aurora PostgreSQL to 15.17, replaced `dynamodb_table` backend lock with `use_lockfile = true`, fixed IAM module subpath (singular → plural `iam-role-for-service-accounts-eks`), added explicit S3 lifecycle filter.

**Reasoning:** EKS 1.29 and Aurora 15.3 are no longer available for new clusters in us-east-1 — AWS rejects `CreateCluster` and `CreateDBCluster` for those versions. The DynamoDB lock table param is deprecated in favour of S3's native lockfile. The IAM module subpath was a typo vs the actual downloaded module directory name.

---

## 2026-06-15 • Karpenter migration: Provisioner → NodePool/EC2NodeClass

**Decision:** Rewrote `infra/karpenter/provisioner-cpu.yaml` and `provisioner-gpu.yaml` from old `karpenter.sh/v1beta1 Provisioner` to current `karpenter.sh/v1 NodePool` + `karpenter.k8s.aws/v1 EC2NodeClass`. Added `infra/terraform/karpenter.tf` with IRSA-based controller role and a dedicated node IAM role.

**Reasoning:** Karpenter dropped the `Provisioner` API. The old manifests would fail with "no matches for kind Provisioner" even after CRD install. Current API separates node configuration (EC2NodeClass, including AMI, subnets, SGs, instance profile) from scheduling policy (NodePool). IRSA is required for the controller to call EC2/EKS/SQS APIs without inheriting the broad node instance role — which is exactly why the controller was CrashLooping on first install (no IAM identity, just the node role via IMDS).

---

## 2026-06-16 • Karpenter IAM and aws-auth fully codified in Terraform

**Decision:** Codified all manually-applied Karpenter IAM and cluster auth config into Terraform so a fresh deploy never requires manual patching.

**Changes made:**
- Added `provider "kubernetes"` block to `main.tf` (was declared but not configured)
- Added `data "aws_caller_identity" "current"` to `main.tf` (needed for ARN interpolation)
- Added `manage_aws_auth_configmap = true` and `aws_auth_roles` to the EKS module in `eks.tf` — the EKS module now owns the `aws-auth` ConfigMap and writes both the system node group role and the Karpenter node role
- Removed the broken `aws_eks_access_entry` resource from `karpenter.tf` (requires `API` or `API_AND_CONFIG_MAP` auth mode; this cluster is `CONFIG_MAP` only)
- Used a plain ARN string in `aws_auth_roles` rather than `aws_iam_role.karpenter_node.arn` to avoid a circular dependency between the EKS module and the Karpenter node role

**Reasoning:** Three separate manual interventions were required on first deploy: (1) manually patching aws-auth, (2) attaching node role policies, (3) adding amiSelectorTerms. All three are now in Terraform. Policy attachments and amiSelectorTerms were already in the config; aws-auth was the last piece.

---

## 2026-06-16 • Karpenter fully operational

**Steps completed:**
1. `terraform apply -target` created `KarpenterControllerRole-rag-platform-cluster` and `KarpenterNodeRole-rag-platform-cluster` IAM roles.
2. `helm upgrade karpenter` (v1.13.0, `--reuse-values`) annotated the service account with the IRSA role ARN — fixed the CrashLoopBackOff.
3. Added `iam:ListInstanceProfiles` + instance profile management actions to the controller policy (second targeted apply) — these are needed for the `instanceprofile.garbagecollection` reconciler.
4. Added `amiSelectorTerms: alias: al2023@latest` to the EC2NodeClass (required field in Karpenter v1 CRD, `amiFamily` alone is not enough).
5. `kubectl apply -f infra/karpenter/` created the `default` EC2NodeClass and `cpu`/`gpu` NodePools — all `Ready: True`.
6. Karpenter immediately began provisioning nodes for pending pods (Ray cluster heads, Qdrant, API).

**Reasoning for `-target` approach:** `db_password` is a required root module variable but irrelevant to IAM resources. Targeted apply avoids requiring the real DB password and avoids touching live RDS/EKS while iterating on IAM permissions.

**Account vCPU quota note:** The account has a 32 vCPU limit for certain instance families. Karpenter's first attempt at `c6i.8xlarge` (32 vCPUs) hit `VcpuLimitExceeded`. It fell back to smaller types (`c6i.2xlarge`, `m6i.4xlarge`) which are within quota.
