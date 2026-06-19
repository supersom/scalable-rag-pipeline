#!/bin/bash
# scripts/build_push_image.sh
#
# Builds a service Docker image from the current git HEAD and uploads it to S3.
# If a git SHA is provided, exports that exact ECR image to S3 instead.
#
# Usage:
#   bash scripts/build_push_image.sh api
#   bash scripts/build_push_image.sh ray
#   bash scripts/build_push_image.sh ingestion
#   bash scripts/build_push_image.sh api <git-sha>
#   bash scripts/build_push_image.sh ray <git-sha>
#   bash scripts/build_push_image.sh ingestion <git-sha>
#

set -euo pipefail

SERVICE="${1:?Usage: build_push_image.sh <api|ray|ingestion> [git-sha]}"
GIT_SHA="${2:-}"

REGION="us-east-1"
MODELS_BUCKET="${MODELS_BUCKET:-rag-platform-models-prod-7649}"
DEFAULT_SHA=$(git rev-parse --short HEAD)

case "$SERVICE" in
  api)
    DOCKERFILE="services/api/Dockerfile"
    ECR_REPO="services/api"
    ;;
  ray)
    DOCKERFILE="services/models/Dockerfile"
    ECR_REPO="services/ray-serve"
    ;;
  ingestion)
    DOCKERFILE="services/ingestion/Dockerfile"
    ECR_REPO="services/ingestion"
    ;;
  *)
    echo "ERROR: unknown service '${SERVICE}'. Must be 'api', 'ray', or 'ingestion'." >&2
    exit 1
    ;;
esac

ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

if [[ -n "$GIT_SHA" ]]; then
  S3_KEY="images/${ECR_REPO}/${GIT_SHA}.tar.gz"
  S3_URI="s3://${MODELS_BUCKET}/${S3_KEY}"
  ECR_TAG="${ECR_BASE}/${ECR_REPO}:${GIT_SHA}"
  IMAGE_REF="${ECR_TAG}"
else
  GIT_SHA="$DEFAULT_SHA"
  S3_KEY="images/${ECR_REPO}/${GIT_SHA}.tar.gz"
  S3_URI="s3://${MODELS_BUCKET}/${S3_KEY}"
  IMAGE_REF="${ECR_REPO}:${GIT_SHA}"
fi

echo "--- Service   : ${SERVICE}"
echo "--- Git SHA   : ${GIT_SHA}"
echo "--- S3 target : ${S3_URI}"

if [[ -n "${2:-}" ]]; then
  echo "--- Source    : ECR"
  echo "--- ECR image : ${ECR_TAG}"

  if ! aws ecr describe-images     --repository-name "${ECR_REPO}"     --image-ids "imageTag=${GIT_SHA}"     --region "${REGION}"     >/dev/null; then
    echo "ERROR: ${ECR_TAG} does not exist in ECR. Nothing uploaded." >&2
    exit 1
  fi

  echo "--- 1. Pull ---"
  aws ecr get-login-password --region "${REGION}" |     docker login --username AWS --password-stdin "${ECR_BASE}"
  docker pull "${ECR_TAG}"
else
  echo "--- Dockerfile: ${DOCKERFILE}"
  echo "--- Source    : local build"
  echo "--- 1. Build ---"
  docker build -t "${IMAGE_REF}" -f "${DOCKERFILE}" .
fi

echo "--- 2. Find previous versions ---"
PREV_KEYS=$(aws s3 ls "s3://${MODELS_BUCKET}/images/${ECR_REPO}/" --region "${REGION}" \
  | awk '{print $4}' \
  | grep '\.tar\.gz$' \
  | grep -v "^${GIT_SHA}\.tar\.gz$")

echo "--- 3. Save + upload to S3 ---"
docker save "${IMAGE_REF}" | gzip | \
  aws s3 cp - "${S3_URI}" \
    --region "${REGION}" \
    --no-progress

if [[ -n "$PREV_KEYS" ]]; then
  echo "--- 4. Delete previous versions ---"
  while IFS= read -r key; do
    echo "    Deleting: ${key}"
    aws s3 rm "s3://${MODELS_BUCKET}/images/${ECR_REPO}/${key}" --region "${REGION}"
  done <<< "$PREV_KEYS"
else
  echo "--- 4. No previous versions to delete ---"
fi

echo "--- Done: ${IMAGE_REF} → ${S3_URI} ---"
