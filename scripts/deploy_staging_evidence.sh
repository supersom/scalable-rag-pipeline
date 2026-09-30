#!/bin/bash
# Stand up the staging-evidence namespace end to end: fake models, a staging app-env-secret,
# the API Helm release pointed at staging's own database, then the evidence delivery
# components via deploy_evidence_delivery.sh. Requires infra/terraform-staging to already be
# applied (Task 13) and the services/api and services/fake_models images already pushed to ECR.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

# Load only model-provider settings from a dotenv file. Bash does not load these
# files automatically, and sourcing one would execute arbitrary shell content.
# Prefer the private evidence config, then the safe checked-in sample. The sample
# has blank hosted endpoints and keys, which keeps staging on fake-models when
# private credentials are unavailable. Set STAGING_ENV_FILE to choose another
# file explicitly.
STAGING_ENV_FILE="${STAGING_ENV_FILE:-}"
if [[ -z "$STAGING_ENV_FILE" ]]; then
  for candidate in "${ROOT_DIR}/.env.evidence" "${ROOT_DIR}/.env.example"; do
    if [[ -f "$candidate" ]]; then
      STAGING_ENV_FILE="$candidate"
      break
    fi
  done
elif [[ "$STAGING_ENV_FILE" != /* ]]; then
  if [[ -f "$PWD/$STAGING_ENV_FILE" ]]; then
    STAGING_ENV_FILE="$PWD/$STAGING_ENV_FILE"
  else
    STAGING_ENV_FILE="${ROOT_DIR}/${STAGING_ENV_FILE}"
  fi
fi

if [[ -n "$STAGING_ENV_FILE" ]]; then
  if [[ ! -f "$STAGING_ENV_FILE" ]]; then
    echo "ERROR: STAGING_ENV_FILE does not exist: ${STAGING_ENV_FILE}" >&2
    exit 1
  fi
  while IFS= read -r env_line || [[ -n "$env_line" ]]; do
    env_line="${env_line%$'\r'}"
    env_line="${env_line#"${env_line%%[![:space:]]*}"}"
    [[ -z "$env_line" || "$env_line" == \#* ]] && continue
    [[ "$env_line" == export\ * ]] && env_line="${env_line#export }"
    [[ "$env_line" =~ ^([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*=(.*)$ ]] || continue
    env_key="${BASH_REMATCH[1]}"
    case "$env_key" in
      LLM_API_ENDPOINT|LLM_API_KEY|LLM_MODEL_NAME|LLM_MODEL_ID|\
      EMBED_API_ENDPOINT|EMBED_API_KEY|EMBED_MODEL_NAME|EMBED_DIM|\
      RERANK_API_ENDPOINT|RERANK_API_KEY|RERANK_MODEL_NAME|\
      LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED) ;;
      *) continue ;;
    esac
    env_value="${BASH_REMATCH[2]}"
    env_value="${env_value#"${env_value%%[![:space:]]*}"}"
    env_value="${env_value%"${env_value##*[![:space:]]}"}"
    if [[ ${#env_value} -ge 2 && ( "$env_value" == \"*\" || "$env_value" == \'*\' ) ]]; then
      env_value="${env_value:1:${#env_value}-2}"
    fi
    printf -v "$env_key" '%s' "$env_value"
    export "$env_key"
  done < "$STAGING_ENV_FILE"
  echo "Loaded hosted-model settings from ${STAGING_ENV_FILE}"
fi

REGION="${REGION:-us-east-1}"
NAMESPACE="staging-evidence"
TERRAFORM_DIR="${ROOT_DIR}/infra/terraform-staging"

for command in aws kubectl terraform helm sed openssl python3; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

CLUSTER_NAME="${CLUSTER_NAME:-rag-platform-cluster}"
if ! CLUSTER_STATUS="$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query 'cluster.status' --output text 2>/dev/null)"; then
  echo "ERROR: cannot verify EKS cluster ${CLUSTER_NAME}; check AWS account, region, and EKS access. If it is not provisioned, review and apply the canonical infra/terraform plan first." >&2
  exit 1
fi
if [[ "$CLUSTER_STATUS" != "ACTIVE" ]]; then
  echo "ERROR: EKS cluster ${CLUSTER_NAME} is ${CLUSTER_STATUS}, not ACTIVE; wait for the canonical infra/terraform owner state to finish before deploying staging." >&2
  exit 1
fi

# The shared cluster and staging evidence resources have separate Terraform states.
# Check the staging state before creating any Kubernetes resources so a missing
# database or queue fails with an actionable message instead of an empty SecretId.
if ! STAGING_OUTPUTS="$(terraform -chdir="$TERRAFORM_DIR" output -json)"; then
  echo "ERROR: cannot read staging Terraform outputs from ${TERRAFORM_DIR}. Run terraform init there and check its backend access." >&2
  exit 1
fi
if ! python3 -c '
import json, sys
outputs = json.load(sys.stdin)
required = (
    "staging_db_endpoint",
    "staging_db_secret_arn",
    "staging_evidence_queue_url",
    "staging_evidence_bucket_name",
    "staging_evidence_kms_key_arn",
    "staging_evidence_worker_irsa_role_arn",
    "staging_evidence_drain_consumer_irsa_role_arn",
)
missing = [name for name in required if not outputs.get(name, {}).get("value")]
if missing:
    print("ERROR: missing staging Terraform outputs: " + ", ".join(missing), file=sys.stderr)
    sys.exit(1)
' <<< "$STAGING_OUTPUTS"; then
  echo "Review and apply staging Terraform with: bash scripts/apply_staging_terraform.sh plan; bash scripts/apply_staging_terraform.sh apply" >&2
  exit 1
fi

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

echo "--- 1c. Qdrant (required by lifespan()'s eager ensure_collection() call; not otherwise used) ---"
kubectl apply -f "${ROOT_DIR}/deploy/k8s/staging-qdrant.yaml"
kubectl rollout status deployment/qdrant --namespace "$NAMESPACE" --timeout=5m

echo "--- 1d. OTEL collector ---"
kubectl apply -f "${ROOT_DIR}/deploy/k8s/staging-otel-collector.yaml"
kubectl rollout status deployment/otel-collector --namespace "$NAMESPACE" --timeout=5m

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

# EVAL_EVIDENCE_ENABLED/EVAL_CAPTURE_CONTENT here (not just in the API's Helm values below) are
# what let the evidence-worker Deployment actually deliver: evidence-delivery-worker.yaml pulls
# its base env via `envFrom: secretRef: app-env-secret`, so worker.main() only stops exiting 2
# once these two keys exist in this secret. Metadata-only delivery is the validated shape in
# staging — content capture stays off everywhere in PR 5a.
STAGING_SECRET_ARGS=(
  --from-literal=DATABASE_URL="$STAGING_DATABASE_URL" \
  --from-literal=JWT_SECRET_KEY="$STAGING_JWT_SECRET" \
  --from-literal=EVAL_IDENTITY_HASH_KEY="$STAGING_IDENTITY_KEY" \
  --from-literal=EVAL_EVIDENCE_ENABLED="true" \
  --from-literal=EVAL_CAPTURE_CONTENT="false" \
  --from-literal=REDIS_URL="$STAGING_REDIS_URL" \
  --from-literal=NEO4J_PASSWORD="unused-in-staging-evidence" \
  --from-literal=S3_BUCKET_NAME="unused-in-staging-evidence"
)
# Provider credentials are supplied by the operator's environment (for example, from a
# protected .env file) and stored only in the namespace Secret—not in Helm values or a ConfigMap.
for provider_key in LLM_API_KEY EMBED_API_KEY RERANK_API_KEY; do
  if [[ -n "${!provider_key:-}" ]]; then
    STAGING_SECRET_ARGS+=("--from-literal=${provider_key}=${!provider_key}")
  fi
done
kubectl create secret generic app-env-secret --namespace "$NAMESPACE" \
  "${STAGING_SECRET_ARGS[@]}" --dry-run=client -o yaml | kubectl apply -f -

echo "--- 3. Staging API Helm release ---"
STAGING_EVIDENCE_QUEUE_URL="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_evidence_queue_url)"

# Helm's list-valued `env` field cannot be safely patched by scalar `--set` calls. The chart's
# deployment template renders every env entry without de-duplicating names, so an omitted default
# could accidentally route staging to production Ray endpoints. This values file replaces the
# complete env list; it also allows explicit hosted endpoints while retaining fake-models by default.
STAGING_VALUES_FILE="$(mktemp)"
trap 'rm -f "$STAGING_VALUES_FILE"' EXIT

cat > "$STAGING_VALUES_FILE" <<EOF
secretEnvKeys:
  - DATABASE_URL
  - REDIS_URL
  - NEO4J_PASSWORD
  - S3_BUCKET_NAME
  - JWT_SECRET_KEY
  - LLM_API_KEY
  - EMBED_API_KEY
  - RERANK_API_KEY
  - TAVILY_API_KEY
  - EVAL_IDENTITY_HASH_KEY
env:
  - name: ENV
    value: "staging"
  - name: LOG_LEVEL
    value: "INFO"
  - name: QDRANT_HOST
    value: "qdrant"
  - name: QDRANT_PORT
    value: "6333"
  - name: QDRANT_COLLECTION
    value: "rag_collection"
  - name: NEO4J_URI
    value: "bolt://neo4j-cluster:7687"
  - name: NEO4J_USER
    value: "neo4j"
  - name: AWS_REGION
    value: "${REGION}"
  - name: RAY_LLM_ENDPOINT
    value: "http://fake-models:8000/v1/chat/completions"
  - name: RAY_EMBED_ENDPOINT
    value: "http://fake-models:8000/api/embeddings"
  - name: LLM_API_ENDPOINT
    value: "${LLM_API_ENDPOINT:-}"
  - name: LLM_MODEL_NAME
    value: "${LLM_MODEL_NAME:-llama3.2:3b}"
  - name: EMBED_API_ENDPOINT
    value: "${EMBED_API_ENDPOINT:-}"
  - name: EMBED_DIM
    value: "${EMBED_DIM:-1024}"
  - name: EMBED_MODEL_NAME
    value: "${EMBED_MODEL_NAME:-BAAI/bge-m3}"
  - name: RERANK_API_ENDPOINT
    value: "${RERANK_API_ENDPOINT:-}"
  - name: RERANK_MODEL_NAME
    value: "${RERANK_MODEL_NAME:-}"
  - name: LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED
    value: "${LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED:-false}"
  - name: JWT_ALGORITHM
    value: "HS256"
  - name: EVAL_EVIDENCE_ENABLED
    value: "true"
  - name: EVAL_CAPTURE_CONTENT
    value: "false"
  - name: EVAL_QUEUE_URL
    value: "${STAGING_EVIDENCE_QUEUE_URL}"
  - name: OTEL_SERVICE_NAME
    value: "rag-api-service"
  - name: OTEL_EXPORTER_OTLP_ENDPOINT
    value: "http://otel-collector:4317"
EOF

helm upgrade --install api-staging "${ROOT_DIR}/deploy/helm/api" \
  --namespace "$NAMESPACE" \
  --set image.repository="${ECR_BASE}/services/api" \
  --set image.tag="${API_IMAGE_TAG}" \
  --set replicaCount=1 \
  --set fullnameOverride=api-staging \
  -f "$STAGING_VALUES_FILE" \
  --wait --timeout 5m

rm -f "$STAGING_VALUES_FILE"
trap - EXIT

echo "--- 4. Evidence delivery components ---"
# Clear any pre-exported production values from the operator's shell: deploy_evidence_delivery.sh
# prefers these over freshly-fetched Terraform outputs ("${VAR:-$(terraform ...)}"), so a stale
# export left over from a prior production deploy in the same shell session would otherwise leak
# production queue/bucket/KMS/role/image/limit values into this staging deploy.
unset EVIDENCE_QUEUE_URL EVIDENCE_BUCKET_NAME EVIDENCE_KMS_KEY_ID EVIDENCE_WORKER_ROLE_ARN EVIDENCE_CONSUMER_ROLE_ARN API_IMAGE_TAG MAINTENANCE_LIMIT

# Unlike production's dark launch, staging's app-env-secret already has EVAL_EVIDENCE_ENABLED=true
# (step 2 above) — the worker needs to actually run here to validate delivery, so override the
# 0-replica dark-launch default.
NAMESPACE="$NAMESPACE" TERRAFORM_DIR="$TERRAFORM_DIR" WORKER_REPLICAS=1 bash "${SCRIPT_DIR}/deploy_evidence_delivery.sh"

echo "--- Staging evidence stack deployed ---"
echo "Port-forward the staging API: kubectl port-forward -n ${NAMESPACE} deployment/api-staging 18000:8000"
