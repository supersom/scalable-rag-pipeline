#!/usr/bin/env bash
# Plan or apply the staging state with a stable evidence-bucket choice.
# The selected mode is saved as a Terraform output after the first apply.
# Usage: bash scripts/apply_staging_terraform.sh [plan|apply]

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd -- "${SCRIPT_DIR}/.." && pwd)
TERRAFORM_DIR="${ROOT_DIR}/infra/terraform-staging"
ACTION="${1:-plan}"
REGION="${REGION:-us-east-1}"
CLUSTER_NAME="${CLUSTER_NAME:-rag-platform-cluster}"

if [[ $# -gt 1 || ( "$ACTION" != plan && "$ACTION" != apply ) ]]; then
  echo "Usage: bash scripts/apply_staging_terraform.sh [plan|apply]" >&2
  exit 2
fi
if [[ ! "$CLUSTER_NAME" =~ ^[a-z0-9-]+$ ]]; then
  echo "ERROR: CLUSTER_NAME must contain only lowercase letters, digits, and hyphens for the S3 bucket name." >&2
  exit 2
fi
for command in aws terraform; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: ${command}" >&2
    exit 1
  }
done

BUCKET_NAME="${CLUSTER_NAME}-evidence-staging-7649"
terraform -chdir="$TERRAFORM_DIR" init -input=false >/dev/null

# A created bucket remains a managed resource in state. A reused bucket stays
# outside state, with the chosen mode recorded in an output after apply.
if ! STATE_RESOURCES="$(terraform -chdir="$TERRAFORM_DIR" state list)"; then
  echo "ERROR: cannot read staging Terraform state." >&2
  exit 1
fi
PINNED_MODE="$(terraform -chdir="$TERRAFORM_DIR" output -raw staging_evidence_bucket_mode 2>/dev/null || true)"
case "$PINNED_MODE" in
  ""|existing|create) ;;
  *) echo "ERROR: unexpected persisted bucket mode: ${PINNED_MODE}" >&2; exit 1 ;;
esac

if [[ "$STATE_RESOURCES" == *'aws_s3_bucket.staging_evidence[0]'* ]]; then
  if [[ "$PINNED_MODE" == existing ]]; then
    echo "ERROR: staging state contains a managed bucket but records existing mode; reconcile state before applying." >&2
    exit 1
  fi
  MODE=create
elif [[ "$PINNED_MODE" == create ]]; then
  echo "ERROR: staging state records create mode but has no managed bucket; reconcile state before applying." >&2
  exit 1
elif [[ "$PINNED_MODE" == existing ]]; then
  MODE=existing
else
  # List only buckets owned by these AWS credentials. A same-name bucket owned
  # elsewhere will cause Terraform's create operation to fail, not be adopted.
  BUCKET_COUNT="$(aws s3api list-buckets --region "$REGION" \
    --query "length(Buckets[?Name=='${BUCKET_NAME}'])" --output text)"
  case "$BUCKET_COUNT" in
    0) MODE=create ;;
    1) MODE=existing ;;
    *) echo "ERROR: unexpected S3 bucket lookup result: ${BUCKET_COUNT}" >&2; exit 1 ;;
  esac
fi

TF_VARS=(
  "-var=cluster_name=${CLUSTER_NAME}"
  "-var=aws_region=${REGION}"
  "-var=staging_evidence_bucket_mode=${MODE}"
)
if [[ "$MODE" == existing ]]; then
  # Existing evidence buckets must already use an ARN-valued SSE-KMS key.
  # The API and bucket write policy both need that exact key ARN.
  if ! KMS_KEY_ARN="$(aws s3api get-bucket-encryption --bucket "$BUCKET_NAME" --region "$REGION" \
    --query 'ServerSideEncryptionConfiguration.Rules[0].ApplyServerSideEncryptionByDefault.KMSMasterKeyID' \
    --output text)"; then
    echo "ERROR: cannot read SSE-KMS configuration for existing bucket ${BUCKET_NAME}; the pinned mode will not switch to create." >&2
    exit 1
  fi
  if [[ "$KMS_KEY_ARN" != arn:aws*:kms:* ]]; then
    echo "ERROR: existing bucket ${BUCKET_NAME} has no ARN-valued SSE-KMS key." >&2
    exit 1
  fi
  TF_VARS+=("-var=staging_evidence_existing_kms_key_arn=${KMS_KEY_ARN}")
fi

echo "Staging evidence bucket: ${BUCKET_NAME} (${MODE}; persisted in Terraform state after apply)"
terraform -chdir="$TERRAFORM_DIR" "$ACTION" -input=false "${TF_VARS[@]}"
