#!/bin/bash

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAMESPACE="${NAMESPACE:-default}"
UI_IMAGE_TAG="${UI_IMAGE_TAG:?UI_IMAGE_TAG is required}"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
UI_IMAGE="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/services/ui:${UI_IMAGE_TAG}"
ROOT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

aws ecr describe-images \
  --repository-name services/ui \
  --image-ids "imageTag=${UI_IMAGE_TAG}" \
  --region "${REGION}" \
  >/dev/null

kubectl get secret app-env-secret --namespace "${NAMESPACE}" >/dev/null

sed "s|UI_IMAGE|${UI_IMAGE}|g" "${ROOT_DIR}/deploy/k8s/ui.yaml" \
  | kubectl apply --namespace "${NAMESPACE}" -f -

kubectl rollout status deployment/ui \
  --namespace "${NAMESPACE}" \
  --timeout=5m

kubectl get deployment/ui service/ui-service \
  --namespace "${NAMESPACE}" \
  -o wide
