#!/bin/bash
# scripts/build_push_image.sh
#
# Builds a service Docker image from the current git HEAD and uploads it to S3.
# Use sync_s3_to_ecr.sh to promote the image from S3 to ECR.
#
# Usage:
#   bash scripts/build_push_image.sh api
#   bash scripts/build_push_image.sh ray
#   bash scripts/build_push_image.sh ingestion

set -euo pipefail

SERVICE="${1:?Usage: build_push_image.sh <api|ray|ingestion>}"

REGION="us-east-1"
MODELS_BUCKET="rag-platform-models-prod-7649"
GIT_SHA=$(git rev-parse --short HEAD)

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

S3_KEY="images/${ECR_REPO}/${GIT_SHA}.tar.gz"
S3_URI="s3://${MODELS_BUCKET}/${S3_KEY}"
LOCAL_TAG="${ECR_REPO}:${GIT_SHA}"

echo "--- Service   : ${SERVICE}"
echo "--- Git SHA   : ${GIT_SHA}"
echo "--- Dockerfile: ${DOCKERFILE}"
echo "--- S3 target : ${S3_URI}"

echo "--- 1. Build ---"
docker build -t "${LOCAL_TAG}" -f "${DOCKERFILE}" .

echo "--- 2. Save + upload to S3 ---"
docker save "${LOCAL_TAG}" | gzip | \
  aws s3 cp - "${S3_URI}" \
    --region "${REGION}" \
    --no-progress

echo "--- Done: ${LOCAL_TAG} → ${S3_URI} ---"
