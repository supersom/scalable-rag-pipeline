#!/bin/bash
# scripts/sync_s3_to_ecr.sh
#
# Promotes Docker images from S3 to ECR. Loads the gzipped tarball from S3,
# retags it with the full ECR URI, and pushes.
#
# Usage:
#   bash scripts/sync_s3_to_ecr.sh api <git-sha>
#   bash scripts/sync_s3_to_ecr.sh ray <git-sha>
#   bash scripts/sync_s3_to_ecr.sh ingestion <git-sha>
#
# To find available SHAs in S3:
#   aws s3 ls s3://rag-platform-models-prod-7649/images/services/api/
#   aws s3 ls s3://rag-platform-models-prod-7649/images/services/ray-serve/

set -euo pipefail

SERVICE="${1:?Usage: sync_s3_to_ecr.sh <api|ray|ingestion> <git-sha>}"
GIT_SHA="${2:?Usage: sync_s3_to_ecr.sh <api|ray|ingestion> <git-sha>}"

REGION="us-east-1"
MODELS_BUCKET="rag-platform-models-prod-7649"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

case "$SERVICE" in
  api)  ECR_REPO="services/api" ;;
  ray)  ECR_REPO="services/ray-serve" ;;
  ingestion) ECR_REPO="services/ingestion" ;;
  *)
    echo "ERROR: unknown service '${SERVICE}'. Must be 'api', 'ray', or 'ingestion'." >&2
    exit 1
    ;;
esac

S3_URI="s3://${MODELS_BUCKET}/images/${ECR_REPO}/${GIT_SHA}.tar.gz"
LOCAL_TAG="${ECR_REPO}:${GIT_SHA}"
ECR_TAG="${ECR_BASE}/${ECR_REPO}:${GIT_SHA}"

echo "--- Service   : ${SERVICE}"
echo "--- Git SHA   : ${GIT_SHA}"
echo "--- S3 source : ${S3_URI}"
echo "--- ECR target: ${ECR_TAG}"

echo "--- 1. ECR login ---"
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ECR_BASE}"

echo "--- 2. Download + load from S3 ---"
aws s3 cp "${S3_URI}" - --region "${REGION}" --no-progress | \
  gunzip | docker load

echo "--- 3. Retag for ECR ---"
docker tag "${LOCAL_TAG}" "${ECR_TAG}"

echo "--- 4. Push to ECR ---"
docker push "${ECR_TAG}"

echo "--- Done: ${ECR_TAG} ---"
