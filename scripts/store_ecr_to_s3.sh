#!/bin/bash
# scripts/store_ecr_to_s3.sh
#
# Thin wrapper around scripts/build_push_image.sh for exporting an existing ECR
# image to S3.
#
# Usage:
#   bash scripts/store_ecr_to_s3.sh api
#   bash scripts/store_ecr_to_s3.sh ray
#   bash scripts/store_ecr_to_s3.sh ingestion
#   bash scripts/store_ecr_to_s3.sh api <git-sha>
#   bash scripts/store_ecr_to_s3.sh ray <git-sha>
#   bash scripts/store_ecr_to_s3.sh ingestion <git-sha>

set -euo pipefail

SERVICE="${1:?Usage: store_ecr_to_s3.sh <api|ray|ingestion> [git-sha]}"
GIT_SHA="${2:-}"
REGION="${REGION:-us-east-1}"

case "$SERVICE" in
  api)
    ECR_REPO="services/api"
    ;;
  ray)
    ECR_REPO="services/ray-serve"
    ;;
  ingestion)
    ECR_REPO="services/ingestion"
    ;;
  *)
    echo "ERROR: unknown service '${SERVICE}'. Must be 'api', 'ray', or 'ingestion'." >&2
    exit 1
    ;;
esac

if [[ -z "$GIT_SHA" ]]; then
  GIT_SHA=$(aws ecr describe-images     --repository-name "$ECR_REPO"     --region "$REGION"     --query 'sort_by(imageDetails, &imagePushedAt)[-1].imageTags[0]'     --output text)

  if [[ -z "$GIT_SHA" || "$GIT_SHA" == "None" ]]; then
    echo "ERROR: no tagged images found in ${ECR_REPO}." >&2
    exit 1
  fi
fi

exec "$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)/build_push_image.sh" "$SERVICE" "$GIT_SHA"
