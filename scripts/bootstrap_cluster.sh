#!/bin/bash
# scripts/bootstrap_cluster.sh
#
# Bootstraps a freshly-created EKS cluster to a fully operational state.
# Run AFTER `terraform apply` completes.
#
# Prerequisites:
#   - aws CLI configured with sufficient IAM permissions
#   - kubectl, helm, docker installed locally
#   - Karpenter IAM roles exist (created by terraform/karpenter.tf)
#   - ECR repos exist for API/Ray; Terraform creates services/ingestion
#   - API, Ray Serve, and ingestion images already built and pushed (see README)
#
# Usage: copy-paste the block below into your terminal, then run the script.
: <<'USAGE'
export API_IMAGE_TAG=$(aws ecr describe-images --repository-name services/api --region us-east-1 --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' --output text)
export RAY_IMAGE_TAG=$(aws ecr describe-images --repository-name services/ray-serve --region us-east-1 --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' --output text)
export DB_SECRET_ARN=$(terraform -chdir=infra/terraform output -raw db_secret_arn)
export DB_ENDPOINT=$(terraform -chdir=infra/terraform output -raw db_endpoint)
export REDIS_URL="rediss://$(terraform -chdir=infra/terraform output -raw redis_primary_endpoint):6379"
export S3_BUCKET=$(terraform -chdir=infra/terraform output -raw s3_documents_bucket_name)
export JWT_SECRET_KEY=$(openssl rand -hex 32)
echo "API_IMAGE_TAG    = $API_IMAGE_TAG"
echo "RAY_IMAGE_TAG    = $RAY_IMAGE_TAG"
echo "DB_SECRET_ARN    = $DB_SECRET_ARN"
echo "DB_ENDPOINT      = $DB_ENDPOINT"
echo "REDIS_URL        = $REDIS_URL"
echo "S3_BUCKET        = $S3_BUCKET"
echo "JWT_SECRET_KEY   = $JWT_SECRET_KEY"
bash scripts/bootstrap_cluster.sh
USAGE

set -euo pipefail

CLUSTER_NAME="rag-platform-cluster"
REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

# ── Stage range ───────────────────────────────────────────────────────────────
# --start N --end M  : run stages N through M inclusive
# --start N --end N  : run stage N only
# --start N          : run stage N only (end defaults to start)
# (no flags)         : run all stages

MAX_STAGE=14
START=1
END=$MAX_STAGE
_start_set=false
_end_set=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --start) START=$2; _start_set=true; shift 2 ;;
    --end)   END=$2;   _end_set=true;   shift 2 ;;
    *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
  esac
done

if $_start_set && ! $_end_set; then
  END=$START
fi

if (( START < 1 || END > MAX_STAGE || START > END )); then
  echo "ERROR: invalid stage range --start $START --end $END (valid: 1–$MAX_STAGE)" >&2
  exit 1
fi

[[ $START -eq $END ]] \
  && echo "Running stage $START of $MAX_STAGE" \
  || echo "Running stages $START–$END of $MAX_STAGE"

stage_active() { (( $1 >= START && $1 <= END )); }

# ── Stages ────────────────────────────────────────────────────────────────────

if stage_active 1; then
  echo "--- 1. Kubeconfig ---"
  aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION"
fi

if stage_active 2; then
  echo "--- 2. Karpenter controller (Helm) ---"
  KARPENTER_ROLE_ARN=$(terraform -chdir=infra/terraform output -raw karpenter_controller_role_arn)
  CLUSTER_ENDPOINT=$(terraform -chdir=infra/terraform output -raw eks_cluster_endpoint)
  # 1.13.0: K8s 1.35 needs Karpenter >= 1.9 (1.0.8 tops out at K8s 1.32)
  helm upgrade --install karpenter oci://public.ecr.aws/karpenter/karpenter \
    --version 1.13.0 \
    --namespace kube-system \
    --set "serviceAccount.annotations.eks\.amazonaws\.com/role-arn=${KARPENTER_ROLE_ARN}" \
    --set "settings.clusterName=${CLUSTER_NAME}" \
    --set "settings.clusterEndpoint=${CLUSTER_ENDPOINT}" \
    --wait --timeout 3m
fi

if stage_active 3; then
  echo "--- 3. Karpenter NodePools ---"
  kubectl apply -f infra/karpenter/provisioner-cpu.yaml
  kubectl apply -f infra/karpenter/provisioner-gpu.yaml
  echo "Waiting for Karpenter NodePools to be Ready..."
  kubectl wait nodepools cpu gpu --for=condition=Ready --timeout=60s 2>/dev/null || true
fi

if stage_active 4; then
  echo "--- 4. Storage ---"
  kubectl apply -f deploy/k8s/storageclass-gp3.yaml
fi

if stage_active 5; then
  echo "--- 5. NVIDIA device plugin ---"
  kubectl apply -f deploy/k8s/nvidia-device-plugin.yaml
fi

if stage_active 6; then
  echo "--- 6. Helm repos ---"
  helm repo add kuberay  https://ray-project.github.io/kuberay-helm/
  helm repo add qdrant   https://qdrant.github.io/qdrant-helm/
  helm repo add neo4j    https://helm.neo4j.com/neo4j
  helm repo update
fi

if stage_active 7; then
  echo "--- 7. KubeRay operator ---"
  # 1.2.2 is the ceiling while images run Ray 2.9.0 — KubeRay 1.3+ requires Ray >= 2.38
  helm upgrade --install kuberay-operator kuberay/kuberay-operator --version 1.2.2 \
    --wait --timeout 3m
fi

if stage_active 8; then
  echo "--- 8. Qdrant ---"
  helm upgrade --install qdrant qdrant/qdrant -f deploy/helm/qdrant/values.yaml \
    --wait --timeout 5m
fi

if stage_active 9; then
  echo "--- 9. Neo4j ---"
  helm upgrade --install neo4j-cluster neo4j/neo4j \
    --version 2026.5.0 \
    -f deploy/helm/neo4j/values.yaml \
    --wait --timeout 5m
fi

if stage_active 10; then
  echo "--- 10. Data store schema ---"
  # BGE-M3 emits 1024-dimensional embeddings. Create collections if missing.
  kubectl run qdrant-schema --rm -i --restart=Never --image=curlimages/curl:8.8.0 -- \
    sh -c 'set -e; for c in rag_collection semantic_cache; do if curl -fsS "http://qdrant:6333/collections/${c}" >/dev/null; then echo "${c} exists"; else curl -fsS -X PUT "http://qdrant:6333/collections/${c}" -H "Content-Type: application/json" -d "{\"vectors\":{\"size\":1024,\"distance\":\"Cosine\"}}"; echo; fi; done'

  kubectl exec neo4j-cluster-0 -- cypher-shell -u neo4j -p password \
    'CREATE FULLTEXT INDEX entity_index IF NOT EXISTS FOR (n:Entity) ON EACH [n.name]'
fi

if stage_active 11; then
  echo "--- 11. app-env-secret ---"
  : "${DB_SECRET_ARN:?DB_SECRET_ARN is required for stage 11}"
  : "${DB_ENDPOINT:?DB_ENDPOINT is required for stage 11}"
  : "${REDIS_URL:?REDIS_URL is required for stage 11}"
  : "${S3_BUCKET:?S3_BUCKET is required for stage 11}"
  : "${JWT_SECRET_KEY:?JWT_SECRET_KEY is required for stage 11 (generate with: openssl rand -hex 32)}"

  # Retrieve Aurora password from Secrets Manager (ManageMasterUserPassword — never hardcode)
  DB_PASSWORD=$(aws secretsmanager get-secret-value \
    --secret-id "$DB_SECRET_ARN" \
    --query SecretString --output text \
    | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

  # Tavily API key is optional — Terraform only provisions a secret for it when
  # var.tavily_api_key was set in terraform.tfvars (see infra/terraform/secrets.tf).
  # `|| echo ""` guards against `terraform output` failing under set -e if the
  # output isn't defined yet (e.g. stale state predating this output).
  TAVILY_SECRET_ARN=$(terraform -chdir=infra/terraform output -raw tavily_api_key_secret_arn 2>/dev/null || echo "")
  if [[ -n "$TAVILY_SECRET_ARN" ]]; then
    TAVILY_API_KEY=$(aws secretsmanager get-secret-value \
      --secret-id "$TAVILY_SECRET_ARN" \
      --query SecretString --output text)
  else
    TAVILY_API_KEY=""
  fi

  kubectl create secret generic app-env-secret \
    --from-literal=DATABASE_URL="postgresql+asyncpg://ragadmin:${DB_PASSWORD}@${DB_ENDPOINT}/rag_db" \
    --from-literal=REDIS_URL="${REDIS_URL}" \
    --from-literal=S3_BUCKET_NAME="${S3_BUCKET}" \
    --from-literal=NEO4J_URI="bolt://neo4j-cluster:7687" \
    --from-literal=NEO4J_PASSWORD="password" \
    --from-literal=RAY_LLM_ENDPOINT="http://llm-service-head-svc:8000/llm/chat/completions" \
    --from-literal=RAY_EMBED_ENDPOINT="http://embed-service-head-svc:8000/embed/embeddings" \
    --from-literal=EMBED_DIM="1024" \
    --from-literal=LLM_API_KEY="" \
    --from-literal=JWT_SECRET_KEY="${JWT_SECRET_KEY}" \
    --from-literal=TAVILY_API_KEY="${TAVILY_API_KEY}" \
    --dry-run=client -o yaml | kubectl apply -f -
fi

if stage_active 12; then
  echo "--- 12. API ---"
  : "${API_IMAGE_TAG:?API_IMAGE_TAG is required for stage 12}"
  if [ ! -f deploy/helm/api/Chart.yaml ]; then
    echo "ERROR: deploy/helm/api/Chart.yaml not found. Build the API Helm chart first." >&2
    exit 1
  fi
  helm upgrade --install api deploy/helm/api \
    --set image.repository="${ECR_BASE}/services/api" \
    --set image.tag="${API_IMAGE_TAG}" \
    --wait --timeout 5m
fi

if stage_active 13; then
  echo "--- 13. Ray Serve (LLM + Embeddings) ---"
  : "${RAY_IMAGE_TAG:?RAY_IMAGE_TAG is required for stage 13}"
  # Substitute the image tag into the manifests before applying.
  # RayService triggers GPU node provisioning via Karpenter — allow up to 15 min.
  sed "s|ray-serve:[^ ]*|ray-serve:${RAY_IMAGE_TAG}|g" deploy/ray/ray-serve-llm.yaml \
    | kubectl apply -f -
  sed "s|ray-serve:[^ ]*|ray-serve:${RAY_IMAGE_TAG}|g" deploy/ray/ray-serve-embed.yaml \
    | kubectl apply -f -
fi

if stage_active 14; then
  echo "--- 14. Queue-backed ingestion ---"
  bash scripts/deploy_ingestion.sh
fi

echo ""
if [[ $START -eq 1 && $END -eq $MAX_STAGE ]]; then
  echo "Bootstrap complete."
  echo ""
  echo "Monitor GPU provisioning:  kubectl get nodeclaims"
  echo "Watch RayService status:   kubectl get rayservice"
  echo "Watch all pods:            kubectl get pods -w"
  echo "Watch ingestion:           kubectl logs -f deploy/ingestion-worker"
  echo ""
  echo "LLM loading takes ~5 min after the GPU node joins."
  echo "End-to-end test (once Running):"
  echo "  AUTH_TOKEN=\$(kubectl get secret app-env-secret -o jsonpath='{.data.AUTH_TOKEN}' | base64 -d)"
  echo "  kubectl port-forward svc/api-service 8000:80 &"
  echo '  curl -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:8000/api/v1/chat/stream \'
  echo '    -d '"'"'{"message":"hello"}'"'"
else
  [[ $START -eq $END ]] \
    && echo "Stage $START complete." \
    || echo "Stages $START–$END complete."
fi
