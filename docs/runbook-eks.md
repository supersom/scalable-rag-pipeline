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
                              ├── llm-service  (vLLM + Llama-3-8B, port 8000 /llm)
                              └── embed-service (bge-m3,           port 8000 /embed)
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
terraform output -raw aurora_cluster_endpoint    # AURORA_ENDPOINT
terraform output -raw aurora_master_secret_arn   # DB_SECRET_ARN
terraform output -raw redis_endpoint             # REDIS_URL (prepend rediss://)
terraform output -raw s3_bucket_name             # S3_BUCKET
```

> **Note on Aurora password:** `ManageMasterUserPassword = true` means Secrets Manager holds and rotates the password. Never hardcode it. Always retrieve via `aws secretsmanager get-secret-value`.

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

The Ray Serve image uses Docker layer caching — only the `COPY services` layer changes on code edits, so rebuilds after the first are ~5 seconds.

### 3. Bootstrap the cluster

```bash
export API_IMAGE_TAG=${GIT_SHA}
export RAY_IMAGE_TAG=${GIT_SHA}
export DB_SECRET_ARN=<from terraform output>
export AURORA_ENDPOINT=<from terraform output>
export REDIS_URL=rediss://<from terraform output>
export S3_BUCKET=<from terraform output>

bash scripts/bootstrap_cluster.sh
```

The script runs steps 1–11 in order:
1. kubeconfig update
2. Karpenter NodePool/EC2NodeClass apply
3. gp3 StorageClass
4. NVIDIA device plugin DaemonSet
5. Helm repos
6. KubeRay operator
7. Qdrant (3-replica StatefulSet)
8. Neo4j (community, 100GB gp3 PVC)
9. `app-env-secret` with all env vars
10. API Helm chart
11. RayService for LLM + embeddings

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
AUTH_TOKEN=$(kubectl get secret app-env-secret \
  -o jsonpath='{.data.AUTH_TOKEN}' | base64 -d)
curl -s -H "Authorization: Bearer $AUTH_TOKEN" \
  http://localhost:8000/api/v1/chat/stream \
  -d '{"message":"hello"}' | jq .
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
```

> **Important:** always `kubectl apply`, never `kubectl delete && kubectl apply` for RayService. Deleting the live service during update causes the KubeRay FSM to enter `FailedToUpdateService` because the old cluster it tries to clean up is already gone.

### Force-reconcile a stuck RayService

If `kubectl get rayservice` shows `FailedToUpdateService`:

```bash
# Nudge the controller — it retries immediately
kubectl annotate rayservice llm-service force-reconcile="$(date +%s)" --overwrite
# If that doesn't clear within 2 min, delete and re-apply (accepts brief downtime):
kubectl delete rayservice llm-service && kubectl apply -f deploy/ray/ray-serve-llm.yaml
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
  "{\"data\":{\"DATABASE_URL\":\"$(echo -n "postgresql://ragadmin:${DB_PASSWORD}@${AURORA_ENDPOINT}/rag_db" | base64 -w0)\"}}"
kubectl rollout restart deployment/api
```

---

## Known issues and workarounds

### vLLM 0.4.2 + Ray 2.9.0 compatibility

`AsyncLLMEngine.from_engine_args()` expects an `AsyncEngineArgs` object (not `EngineArgs`). The code in `services/api/app/models/vllm_engine.py` already uses `AsyncEngineArgs` — do not revert this. If upgrading vLLM, also upgrade the Ray base image and `rayVersion` in both RayService manifests in lockstep, or the worker image and the head node will run different Ray protocol versions.

### GPU node gets g6 instead of g5

Karpenter's `gpu` NodePool lists `g5` and `g6` families. Both have NVIDIA A10G/L4 with 24GB VRAM and fit Llama-3-8B at fp16. If Karpenter picks `g6.4xlarge` instead of `g5.2xlarge`, that's fine — the workload runs identically.

### Qdrant PVCs are on gp2

The running Qdrant StatefulSet was provisioned before the gp3 StorageClass existed. The `values.yaml` now specifies `gp3` but the live PVCs are immutable. To migrate: `helm uninstall qdrant && kubectl delete pvc qdrant-storage-qdrant-{0,1,2}` then reinstall. Qdrant re-indexes from scratch — only do this when you can tolerate re-ingestion.

### Ray Serve cold start

GPU workers scale to zero when idle (Karpenter terminates the node). First request after idle takes ~5 min: Karpenter provisions a node (~2 min) + image pull from ECR (~30 sec, most layers cached after first pull on that node) + vLLM loads Llama-3-8B (~2 min). Set `min_replicas: 1` in `serveConfigV2` (already done) to keep one replica warm and avoid cold starts.

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
