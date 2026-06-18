# EKS Cluster Runbook — scalable-rag-pipeline

How to bring up the full stack from zero, diagnose common failures, and tear it down.

---

## Architecture summary

```
Internet → (ALB/Ingress) → FastAPI (EKS, ECR image)
                         → Qdrant (StatefulSet, gp3 PVCs)
                         → Neo4j  (StatefulSet, gp3 PVC)
                         → Aurora PostgreSQL (Serverless v2, Secrets Manager password)
                         → ElastiCache Redis (TLS, rediss://)
                         → Ray Serve on g6/g5 GPU nodes (Karpenter spot/on-demand)
                              ├── llm-service  (vLLM + Llama-3-8B, port 8000 /llm/chat/completions)
                              └── embed-service (BGE-M3,           port 8000 /embed/embeddings)
```

Node pools:
- `system` — 2× m6i.large (managed, always on), runs CoreDNS + Karpenter + kuberay-operator
- `cpu` (Karpenter) — m6i/c6i/r6i spot, 100GB root; runs API, Qdrant, Neo4j, Ray heads
- `gpu` (Karpenter) — g6/g5 on-demand, 100GB root; runs Ray GPU workers

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
terraform output -raw redis_primary_endpoint     # REDIS_URL (prepend rediss://)
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
export REDIS_URL=rediss://<from terraform output>
export S3_BUCKET=<from terraform output>

bash scripts/bootstrap_cluster.sh
```

The script runs steps 1–12 in order:
1. kubeconfig update
2. Karpenter NodePool/EC2NodeClass apply
3. gp3 StorageClass
4. NVIDIA device plugin DaemonSet
5. Helm repos
6. KubeRay operator
7. Qdrant (3-replica StatefulSet)
8. Neo4j (community, 100GB gp3 PVC)
9. Data-store schema: create missing Qdrant `rag_collection` + `semantic_cache` at 1024 dimensions, and Neo4j `entity_index`
10. `app-env-secret` with all env vars
11. API Helm chart
12. RayService for LLM + embeddings

The API also creates its `chat_history` table on startup via SQLAlchemy `Base.metadata.create_all()`, so a fresh Aurora database does not need a manual table creation step.

### 4. Wait for GPU inference

```bash
# Watch Karpenter provision the GPU node (g6.4xlarge, ~2 min)
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

### GPU node gets g6 instead of g5

Karpenter's `gpu` NodePool lists `g5` and `g6` families. Both have NVIDIA A10G/L4 with 24GB VRAM and fit Llama-3-8B at fp16. If Karpenter picks `g6.4xlarge` instead of `g5.2xlarge`, that's fine — the workload runs identically.

### Qdrant PVCs are on gp2

The running Qdrant StatefulSet was provisioned before the gp3 StorageClass existed. The `values.yaml` now specifies `gp3` but the live PVCs are immutable. To migrate: `helm uninstall qdrant && kubectl delete pvc qdrant-storage-qdrant-{0,1,2}` then reinstall. Qdrant re-indexes from scratch — only do this when you can tolerate re-ingestion.

### Ray Serve cold start

GPU workers scale to zero when idle (Karpenter terminates the node). First request after idle takes ~5–7 min:
- Karpenter provisions a g6/g5 node: ~2 min
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

> **Cost note:** The Aurora Serverless v2 cluster continues billing at minimum ACU even when idle. The ElastiCache instance also runs continuously. If dev work is paused for >24h, consider pausing Aurora (AWS Console → RDS → Stop temporarily) or destroying and restoring from the final snapshot.
