#!/bin/bash
# Stand up the staging-evidence namespace end to end: fake models, a staging app-env-secret,
# the API Helm release pointed at staging's own database, then the evidence delivery
# components via deploy_evidence_delivery.sh. Requires infra/terraform-staging to already be
# applied (Task 13) and the services/api and services/fake_models images already pushed to ECR.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

REGION="${REGION:-us-east-1}"
NAMESPACE="staging-evidence"
TERRAFORM_DIR="${ROOT_DIR}/infra/terraform-staging"

for command in aws kubectl terraform helm sed openssl; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

API_IMAGE_TAG="${API_IMAGE_TAG:-$(aws ecr describe-images --repository-name services/api --region "$REGION" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' --output text)}"
FAKE_MODELS_IMAGE_TAG="${FAKE_MODELS_IMAGE_TAG:-$(aws ecr describe-images --repository-name services/fake-models \
  --region "$REGION" --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' --output text 2>/dev/null || echo None)}"

if [[ -z "$FAKE_MODELS_IMAGE_TAG" || "$FAKE_MODELS_IMAGE_TAG" == "None" ]]; then
  echo "ERROR: no services/fake-models image found in ECR. Build and push it first:" >&2
  echo "  docker build -f services/fake_models/Dockerfile -t \${ECR_BASE}/services/fake-models:staging ." >&2
  exit 1
fi

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

echo "--- 1. Fake models ---"
sed -e "s|FAKE_MODELS_IMAGE|${ECR_BASE}/services/fake-models:${FAKE_MODELS_IMAGE_TAG}|g" \
  "${ROOT_DIR}/deploy/k8s/staging-fake-models.yaml" | kubectl apply -f -
kubectl rollout status deployment/fake-models --namespace "$NAMESPACE" --timeout=5m

echo "--- 1b. Redis (required by the API's /health/readiness probe; not otherwise used) ---"
kubectl apply -f "${ROOT_DIR}/deploy/k8s/staging-redis.yaml"
kubectl rollout status deployment/redis --namespace "$NAMESPACE" --timeout=5m

echo "--- 2. Staging app-env-secret ---"
STAGING_DB_ENDPOINT="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_db_endpoint)"
STAGING_DB_SECRET_ARN="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_db_secret_arn)"
STAGING_DB_PASSWORD="$(aws secretsmanager get-secret-value --secret-id "$STAGING_DB_SECRET_ARN" \
  --query SecretString --output text | python3 -c 'import json,sys; print(json.load(sys.stdin)["password"])')"
STAGING_DATABASE_URL="postgresql+asyncpg://ragadmin:${STAGING_DB_PASSWORD}@${STAGING_DB_ENDPOINT}:5432/evidence_staging"
STAGING_JWT_SECRET="${STAGING_JWT_SECRET:-$(openssl rand -hex 32)}"
STAGING_IDENTITY_KEY="${STAGING_IDENTITY_KEY:-$(openssl rand -hex 32)}"

# services/api/app/config.py's Settings (pydantic BaseSettings, instantiated eagerly at import)
# requires REDIS_URL, NEO4J_PASSWORD and S3_BUCKET_NAME with no defaults, even though staging
# evidence validation only exercises the evidence-delivery path (Postgres outbox + SQS +
# evidence S3), not chat's Redis cache, Neo4j graph, or document uploads.
#
# REDIS_URL points at the real in-namespace Redis deployed in step 1b: services/api/main.py's
# lifespan calls `redis_client.connect()`, which only builds a connection pool object (no
# socket I/O), but /health/readiness actively pings Redis on every probe — Helm's
# `--wait` needs that probe to succeed, so a dummy/unreachable value would hang Stage 3 forever
# instead of failing fast. NEO4J_PASSWORD and S3_BUCKET_NAME are safe as inert placeholders:
# Neo4jClient.connect() only constructs a driver object (no connectivity check), readiness only
# checks that the driver object exists (no live query), and S3_BUCKET_NAME is read solely by
# the upload/presigned-URL route, which staging evidence validation never calls.
STAGING_REDIS_URL="${STAGING_REDIS_URL:-redis://redis:6379/0}"

kubectl create secret generic app-env-secret --namespace "$NAMESPACE" \
  --from-literal=DATABASE_URL="$STAGING_DATABASE_URL" \
  --from-literal=JWT_SECRET_KEY="$STAGING_JWT_SECRET" \
  --from-literal=EVAL_IDENTITY_HASH_KEY="$STAGING_IDENTITY_KEY" \
  --from-literal=REDIS_URL="$STAGING_REDIS_URL" \
  --from-literal=NEO4J_PASSWORD="unused-in-staging-evidence" \
  --from-literal=S3_BUCKET_NAME="unused-in-staging-evidence" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "--- 3. Staging API Helm release ---"
helm upgrade --install api-staging "${ROOT_DIR}/deploy/helm/api" \
  --namespace "$NAMESPACE" \
  --set image.repository="${ECR_BASE}/services/api" \
  --set image.tag="${API_IMAGE_TAG}" \
  --set replicaCount=1 \
  --set fullnameOverride=api-staging \
  --set-string env[0].name=ENV --set-string env[0].value=local \
  --set-string env[1].name=LOG_LEVEL --set-string env[1].value=INFO \
  --set-string env[2].name=AWS_REGION --set-string env[2].value="$REGION" \
  --set-string env[3].name=RAY_LLM_ENDPOINT --set-string env[3].value="http://fake-models:8000/v1/chat/completions" \
  --set-string env[4].name=RAY_EMBED_ENDPOINT --set-string env[4].value="http://fake-models:8000/api/embeddings" \
  --set-string env[5].name=EVAL_EVIDENCE_ENABLED --set-string env[5].value="true" \
  --set-string env[6].name=EVAL_CAPTURE_CONTENT --set-string env[6].value="false" \
  --set-string env[7].name=EVAL_QUEUE_URL --set-string env[7].value="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_evidence_queue_url)" \
  --wait --timeout 5m

echo "--- 4. Evidence delivery components ---"
NAMESPACE="$NAMESPACE" TERRAFORM_DIR="$TERRAFORM_DIR" bash "${SCRIPT_DIR}/deploy_evidence_delivery.sh"

echo "--- Staging evidence stack deployed ---"
echo "Port-forward the staging API: kubectl port-forward -n ${NAMESPACE} deployment/api-staging 18000:8000"
