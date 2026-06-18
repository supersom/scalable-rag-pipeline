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
#   - ECR repos exist: services/api, services/ray-serve
#   - API and Ray Serve images already built and pushed (see README)
#
# Usage:
#   export API_IMAGE_TAG=<git-sha>
#   export RAY_IMAGE_TAG=<git-sha>
#   export DB_SECRET_ARN=<aurora-secret-arn>   # from terraform output
#   export DB_ENDPOINT=<aurora-endpoint>   # from terraform output
#   export REDIS_URL=<redis-url>               # from terraform output
#   export S3_BUCKET=<bucket-name>             # from terraform output
#   bash scripts/bootstrap_cluster.sh

set -euo pipefail

CLUSTER_NAME="rag-platform-cluster"
REGION="us-east-1"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

: "${API_IMAGE_TAG:?API_IMAGE_TAG is required}"
: "${RAY_IMAGE_TAG:?RAY_IMAGE_TAG is required}"
: "${DB_SECRET_ARN:?DB_SECRET_ARN is required}"
: "${DB_ENDPOINT:?DB_ENDPOINT is required}"
: "${REDIS_URL:?REDIS_URL is required}"
: "${S3_BUCKET:?S3_BUCKET is required}"

echo "--- 1. Kubeconfig ---"
aws eks update-kubeconfig --name "$CLUSTER_NAME" --region "$REGION"

echo "--- 2. Karpenter ---"
# IRSA role ARN comes from terraform output; helm value is already set in values if using helm.
# Just apply the NodePool and EC2NodeClass manifests:
kubectl apply -f infra/karpenter/provisioner-cpu.yaml
kubectl apply -f infra/karpenter/provisioner-gpu.yaml
echo "Waiting for Karpenter NodePools to be Ready..."
kubectl wait nodepools cpu gpu --for=condition=Ready --timeout=60s 2>/dev/null || true

echo "--- 3. Storage ---"
kubectl apply -f deploy/k8s/storageclass-gp3.yaml

echo "--- 4. NVIDIA device plugin ---"
kubectl apply -f deploy/k8s/nvidia-device-plugin.yaml

echo "--- 5. Helm repos ---"
helm repo add kuberay  https://ray-project.github.io/kuberay-helm/
helm repo add qdrant   https://qdrant.github.io/qdrant-helm/
helm repo add neo4j    https://helm.neo4j.com/neo4j
helm repo update

echo "--- 6. KubeRay operator ---"
helm upgrade --install kuberay-operator kuberay/kuberay-operator --version 1.0.0 \
  --wait --timeout 3m

echo "--- 7. Qdrant ---"
helm upgrade --install qdrant qdrant/qdrant -f deploy/helm/qdrant/values.yaml \
  --wait --timeout 5m

echo "--- 8. Neo4j ---"
helm upgrade --install neo4j-cluster neo4j/neo4j \
  --version 2026.5.0 \
  -f deploy/helm/neo4j/values.yaml \
  --wait --timeout 5m

echo "--- 9. Data store schema ---"
# BGE-M3 emits 1024-dimensional embeddings. Create collections if missing.
kubectl run qdrant-schema --rm -i --restart=Never --image=curlimages/curl:8.8.0 -- \
  sh -c 'set -e; for c in rag_collection semantic_cache; do if curl -fsS "http://qdrant:6333/collections/${c}" >/dev/null; then echo "${c} exists"; else curl -fsS -X PUT "http://qdrant:6333/collections/${c}" -H "Content-Type: application/json" -d "{\"vectors\":{\"size\":1024,\"distance\":\"Cosine\"}}"; echo; fi; done'

kubectl exec neo4j-cluster-0 -- cypher-shell -u neo4j -p password \
  'CREATE FULLTEXT INDEX entity_index IF NOT EXISTS FOR (n:Entity) ON EACH [n.name]'

echo "--- 10. app-env-secret ---"
# Retrieve Aurora password from Secrets Manager (ManageMasterUserPassword — never hardcode)
DB_PASSWORD=$(aws secretsmanager get-secret-value \
  --secret-id "$DB_SECRET_ARN" \
  --query SecretString --output text \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['password'])")

kubectl create secret generic app-env-secret \
  --from-literal=DATABASE_URL="postgresql://ragadmin:${DB_PASSWORD}@${DB_ENDPOINT}/rag_db" \
  --from-literal=REDIS_URL="${REDIS_URL}" \
  --from-literal=S3_BUCKET_NAME="${S3_BUCKET}" \
  --from-literal=NEO4J_URI="bolt://neo4j-cluster:7687" \
  --from-literal=NEO4J_PASSWORD="password" \
  --from-literal=RAY_LLM_ENDPOINT="http://llm-service:8000/llm/chat/completions" \
  --from-literal=RAY_EMBED_ENDPOINT="http://embed-service:8000/embed/embeddings" \
  --from-literal=EMBED_DIM="1024" \
  --from-literal=LLM_API_KEY="" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "--- 11. API ---"
if [ ! -f deploy/helm/api/Chart.yaml ]; then
  echo "ERROR: deploy/helm/api/Chart.yaml not found. Build the API Helm chart first." >&2
  exit 1
fi
helm upgrade --install api deploy/helm/api \
  --set image.repository="${ECR_BASE}/services/api" \
  --set image.tag="${API_IMAGE_TAG}" \
  --wait --timeout 5m

echo "--- 12. Ray Serve (LLM + Embeddings) ---"
# Substitute the image tag into the manifests before applying.
# RayService triggers GPU node provisioning via Karpenter — allow up to 15 min.
sed "s|ray-serve:[^ ]*|ray-serve:${RAY_IMAGE_TAG}|g" deploy/ray/ray-serve-llm.yaml \
  | kubectl apply -f -
sed "s|ray-serve:[^ ]*|ray-serve:${RAY_IMAGE_TAG}|g" deploy/ray/ray-serve-embed.yaml \
  | kubectl apply -f -

echo ""
echo "✅ Bootstrap complete."
echo ""
echo "Monitor GPU provisioning:  kubectl get nodeclaims"
echo "Watch RayService status:   kubectl get rayservice"
echo "Watch all pods:            kubectl get pods -w"
echo ""
echo "LLM loading takes ~5 min after the GPU node joins."
echo "End-to-end test (once Running):"
echo "  AUTH_TOKEN=\$(kubectl get secret app-env-secret -o jsonpath='{.data.AUTH_TOKEN}' | base64 -d)"
echo "  kubectl port-forward svc/api-service 8000:80 &"
echo '  curl -H "Authorization: Bearer $AUTH_TOKEN" http://localhost:8000/api/v1/chat/stream \'
echo '    -d '"'"'{"message":"hello"}'"'"
