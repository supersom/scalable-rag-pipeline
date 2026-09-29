# EKS Cluster Runbook — scalable-rag-pipeline

How to bring up the full stack from zero, diagnose common failures, and tear it down.

---

## Architecture summary

```
Internet → (ALB/Ingress) → FastAPI (EKS, ECR image)
                         → Qdrant (StatefulSet, gp3 PVCs)
                         → Neo4j  (StatefulSet, gp3 PVC)
                         → Aurora PostgreSQL (Serverless v2, Secrets Manager password)
                         → Redis (EKS primary + replica, Helm)
                         → Ray Serve on g5 GPU nodes (Karpenter on-demand)
                              ├── llm-service  (vLLM + Llama-3-8B, port 8000 /llm/chat/completions)
                              └── embed-service (BGE-M3,           port 8000 /embed/embeddings)
```

Node pools:
- `system` — 2× m6i.large (managed, always on), runs CoreDNS + Karpenter + kuberay-operator
- `cpu` (Karpenter) — m6i/c6i/r6i spot, 100GB root; runs API, Qdrant, Neo4j, Ray heads
- `gpu` (Karpenter) — g5 spot/on-demand, 100GB root; runs Ray GPU workers

---

## First-time bring-up

### 1. Terraform

```bash
cd infra/terraform
terraform init
terraform apply   # supply db_password when prompted
```

Key outputs needed for bootstrap:
```bash
terraform output -raw db_endpoint                # DB_ENDPOINT (Aurora or RDS depending on db_tier)
terraform output -raw s3_documents_bucket_name   # S3_BUCKET
terraform output -raw s3_models_bucket_name      # MODEL_CACHE_BUCKET (for scripts/cache_models_s3.py)
```

> **Database tier:** controlled by `db_tier` in `terraform.tfvars`. Default is `"rds"` (db.t3.micro, ~$15/month). Set `db_tier = "aurora"` for production (Serverless v2, ~$86/month minimum, multi-AZ HA). Switching tiers on a live cluster destroys and recreates the instance — migrate chat history first if it matters.

> **Note on database password:** supplied as `db_password` at `terraform apply`. Store it in `terraform.tfvars` (gitignored) — never hardcode it.

### 2. Build and push images

```bash
# Authenticate to ECR
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com

# API image
GIT_SHA=$(git rev-parse --short HEAD)
docker build -t ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/api:${GIT_SHA} \
  -f services/api/Dockerfile .
docker push ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/api:${GIT_SHA}

# Ray Serve GPU image (~28GB, CUDA + vLLM + app code)
docker build -t ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:${GIT_SHA} \
  -f services/models/Dockerfile .
docker push ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:${GIT_SHA}
```

The Ray Serve image uses Docker layer caching — only the `COPY services` layer changes on code edits, so rebuilds after the first are usually quick. The image build also validates the Ray 2.9.0 Serve proxy patch needed for the pinned Starlette version.

### 2.1 Ingestion image (build → S3 → ECR)

The ingestion image goes through a build-to-S3-then-promote-to-ECR relay instead of a direct `docker push` — useful on a slow uplink, and it's the flow needed the first time a fresh/recreated `services/ingestion` ECR repo has no images in it yet:

```bash
GIT_SHA=$(git rev-parse --short HEAD)

# 1. Build locally, docker save + gzip, upload to
#    s3://<models-bucket>/images/services/ingestion/<sha>.tar.gz
bash scripts/build_push_image.sh ingestion

# 2. Download from S3, docker load, retag for ECR, push
bash scripts/sync_s3_to_ecr.sh ingestion "${GIT_SHA}"
```

Verify the push landed:

```bash
aws ecr describe-images --repository-name services/ingestion \
  --query 'imageDetails[].imageTags' --output text
```

`build_push_image.sh ingestion <existing-git-sha>` (second argument) instead pulls that exact tag from ECR and re-exports it to S3 without rebuilding — for backing up a known-good image before an unrelated change.

**Before building:** confirm `git log --oneline -- pipelines/ingestion services/ingestion` includes `ace240e` (pre-downloads the NLTK corpora `unstructured`'s PDF parser needs). Without it, PDF ingestion parses to zero chunks silently — the job still reports success (see DEVLOG).

### 2.5 Populate S3 model weight cache (one-time per bucket)

GPU node cold starts pull model weights from S3 via the free Gateway VPC endpoint rather than HuggingFace via NAT, saving ~$0.90/node in data transfer. The bucket must be populated before RayServices are applied.

```bash
pip install huggingface_hub
python scripts/cache_models_s3.py   # downloads ~18 GB, uploads to S3 (~5 min)
```

Re-running is safe — `aws s3 sync` skips files already present. If the bucket is empty when a GPU worker starts, `_resolve_model_path()` falls back to downloading from HuggingFace automatically (slower, costs NAT transfer).

### 3. Bootstrap the cluster

```bash
export API_IMAGE_TAG=${GIT_SHA}
export RAY_IMAGE_TAG=${GIT_SHA}
export DB_SECRET_ARN=<from terraform output>
export DB_ENDPOINT=<from terraform output db_endpoint>
export S3_BUCKET=<from terraform output>

bash scripts/bootstrap_cluster.sh
```

The script runs steps 1–14 in order:
1. kubeconfig update
2. Karpenter controller Helm install (installs CRDs + controller; derives role ARN + cluster endpoint from terraform output)
3. Karpenter NodePool/EC2NodeClass apply
4. gp3 StorageClass
5. NVIDIA device plugin DaemonSet
6. Helm repos
7. KubeRay operator
8. Qdrant (3-replica StatefulSet)
9. Neo4j (community, 100GB gp3 PVC)
10. Data-store schema: create missing Qdrant `rag_collection` + `semantic_cache` at 1024 dimensions, and Neo4j `entity_index`
11. `app-env-secret` with all env vars
12. API Helm chart
13. RayService for LLM + embeddings
14. Queue-backed ingestion: service account, CPU RayCluster, and SQS consumer

The API also creates its `chat_history` table on startup via SQLAlchemy `Base.metadata.create_all()`, so a fresh Aurora database does not need a manual table creation step.

**KubeRay `serveService` port bug** — KubeRay operator v1.0.0 still creates the named serve services (`llm-service` and `embed-service`) with an empty port list, because `serveService.spec.ports` is ignored. Both RayService manifests declare `containerPort: 8000`, which correctly adds port 8000 to the stable `llm-service-head-svc` and `embed-service-head-svc` services. The API targets those head services directly. No bootstrap service patch is required.

### Automatic document ingestion

Terraform connects the documents bucket to an SQS queue. Uploading a supported
file after that notification is applied causes `deployment/ingestion-worker` to
submit a Ray Data job to the CPU-only `ingestion-ray` cluster. The job parses and
chunks the file, calls the embedding and LLM RayServices, then writes Qdrant and
Neo4j. Existing bucket objects are not retroactively notified; copy or re-upload
them to enqueue ingestion.

For day-2 deployment or updates, run the standalone idempotent deployer rather
than rerunning the complete cluster bootstrap:

```bash
bash scripts/deploy_ingestion.sh
```

It derives the latest `services/ingestion` ECR tag and the Terraform queue and
IRSA outputs unless those values are exported explicitly.

```bash
kubectl get raycluster ingestion-ray
kubectl logs -f deploy/ingestion-worker
kubectl port-forward svc/ingestion-ray-head-svc 8265:8265
ray job list --address http://localhost:8265
```

### 4. Wait for GPU inference

```bash
# Watch Karpenter provision the GPU node (g5.2xlarge, ~2 min)
kubectl get nodeclaims -w

# Watch LLM load (~5 min after GPU node joins)
kubectl get rayservice llm-service -w

# All services up when:
kubectl get rayservice
# NAME           STATUS
# llm-service    Running
# embed-service  Running
```

### 5. End-to-end test

```bash
kubectl port-forward svc/api-service 8000:80 &
TOKEN=$(kubectl exec deploy/api -- python3 -c "from jose import jwt; import time; print(jwt.encode({'sub':'test','role':'admin','exp':int(time.time())+3600}, 'i_need_to_change_this', algorithm='HS256'))")
curl -s -N -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  http://localhost:8000/api/v1/chat/stream \
  -d '{"message":"hello","session_id":"runbook-smoke"}'
```

---

## Day-2 operations

### Update Ray Serve image after code change

```bash
GIT_SHA=$(git rev-parse --short HEAD)  # commit first
docker build -t ${ECR_BASE}/services/ray-serve:${GIT_SHA} -f services/models/Dockerfile .
docker push ${ECR_BASE}/services/ray-serve:${GIT_SHA}

# Update tag in both manifests
sed -i "s|ray-serve:[^ ]*|ray-serve:${GIT_SHA}|g" \
  deploy/ray/ray-serve-llm.yaml deploy/ray/ray-serve-embed.yaml

# Apply (do NOT delete — use apply only to avoid FailedToUpdateService)
kubectl apply -f deploy/ray/ray-serve-llm.yaml
kubectl apply -f deploy/ray/ray-serve-embed.yaml

# If GPU quota prevents a surge rollout, the new workers can remain Pending while
# old workers occupy the only GPUs. Delete old active worker pods only after the
# new heads and proxies are healthy; this causes brief inference downtime.
```

> **Important:** always `kubectl apply`, never `kubectl delete && kubectl apply` for RayService. Deleting the live service during update causes the KubeRay FSM to enter `FailedToUpdateService` because the old cluster it tries to clean up is already gone.

### Force-reconcile a stuck RayService

If `kubectl get rayservice` shows `FailedToUpdateService`:

```bash
# Nudge the controller — it retries immediately
kubectl annotate rayservice llm-service force-reconcile="$(date +%s)" --overwrite
kubectl annotate rayservice embed-service force-reconcile="$(date +%s)" --overwrite

# If the active RayService changed but the ClusterIP service selector is stale,
# patch the selector to the active cluster name reported by kubectl get rayservice -o yaml.
kubectl patch svc llm-service -p '{"spec":{"selector":{"ray.io/cluster":"<active-llm-raycluster>","ray.io/node-type":"head"}}}'
kubectl patch svc embed-service -p '{"spec":{"selector":{"ray.io/cluster":"<active-embed-raycluster>","ray.io/node-type":"head"}}}'

# Delete and re-apply only as a last resort; it accepts downtime and can leave
# old pods terminating while KubeRay reconciles.
```

### Recover stuck Terminating pods (dead node)

Pods on an unreachable node hang in `Terminating` indefinitely — the kubelet can't acknowledge the SIGTERM.

```bash
# Identify stuck pods
kubectl get pods -A | grep Terminating

# Find the node
kubectl get pod <pod-name> -o jsonpath='{.spec.nodeName}'

# Force-delete the pods
kubectl delete pod <pod-name> --force --grace-period=0

# If the EC2 instance is still alive but kubelet is dead, delete its NodeClaim
# to terminate the instance and release any EBS volumes it holds:
kubectl get nodeclaims   # find the claim for the dead node
kubectl delete nodeclaim <name>
```

### Karpenter logs

```bash
# Follow live (both controller replicas interleaved)
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter -f

# Single pod — cleaner if you want no interleaving
kubectl logs -n kube-system karpenter-d8756bb89-j6qrv -f

# Last 100 lines without following
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter --tail=100

# Filter to provisioning decisions only
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter -f \
  | grep -E 'launched|disrupted|failed|nodeclaim|NodeClaim'

# Pretty-print JSON logs
kubectl logs -n kube-system -l app.kubernetes.io/name=karpenter -f \
  | jq -r '[.time, .level, .msg] | @tsv'
```

### Check Ray cluster health

```bash
# From inside the head pod:
kubectl exec <llm-head-pod> -- ray status
# Shows: active nodes, pending nodes, resource demands, recent failures

# Check serve deployment status:
kubectl get rayservice llm-service -o jsonpath=\
'{.status.activeServiceStatus.applicationStatuses.llama3.serveDeploymentStatuses.LLMDeployment}'
```

### Update Aurora password in secret

Aurora rotates the password automatically. If the API starts 500ing on DB connections:

```bash
DB_SECRET_ARN=$(aws rds describe-db-clusters \
  --db-cluster-identifier rag-platform-cluster-postgres \
  --query 'DBClusters[0].MasterUserSecret.SecretArn' --output text)

DB_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$DB_SECRET_ARN" \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

kubectl patch secret app-env-secret -p \
  "{\"data\":{\"DATABASE_URL\":\"$(echo -n "postgresql://ragadmin:${DB_PASSWORD}@${DB_ENDPOINT}/rag_db" | base64 -w0)\"}}"
kubectl rollout restart deployment/api
```

### Retrieve JWT secret key

```bash
kubectl get secret app-env-secret -o jsonpath='{.data.JWT_SECRET_KEY}' | base64 -d
```

### Generate a JWT token

Retrieve the key first (see above), then:

```bash
JWT_SECRET_KEY=$(kubectl get secret app-env-secret -o jsonpath='{.data.JWT_SECRET_KEY}' | base64 -d)
python3 - <<EOF
import jwt, time
token = jwt.encode(
    {"sub": "dev", "exp": int(time.time()) + 86400},
    "$JWT_SECRET_KEY",
    algorithm="HS256",
)
print(token)
EOF
```

Set for curl:

```bash
TOKEN=$(python3 - <<EOF
import jwt, time
token = jwt.encode(
    {"sub": "dev", "exp": int(time.time()) + 86400},
    "$JWT_SECRET_KEY",
    algorithm="HS256",
)
print(token)
EOF
)
```

Note: tokens expire. If you see `{"detail":"Could not validate credentials"}` on a token that previously worked, it has likely expired — regenerate it.

---

## Observability

### Port-forward dashboards

All dashboards run inside the cluster with no external ingress. Forward locally:

```bash
# Ray dashboard — LLM service (head pod, port 8265)
kubectl port-forward svc/llm-service-head-svc 8265:8265
# open http://localhost:8265

# Ray dashboard — embed service
kubectl port-forward svc/embed-service-head-svc 8266:8265
# open http://localhost:8266

# Ray dashboard — ingestion cluster
kubectl port-forward svc/ingestion-ray-head-svc 8267:8265
# open http://localhost:8267

# Grafana (once kube-prometheus-stack is installed)
kubectl port-forward -n monitoring svc/kube-prometheus-stack-grafana 3000:80
# open http://localhost:3000  (default creds: admin / prom-operator)
```

Run all in background:

```bash
kubectl port-forward svc/llm-service-head-svc 8265:8265 &
kubectl port-forward svc/embed-service-head-svc 8266:8265 &
kubectl port-forward svc/ingestion-ray-head-svc 8267:8265 &
# kill with: kill %1 %2 %3
```

### Install Prometheus + Grafana (kube-prometheus-stack)

```bash
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo update

helm install kube-prometheus-stack prometheus-community/kube-prometheus-stack \
  --namespace monitoring --create-namespace \
  --set prometheus.prometheusSpec.podMonitorSelectorNilUsesHelmValues=false \
  --set prometheus.prometheusSpec.serviceMonitorSelectorNilUsesHelmValues=false \
  --set grafana.adminPassword=changeme
```

The two `SelectorNilUsesHelmValues=false` flags make Prometheus pick up PodMonitors and ServiceMonitors from any namespace, not just those created by the chart itself. Without them the Ray PodMonitors below are silently ignored.

Then apply the Ray scrape targets:

```bash
kubectl apply -f deploy/monitoring/ray-service-monitors.yaml
```

This creates two PodMonitors (`ray-heads`, `ray-workers`) in the `monitoring` namespace. They match pods by `ray.io/node-type` label across all Ray clusters in the `default` namespace and scrape port `8080` (named `metrics` by KubeRay). Metrics include active nodes, pending tasks, object store usage, GPU utilisation, and Serve request latency.

Verify Prometheus is scraping:

```bash
# Port-forward Prometheus UI
kubectl port-forward -n monitoring svc/kube-prometheus-stack-prometheus 9090:9090
# open http://localhost:9090/targets  — look for ray-heads and ray-workers
```

Useful Ray metric names in Grafana:
- `ray_cluster_active_nodes` — nodes per cluster
- `ray_object_store_memory` — object store pressure
- `ray_serve_num_ongoing_requests` — per-deployment request queue depth
- `ray_serve_deployment_replica_starts_total` — cold start events
- `ray_node_gpus_utilization` — GPU % per worker

### Scripted health check

```bash
bash scripts/check_ray_health.sh                   # full check: CRD + ray status + HTTP probes
bash scripts/check_ray_health.sh --endpoints-only  # HTTP probes only (faster)
bash scripts/check_ray_health.sh --watch           # repeat every 30s
bash scripts/check_ray_health.sh --watch --interval 10
```

Exit code 0 = all checks passed. `[WARN]` entries (known KubeRay 1.0.0 states, pods still starting) do not affect the exit code.

---

## Known issues and workarounds

### vLLM 0.4.2 + Ray 2.9.0 compatibility

`AsyncLLMEngine.from_engine_args()` expects an `AsyncEngineArgs` object (not `EngineArgs`). The code in `services/api/app/models/vllm_engine.py` already uses `AsyncEngineArgs` — do not revert this. If upgrading vLLM, also upgrade the Ray base image and `rayVersion` in both RayService manifests in lockstep, or the worker image and the head node will run different Ray protocol versions.

### Ray Serve proxy + Starlette compatibility

The custom Ray image patches Ray 2.9.0's Serve proxy from `middleware.options` to `middleware.kwargs`. Without this patch, Serve applications can report HEALTHY while every HTTP proxy actor crash-loops and port 8000 refuses traffic. The Dockerfile validates the patched source during image build; do not remove that build step unless Ray/Starlette versions are upgraded and the issue is verified gone.

### Ray Serve API paths

Ray Serve mounts the FastAPI ingress apps under route prefixes. The API must call `/llm/chat/completions` and `/embed/embeddings`; `/llm` and `/embed` are route prefixes and return 404 for POST inference requests. The API Helm values and defaults already use the full paths.

### BGE-M3 embedding dimension

The EKS embedding service uses BGE-M3, which returns 1024-dimensional vectors. Qdrant `rag_collection` and `semantic_cache` must be created with vector size 1024. Local Ollama development with `nomic-embed-text` can still use 768 dimensions, but set `EMBED_DIM=768` and use a separate local Qdrant collection/cache to avoid dimension conflicts.

If an existing EKS collection was created with the wrong dimension, delete and recreate that collection before re-ingestion; Qdrant vector dimensions are immutable.

### GPU NodePool is pinned to g5.2xlarge on-demand

The `gpu` NodePool is currently constrained to `instance-family: g5` + `instance-size: 2xlarge` + `capacity-type: on-demand`. This was narrowed from a broader g/p spot selector after AZ capacity issues and a 16-vCPU G-instance quota limit. If you want spot or larger instances, update `infra/karpenter/provisioner-gpu.yaml`.

### Qdrant PVCs are on gp2

The running Qdrant StatefulSet was provisioned before the gp3 StorageClass existed. The `values.yaml` now specifies `gp3` but the live PVCs are immutable. To migrate: `helm uninstall qdrant && kubectl delete pvc qdrant-storage-qdrant-{0,1,2}` then reinstall. Qdrant re-indexes from scratch — only do this when you can tolerate re-ingestion.

### Ray Serve cold start

GPU workers scale to zero when idle (Karpenter terminates the node). First request after idle takes ~5–7 min:
- Karpenter provisions a g5.2xlarge node: ~2 min
- ECR image pull (~15 GB): ~30 sec via VPC endpoint (no NAT charge)
- S3 model weight sync (~18 GB): ~2–3 min via Gateway endpoint ($0 transfer)
- vLLM loads Llama-3-8B into GPU: ~2 min

Set `min_replicas: 1` in `serveConfigV2` (already done) to keep one replica warm and avoid cold starts. If the S3 model cache bucket is empty, the S3 sync step is skipped and HuggingFace is used instead — functional but slower and charged at NAT rates.

---

## Teardown

```bash
# Delete RayServices first (they own GPU nodes — Karpenter drains them)
kubectl delete rayservice llm-service embed-service

# Watch GPU nodes drain
kubectl get nodeclaims -w   # gpu-* claims should disappear within 2 min

# Remove remaining workloads
kubectl delete -f deploy/k8s/
helm uninstall api qdrant neo4j-cluster kuberay-operator

# Destroy infra (takes ~15 min; RDS has skip_final_snapshot=false so a snapshot is created)
cd infra/terraform && terraform destroy
```

> **Cost note:** The Aurora Serverless v2 cluster continues billing at minimum ACU even when idle. If dev work is paused for >24h, consider pausing Aurora (AWS Console → RDS → Stop temporarily) or destroying and restoring from the final snapshot.
