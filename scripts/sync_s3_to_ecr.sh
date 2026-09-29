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
#   bash scripts/sync_s3_to_ecr.sh fake-models <git-sha>
#
# To find available SHAs in S3:
#   aws s3 ls s3://rag-platform-models-prod-7649/images/services/api/
#   aws s3 ls s3://rag-platform-models-prod-7649/images/services/ray-serve/

set -euo pipefail

SERVICE="${1:?Usage: sync_s3_to_ecr.sh <api|ray|ingestion|fake-models> <git-sha>}"
GIT_SHA="${2:?Usage: sync_s3_to_ecr.sh <api|ray|ingestion|fake-models> <git-sha>}"

REGION="us-east-1"
MODELS_BUCKET="rag-platform-models-prod-7649"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ECR_BASE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

case "$SERVICE" in
  api)  ECR_REPO="services/api" ;;
  ray)  ECR_REPO="services/ray-serve" ;;
  ingestion) ECR_REPO="services/ingestion" ;;
  fake-models) ECR_REPO="services/fake-models" ;;
  *)
    echo "ERROR: unknown service '${SERVICE}'. Must be 'api', 'ray', 'ingestion', or 'fake-models'." >&2
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

echo "--- 5. Delete older ECR images ---"
# Docker's default buildx builder attaches a provenance manifest, so every
# push is an OCI index (the tagged, top-level image) referencing untagged
# per-platform/attestation children. Filtering on "not tagged with GIT_SHA"
# alone — as this used to — matches those untagged children of the image
# we JUST pushed too (they carry no tag at all, so the check trivially
# passes), and ECR only refused to delete them because they're still
# ImageReferencedByManifestList by the current tag. Two-phase delete instead:
# first the stale top-level tags, then their now-orphaned children.

delete_digests() {
  # $1 = whitespace/newline-separated digests. Prints the batch-delete-image
  # JSON response, or {"imageIds":[],"failures":[]} if given nothing.
  local digests="$1"
  if [[ -z "$digests" ]]; then
    echo '{"imageIds":[],"failures":[]}'
    return
  fi
  local image_ids
  image_ids=$(echo "$digests" | tr '\t\n' ' ' | xargs -n1 | \
    python3 -c "import sys,json; print(json.dumps([{'imageDigest':d.strip()} for d in sys.stdin if d.strip()]))")
  aws ecr batch-delete-image \
    --repository-name "${ECR_REPO}" \
    --region "${REGION}" \
    --image-ids "${image_ids}"
}

OLD_TAGGED_DIGESTS=$(aws ecr describe-images \
  --repository-name "${ECR_REPO}" \
  --region "${REGION}" \
  --query "imageDetails[?imageTags && !contains(imageTags, '${GIT_SHA}')].imageDigest" \
  --output text)

if [[ -n "$OLD_TAGGED_DIGESTS" ]]; then
  delete_digests "$OLD_TAGGED_DIGESTS" >/dev/null
  echo "    Deleted $(echo "$OLD_TAGGED_DIGESTS" | wc -w | tr -d ' ') old tagged image(s)"
else
  echo "    No older tagged images to delete"
fi

# Orphaned children of the tags just deleted are now unreferenced and safe to
# remove. Children still belonging to a kept tag (e.g. the one just pushed)
# fail with ImageReferencedByManifestList — that's the expected, correct
# outcome, not an error; only a different failure code is worth surfacing.
UNTAGGED_DIGESTS=$(aws ecr describe-images \
  --repository-name "${ECR_REPO}" \
  --region "${REGION}" \
  --filter tagStatus=UNTAGGED \
  --query 'imageDetails[].imageDigest' \
  --output text)

if [[ -n "$UNTAGGED_DIGESTS" ]]; then
  RESPONSE=$(delete_digests "$UNTAGGED_DIGESTS")
  echo "$RESPONSE" | python3 -c "
import sys, json
data = json.load(sys.stdin)
deleted = len(data.get('imageIds', []))
failures = data.get('failures', [])
kept = [f for f in failures if f.get('failureCode') == 'ImageReferencedByManifestList']
other = [f for f in failures if f.get('failureCode') != 'ImageReferencedByManifestList']
print(f'    Deleted {deleted} orphaned untagged manifest(s); {len(kept)} still referenced by a kept image (expected)')
for f in other:
    print(f\"    WARNING: {f['imageId']['imageDigest']}: {f['failureCode']} — {f['failureReason']}\")
"
else
  echo "    No untagged images to clean up"
fi

echo "--- Done: ${ECR_TAG} ---"
