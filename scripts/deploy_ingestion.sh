#!/bin/bash
# Deploy or update the queue-backed ingestion components in an existing cluster.
#
# Required infrastructure:
#   - Terraform ingestion resources have been applied.
#   - The services/ingestion image has been pushed to ECR.
#   - KubeRay and the CPU Karpenter NodePool are installed.
#   - app-env-secret, Qdrant, Neo4j, and the model RayServices exist.
#
# All values can be exported explicitly. When omitted, this script derives them
# from AWS/ECR and Terraform outputs.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

REGION="${REGION:-us-east-1}"
NAMESPACE="${NAMESPACE:-default}"
TERRAFORM_DIR="${TERRAFORM_DIR:-${ROOT_DIR}/infra/terraform}"

for command in aws kubectl terraform sed; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

INGESTION_IMAGE_TAG="${INGESTION_IMAGE_TAG:-$(aws ecr describe-images \
  --repository-name services/ingestion \
  --region "$REGION" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' \
  --output text)}"
INGESTION_QUEUE_URL="${INGESTION_QUEUE_URL:-$(terraform -chdir="$TERRAFORM_DIR" output -raw ingestion_queue_url)}"
INGESTION_ROLE_ARN="${INGESTION_ROLE_ARN:-$(terraform -chdir="$TERRAFORM_DIR" output -raw ingestion_irsa_role_arn)}"

if [[ -z "$INGESTION_IMAGE_TAG" || "$INGESTION_IMAGE_TAG" == "None" ]]; then
  echo "ERROR: no services/ingestion image tag found in ECR." >&2
  exit 1
fi

aws ecr describe-images \
  --repository-name services/ingestion \
  --image-ids "imageTag=${INGESTION_IMAGE_TAG}" \
  --region "$REGION" \
  >/dev/null

INGESTION_IMAGE="${ECR_BASE}/services/ingestion:${INGESTION_IMAGE_TAG}"

kubectl cluster-info >/dev/null
kubectl get secret app-env-secret --namespace "$NAMESPACE" >/dev/null

echo "--- Ingestion deployment ---"
echo "Namespace : ${NAMESPACE}"
echo "Image     : ${INGESTION_IMAGE}"
echo "Queue     : ${INGESTION_QUEUE_URL}"
echo "IAM role  : ${INGESTION_ROLE_ARN}"

echo "--- 1. Service account ---"
kubectl create serviceaccount ingestion-worker --namespace "$NAMESPACE" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl annotate serviceaccount ingestion-worker --namespace "$NAMESPACE" \
  "eks.amazonaws.com/role-arn=${INGESTION_ROLE_ARN}" --overwrite

echo "--- 2. CPU RayCluster ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|INGESTION_IMAGE|${INGESTION_IMAGE}|g" \
  "${ROOT_DIR}/deploy/ray/ingestion-cluster.yaml" | kubectl apply -f -

echo "Waiting for ingestion-ray head service..."
for _ in $(seq 1 60); do
  if kubectl get service ingestion-ray-head-svc --namespace "$NAMESPACE" >/dev/null 2>&1; then
    break
  fi
  sleep 5
done
kubectl get service ingestion-ray-head-svc --namespace "$NAMESPACE" >/dev/null

echo "Waiting for ingestion-ray head pod..."
kubectl wait pod \
  --namespace "$NAMESPACE" \
  --selector=ray.io/cluster=ingestion-ray,ray.io/node-type=head \
  --for=condition=Ready \
  --timeout=10m

echo "--- 3. SQS consumer ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|INGESTION_IMAGE|${INGESTION_IMAGE}|g" \
  -e "s|INGESTION_QUEUE_URL_VALUE|${INGESTION_QUEUE_URL}|g" \
  "${ROOT_DIR}/deploy/k8s/ingestion-worker.yaml" | kubectl apply -f -

kubectl rollout status deployment/ingestion-worker \
  --namespace "$NAMESPACE" \
  --timeout=5m

echo "--- Ingestion deployment complete ---"
echo "Watch worker logs: kubectl logs -f deployment/ingestion-worker -n ${NAMESPACE}"
echo "List Ray jobs:     kubectl port-forward -n ${NAMESPACE} service/ingestion-ray-head-svc 8265:8265"
