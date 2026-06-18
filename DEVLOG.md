# DEVLOG — scalable-rag-pipeline

Chronological record of design decisions, architectural trade-offs, and non-obvious insights.
Focus: *why*, not *what* (git log has the what).

---


## 2026-06-18 • KubeRay serveService port bug — actual YAML fix

**Context:** The previous workaround patched `<name>-head-svc` services and pointed the API there. This session applied the proper structural fix so KubeRay creates the named `llm-service` and `embed-service` ClusterIP services itself.

**Root cause (confirmed):** KubeRay 1.0.0's serve service reconciler builds `ServiceSpec.Ports` from the head container's `containerPorts` list. Port 8000 (Ray Serve HTTP) was absent from both RayService manifests — only ports 6379 (Redis/GCS) and 8265 (Ray dashboard) were listed. The reconciler produced an empty `Ports` array, Kubernetes rejected it with `spec.ports: Required value`, and the named service was never created. The `serveService.spec.ports` stanza in the RayService YAML has no effect — the operator ignores it.

**Fix:** Added `- containerPort: 8000` to the head container's `ports` list in both `deploy/ray/ray-serve-llm.yaml` and `deploy/ray/ray-serve-embed.yaml`. Removed the `spec.ports` block from both `serveService` stanzas (redundant). Applied with `kubectl apply`.

**Result:** A rolling update was triggered for each RayService. New RayClusters with corrected head pod specs were created, and their stable `llm-service-head-svc` and `embed-service-head-svc` services now expose port 8000 automatically. KubeRay 1.0.0 still produces unusable named serve services with empty generated port lists, so the API continues to target the stable head services. The obsolete bootstrap step 14 service patch was removed.

---

## 2026-06-18 • Stale API image caused missing `chat_history` table

**Symptom:** Internal server error during retriever step with an `UndefinedTableError: relation "chat_history" does not exist` (visible in API pod logs after `kubectl logs`).

**BACKLOG diagnosis was wrong:** The backlog item attributed this to Aurora Serverless v2 cold-start timing racing `Base.metadata.create_all()`. That is a real risk, but it was not the cause here.

**Actual root cause:** The running API deployment was using image tag `f25f720` (`Add Streamlit chat UI` commit), which predates commit `f5bae9b` that added the `Base.metadata.create_all()` call to the FastAPI lifespan. The `create_all()` call simply did not exist in the deployed code — Aurora cold-start timing was irrelevant.

**How identified:** The pod was the right SHA (`f25f720`) for the "stale image" hypothesis; `git log` confirmed `f5bae9b` added the auto-create and landed after `f25f720`. There was no create-table call to race against.

**Fix:** Rebuilt the API image from HEAD (commit `f5bae9b` or later), pushed to ECR, updated the deployment image tag. Table created on next startup; all subsequent requests succeeded.

**Residual risk:** Aurora cold-start timing is still a real concern if the DB connection resolves before the cluster is active. The `chat_history` cold-start backlog item remains valid but is lower urgency than implied — observed once as a stale image misdiagnosis, not a confirmed timing bug.

---

## 2026-06-18 • Terraform Aurora `manage_master_user_password` alignment

**Problem:** Terraform `infra/terraform/rds.tf` defined a manual `aws_secretsmanager_secret` + `aws_secretsmanager_secret_version` for the DB password, and a `db_password` variable. The live Aurora cluster was created with `ManageMasterUserPassword=true`, meaning Aurora auto-rotates the password in Secrets Manager under an RDS-owned secret (`rds!cluster-*`). Terraform's manually-created secret had `password = "password"` (placeholder), which was what `app-env-secret` was initially pointing at — causing `InvalidPasswordError` on every connection attempt.

**Diagnosis path:** `aws rds describe-db-clusters --db-cluster-identifier rag-platform-aurora` showed `ManagedMasterUserPassword: true`. AWS rejected a `ModifyDBCluster` attempt to set a manual password with `InvalidParameterValue: You can't specify MasterUserPassword for an instance with ManageMasterUserPassword enabled`.

**Fix:**
- Added `manage_master_user_password = true` to both `module "aurora"` and `aws_db_instance.postgres` in `rds.tf`.
- Removed `master_password = var.db_password` (Aurora) and `password = var.db_password` (RDS) from the resource configs.
- Removed the manual `aws_secretsmanager_secret` and `aws_secretsmanager_secret_version` resources entirely.
- Added a `locals` block with `db_secret_arn` that reads from `module.aurora[0].cluster_master_user_secret[0].secret_arn` (or the RDS equivalent).
- Removed the `db_password` variable from `variables.tf` and the corresponding line from `terraform.tfvars`.
- Added a `db_secret_arn` output to `outputs.tf`.

**Updated `bootstrap_cluster.sh`:** Added `DB_SECRET_ARN=$(terraform ... output -raw db_secret_arn)` to the USAGE heredoc so operators can retrieve the real password with `aws secretsmanager get-secret-value --secret-id $DB_SECRET_ARN`.

**Pending:** `terraform apply` needs to run to remove the manual secret resources from AWS state and register the new locals/output. Targeted apply is safe: only the manual secret resources and outputs change; the Aurora cluster itself is unaffected.

---

## 2026-06-18 • Image build pipeline — S3 staging + ECR sync scripts

**Problem:** The previous `scripts/build_push_api.sh` pushed directly from the build machine to ECR. For large images (~15GB GPU image) this is slow, unreliable over a flaky uplink, and not restartable. Resuming a failed push means re-pushing all layers.

**New approach:** Two-script pipeline:
1. `scripts/build_push_image.sh <api|ray>` — builds the Docker image locally and streams it to S3 as a gzipped tar: `docker save ... | gzip | aws s3 cp - s3://<bucket>/images/<ecr-repo>/<git-sha>.tar.gz`. The S3 upload uses AWS multipart under the hood and is resumable. Does not touch ECR.
2. `scripts/sync_s3_to_ecr.sh <api|ray> <git-sha>` — runs on demand (or from any machine with ECR access): `aws s3 cp <s3-uri> - | gunzip | docker load` then retags and pushes to ECR. Lists available SHAs if no SHA is given.

**Routing:**
- `api` → `services/api/Dockerfile`, ECR repo `services/api`
- `ray` → `services/models/Dockerfile`, ECR repo `services/ray-serve`

**Why S3 intermediate:** Decouples the slow build-machine upload from the ECR push. A CI/CD agent with high-bandwidth egress to AWS can run `sync_s3_to_ecr.sh` independently. Also creates a versioned image archive in S3 for rollback without re-building.

---

## 2026-06-18 • KubeRay serveService port bug and head-svc workaround

**Problem:** The API returned `An internal error occurred` at the retriever step. Both the planner's LLM call and the retriever's embed call failed with `[Errno -2] Name or service not known`. The DNS names `llm-service` and `embed-service` — specified in the `serveService.metadata.name` field of the RayService manifests — never resolved.

**Root cause:** KubeRay operator v1.0.0 has a bug in `rayservice_controller.go:218`: when reconciling the serve ClusterIP service it builds the `ServiceSpec` with an empty `Ports` array, even when `serveService.spec.ports` is provided. Kubernetes rejects the resulting object with `spec.ports: Required value`. This reconcile error repeats every cycle; the named serve services are never created.

**Workaround:** KubeRay *does* correctly create a stable `<name>-head-svc` ClusterIP service for each RayService (e.g. `llm-service-head-svc`). Ray Serve's HTTP proxy runs on port 8000 of the head pod, which carries `ray.io/serve=true`. We patch port 8000 onto the head services and point the API at `llm-service-head-svc:8000` and `embed-service-head-svc:8000` instead.

**Why not fix the serveService YAML?** The operator ignores the port spec entirely — the newSvc log shows `Ports:[]ServicePort{}` regardless of what the YAML contains. The only structural fix would be upgrading KubeRay, which is a larger change.

**Changes:** `deploy/helm/api/values.yaml` (endpoints), `scripts/bootstrap_cluster.sh` step 14 (patch head services on deploy), comment in RayService YAMLs.

---

## 2026-06-18 • GPU NodePool narrowed to g5.2xlarge on-demand; Ray worker memory resized to 24Gi

**Problem (capacity):** The original GPU NodePool used `instance-category In [g,p] + instance-generation Gt [4]` with spot + on-demand. On deploy, Karpenter selected `g5.4xlarge` (16 vCPU), which consumed the entire 16-vCPU G-instance quota (L-DB2E81BA) in one shot — the second GPU worker had no capacity. Switching to two `g5.2xlarge` (8 vCPU each) fits within quota.

**Problem (memory):** Workers were requesting 32Gi but `g5.2xlarge` has only ~28.3 GiB allocatable after EKS node system reservations (~3.6 GiB). Requests exceeded node capacity; workers stayed Pending.

**Fix:** Pinned NodePool to `instance-family: g5` + `instance-size: 2xlarge` + `capacity-type: on-demand`. Reduced Ray worker memory from 32Gi → 24Gi (leaves ~3.6 GiB headroom above the 24.1 GiB consumed by worker + daemonsets). Both services — LLM and embedding — got separate g5.2xlarge nodes.

**Trade-off:** Losing the spot fallback increases cost (~$1/hr for both nodes) but eliminates interruption risk while GPU quota is at 16 vCPU. Spot can be re-added once quota is raised to ≥32.

---

## 2026-06-18 • Model weight S3 caching to eliminate HuggingFace NAT transfer cost

**Problem:** Each GPU node cold start downloaded ~18 GB from HuggingFace through the NAT Gateway — ~16 GB for `NousResearch/Meta-Llama-3-8B-Instruct` and ~2.2 GB for `BAAI/bge-m3`. At $0.045/GB that's ~$0.90 per cold start, compounding with Karpenter spot churn.

**Solution:** Weights are uploaded to an S3 bucket once via `scripts/cache_models_s3.py` (uses `huggingface_hub.snapshot_download` + `aws s3 sync`). At pod startup, `_resolve_model_path()` in both `vllm_engine.py` and `embedding_engine.py` checks for `MODEL_CACHE_BUCKET` env var; if set, it syncs the model directory from S3 to `/model-cache/<org>--<name>/` on the node's ephemeral disk before loading. S3 traffic routes via the free Gateway VPC endpoint (see entry below) — $0 data transfer.

**Fallback:** If `MODEL_CACHE_BUCKET` is unset (local dev), the function returns the original HuggingFace model ID unchanged — no behaviour change for local runs.

**Infrastructure:** New `rag-platform-models-prod-7649` S3 bucket (`force_destroy = true`, no versioning). S3 read policy attached to the Karpenter node IAM role so all GPU workers can pull without per-pod IRSA. `MODEL_CACHE_BUCKET` wired into both RayService manifests.

**Prerequisite for each new cluster deploy:** run `scripts/cache_models_s3.py` after `terraform apply` and before deploying RayServices, otherwise pods fall back to HuggingFace.

---

## 2026-06-18 • VPC endpoints to cut NAT Gateway data transfer and fixed costs

**Problem:** Three NAT Gateways (one per AZ) cost ~$97/month fixed. ECR image pulls for the 15 GB GPU image and all AWS API calls (IRSA, Secrets Manager) went through NAT at $0.045/GB, making the NAT the dominant cost line — more than the EC2 instances.

**Fix:**
- `single_nat_gateway = true`: drops fixed NAT cost from ~$97 to ~$32/month. Single-AZ failure risk is acceptable for dev/test.
- **S3 Gateway endpoint** (free): routes all S3 traffic — ECR layer storage, pip caches, model weight downloads — over the AWS backbone, bypassing NAT entirely.
- **ECR interface endpoints** (`ecr.dkr` + `ecr.api`): image pulls from ECR never touch the NAT Gateway. Cost: ~$43/month for both endpoints across 3 AZs, but break-even on the first couple of 15 GB image pulls.
- **STS interface endpoint**: IRSA token exchanges stay off NAT.
- **Secrets Manager interface endpoint**: Aurora password fetches during bootstrap stay off NAT.

**Remaining NAT traffic:** pip installs, HuggingFace downloads (addressed by S3 model cache above), any other public internet access.

**All five resources share one security group** (`vpc_endpoints` SG) that allows inbound HTTPS from within the VPC CIDR only.

---

## 2026-06-17 • Ray Job runtime dependencies & NumPy 2.0 / PyArrow conflict

**Problem:** Submitting the S3 ingestion job via `ray job submit` failed with `ImportError: numpy.core.multiarray failed to import` inside the `JobSupervisor` initialization. This happened because the job's `runtime_env` pip list originally included `unstructured[pdf,docx]`, which transitively pulled in the latest NumPy 2.0 version. Since the system-wide pre-installed `pyarrow 12.0.1` on the Ray head node is compiled against NumPy 1.x, the active virtual environment's NumPy 2.0 broke `pyarrow`'s compiled C extensions.

**Backtracking compilation failure:** Attempting to pin `numpy==1.26.2` alongside `unstructured[pdf,docx]` caused pip to backtrack to ancient versions of `numba` (like `0.22.0`) to satisfy constraints from `unstructured-inference` (a layout model parser dependency). This resulted in compilation failures on the cluster due to deprecated `distutils` support in newer setup tools.

**Solution:** 
1. Pinned `numpy==1.26.2` in the runtime environment to maintain binary compatibility with system `pyarrow`.
2. Replaced the heavy `unstructured[pdf,docx]` extra with the base `unstructured==0.11.0` library, which does not pull in `unstructured-inference`, `numba`, or heavy ML models.
3. Manually declared the lightweight libraries required for `strategy="fast"` PDF/document parsing: `pdfminer.six`, `pdf2image`, `pypdf`, `pypdfium2`, `pi-heif`, `python-docx`, `python-pptx`.
4. Codified these changes in [pipelines/jobs/ray_job.yaml](file:///home/som/code/scalable-rag-pipeline/pipelines/jobs/ray_job.yaml) and [pipelines/jobs/s3_event_handler.py](file:///home/som/code/scalable-rag-pipeline/pipelines/jobs/s3_event_handler.py).
5. Documented that `--runtime-env-json` should be used instead of `--runtime-env` for raw job specs containing root-level keys like `entrypoint` to avoid parsing failures.

**Result:** Ingestion jobs now start and run with an extremely lightweight virtual environment, avoiding heavy compile phases, C-extensions issues, and dependency version backtracking.

---

## 2026-06-17 • Ray Serve proxy, route paths, and BGE-M3 schema

**Proxy crash:** Ray 2.9.0's Serve HTTP proxy expects `starlette.middleware.Middleware.options`, but the Starlette version in the custom image exposes `kwargs`. This crashed every Serve proxy actor even while the LLM/embed deployments reported HEALTHY. The Ray Serve image now patches `ray/serve/_private/proxy.py` at build time (`middleware.options` → `middleware.kwargs`), deletes stale `__pycache__`, and validates the patched source during Docker build. Patched image: `services/ray-serve:4c46e7c-proxy-fix2`.

**Route paths:** Ray Serve ingress mounts the FastAPI apps under route prefixes. The actual API endpoints are `/llm/chat/completions` and `/embed/embeddings`, not just `/llm` and `/embed`. Updated API defaults, Helm values, bootstrap, and README snippets accordingly.

**Embedding dimension/schema:** The EKS embedding service uses BGE-M3, which emits 1024-dimensional vectors. The old semantic cache default was 768 from local `nomic-embed-text`, causing Qdrant dimension errors. Live Qdrant collections were recreated/provisioned with size 1024, and bootstrap now creates missing `rag_collection`, `semantic_cache`, and Neo4j `entity_index` before the API starts. Existing Qdrant collection dimensions are immutable; wrong-dimension collections must be deleted and recreated before re-ingestion.

**API DB schema:** `chat_history` is now created during FastAPI lifespan startup via SQLAlchemy `Base.metadata.create_all()`, removing the manual Aurora table creation step that was needed during the first E2E test.

**Rollout observation:** With only two available GPU slots, KubeRay could not surge both new RayService workers while old workers occupied the GPUs. The live rollout required brief inference downtime by deleting old worker pods after the new heads/proxies were healthy. KubeRay later reported both RayServices `Running` with the patched clusters active.

**Validation:** End-to-end `/api/v1/chat/stream` now completes planner → retriever → responder through the API pod against the EKS RayServices.

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
