#!/bin/bash
# Deploy or update the evidence delivery components (migration job, delivery worker, drain
# consumer, maintenance CronJob) in an existing cluster. Mirrors scripts/deploy_ingestion.sh.
#
# Required infrastructure:
#   - Terraform evidence resources have been applied (infra/terraform or infra/terraform-staging).
#   - The services/api image has been pushed to ECR (it hosts the worker/consumer/maintenance
#     entrypoints, not a separate image).
#   - app-env-secret exists in the target namespace with DATABASE_URL and EVAL_IDENTITY_HASH_KEY set.
#
# EVAL_EVIDENCE_ENABLED is NOT set by this script. Ships dark: the worker Deployment defaults to
# WORKER_REPLICAS=0 (override via the env var) so it can't crash-loop while delivery is disabled;
# a separate, explicit change flips the flag in app-env-secret and scales the Deployment up
# together (see Task 14). Callers that already enable delivery (deploy_staging_evidence.sh) pass
# WORKER_REPLICAS=1.
#
# All values can be exported explicitly. When omitted, this script derives them from AWS/ECR
# and Terraform outputs.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)

REGION="${REGION:-us-east-1}"
NAMESPACE="${NAMESPACE:-default}"
TERRAFORM_DIR="${TERRAFORM_DIR:-${ROOT_DIR}/infra/terraform}"
MAINTENANCE_LIMIT="${MAINTENANCE_LIMIT:-100}"
# Dark-launch default: production ships with the worker at 0 replicas since EVAL_EVIDENCE_ENABLED
# is unset there (see deploy/k8s/evidence-delivery-worker.yaml). Callers that already enable
# delivery in app-env-secret (deploy_staging_evidence.sh) override this to 1.
WORKER_REPLICAS="${WORKER_REPLICAS:-0}"

for command in aws kubectl terraform sed; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

ACCOUNT_ID="${ACCOUNT_ID:-$(aws sts get-caller-identity --query Account --output text)}"
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

API_IMAGE_TAG="${API_IMAGE_TAG:-$(aws ecr describe-images \
  --repository-name services/api \
  --region "$REGION" \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]' \
  --output text)}"

if [[ -z "$API_IMAGE_TAG" || "$API_IMAGE_TAG" == "None" ]]; then
  echo "ERROR: no services/api image tag found in ECR." >&2
  exit 1
fi

API_IMAGE="${ECR_BASE}/services/api:${API_IMAGE_TAG}"

QUEUE_URL_OUTPUT="evidence_queue_url"
BUCKET_OUTPUT="evidence_bucket_name"
WORKER_ROLE_OUTPUT="evidence_worker_irsa_role_arn"
CONSUMER_ROLE_OUTPUT="evidence_drain_consumer_irsa_role_arn"
KMS_KEY_OUTPUT="evidence_kms_key_arn"
if [[ "$TERRAFORM_DIR" == *terraform-staging ]]; then
  QUEUE_URL_OUTPUT="staging_evidence_queue_url"
  BUCKET_OUTPUT="staging_evidence_bucket_name"
  WORKER_ROLE_OUTPUT="staging_evidence_worker_irsa_role_arn"
  CONSUMER_ROLE_OUTPUT="staging_evidence_drain_consumer_irsa_role_arn"
  KMS_KEY_OUTPUT="staging_evidence_kms_key_arn"
fi

EVIDENCE_QUEUE_URL="${EVIDENCE_QUEUE_URL:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$QUEUE_URL_OUTPUT")}"
EVIDENCE_BUCKET_NAME="${EVIDENCE_BUCKET_NAME:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$BUCKET_OUTPUT")}"
EVIDENCE_WORKER_ROLE_ARN="${EVIDENCE_WORKER_ROLE_ARN:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$WORKER_ROLE_OUTPUT")}"
EVIDENCE_CONSUMER_ROLE_ARN="${EVIDENCE_CONSUMER_ROLE_ARN:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$CONSUMER_ROLE_OUTPUT")}"
EVIDENCE_KMS_KEY_ID="${EVIDENCE_KMS_KEY_ID:-$(terraform -chdir="$TERRAFORM_DIR" output -raw "$KMS_KEY_OUTPUT")}"

kubectl cluster-info >/dev/null
kubectl get secret app-env-secret --namespace "$NAMESPACE" >/dev/null

echo "--- Evidence delivery deployment ---"
echo "Namespace     : ${NAMESPACE}"
echo "Image         : ${API_IMAGE}"
echo "Queue         : ${EVIDENCE_QUEUE_URL}"
echo "Bucket        : ${EVIDENCE_BUCKET_NAME}"
echo "KMS key       : ${EVIDENCE_KMS_KEY_ID}"
echo "Worker role   : ${EVIDENCE_WORKER_ROLE_ARN}"
echo "Consumer role : ${EVIDENCE_CONSUMER_ROLE_ARN}"
echo "Worker replicas: ${WORKER_REPLICAS}"

echo "--- 1. Service accounts ---"
for name_role in "evidence-worker:${EVIDENCE_WORKER_ROLE_ARN}" "evidence-drain-consumer:${EVIDENCE_CONSUMER_ROLE_ARN}"; do
  sa="${name_role%%:*}"
  role="${name_role##*:}"
  kubectl create serviceaccount "$sa" --namespace "$NAMESPACE" \
    --dry-run=client -o yaml | kubectl apply -f -
  kubectl annotate serviceaccount "$sa" --namespace "$NAMESPACE" \
    "eks.amazonaws.com/role-arn=${role}" --overwrite
done

echo "--- 2. Migration job ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-migrate-job.yaml" | kubectl apply -f -

kubectl wait job/evidence-migrate --namespace "$NAMESPACE" --for=condition=complete --timeout=5m

echo "--- 3. Delivery worker ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  -e "s|EVIDENCE_QUEUE_URL_VALUE|${EVIDENCE_QUEUE_URL}|g" \
  -e "s|WORKER_REPLICAS_VALUE|${WORKER_REPLICAS}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-delivery-worker.yaml" | kubectl apply -f -

kubectl rollout status deployment/evidence-worker --namespace "$NAMESPACE" --timeout=5m

echo "--- 4. Drain consumer ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  -e "s|EVIDENCE_QUEUE_URL_VALUE|${EVIDENCE_QUEUE_URL}|g" \
  -e "s|EVIDENCE_BUCKET_NAME_VALUE|${EVIDENCE_BUCKET_NAME}|g" \
  -e "s|EVIDENCE_KMS_KEY_ID_VALUE|${EVIDENCE_KMS_KEY_ID}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-drain-consumer.yaml" | kubectl apply -f -

kubectl rollout status deployment/evidence-drain-consumer --namespace "$NAMESPACE" --timeout=5m

echo "--- 5. Maintenance CronJob ---"
sed \
  -e "s|namespace: default|namespace: ${NAMESPACE}|" \
  -e "s|API_IMAGE|${API_IMAGE}|g" \
  -e "s|MAINTENANCE_LIMIT_VALUE|${MAINTENANCE_LIMIT}|g" \
  "${ROOT_DIR}/deploy/k8s/evidence-maintenance-cronjob.yaml" | kubectl apply -f -

echo "--- Evidence delivery deployment complete ---"
echo "EVAL_EVIDENCE_ENABLED was not touched — delivery stays disabled until app-env-secret is updated explicitly."
echo "evidence-worker replicas: ${WORKER_REPLICAS} — Task 14 must scale this up in production at the same time it flips EVAL_EVIDENCE_ENABLED."
echo "Watch worker logs:   kubectl logs -f deployment/evidence-worker -n ${NAMESPACE}"
echo "Watch consumer logs: kubectl logs -f deployment/evidence-drain-consumer -n ${NAMESPACE}"
