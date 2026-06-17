#!/bin/bash
# scripts/bootstrap_cluster.sh

set -e # Exit on error

CLUSTER_NAME="rag-platform-cluster"
REGION="us-east-1"

echo "🔹 1. Updating Kubeconfig..."
aws eks update-kubeconfig --name $CLUSTER_NAME --region $REGION

echo "🔹 2. Installing KubeRay Operator..."
helm repo add kuberay https://ray-project.github.io/kuberay-helm/
helm repo add qdrant https://qdrant.github.io/qdrant-helm/
helm repo update
helm upgrade --install kuberay-operator kuberay/kuberay-operator --version 1.0.0

echo "🔹 3. Installing Vector DB (Qdrant)..."
helm upgrade --install qdrant qdrant/qdrant -f deploy/helm/qdrant/values.yaml

echo "🔹 4. Deploying Ray Cluster (This spawns the Head Node)..."
kubectl apply -f deploy/ray/ray-cluster.yaml

echo "🔹 5. Waiting for Ray Cluster to be ready..."
sleep 30

echo "🔹 6. Deploying AI Engines (vLLM & Embeddings)..."
# These trigger Karpenter to buy GPUs
kubectl apply -f deploy/ray/ray-serve-llm.yaml
kubectl apply -f deploy/ray/ray-serve-embed.yaml

echo "🔹 7. Deploying API Gateway (Ingress)..."
kubectl apply -f deploy/ingress/nginx.yaml

echo "🔹 8. Deploying Backend API..."
if [ ! -f deploy/helm/api/Chart.yaml ]; then
    echo "ERROR: Missing deploy/helm/api/Chart.yaml. The API Helm chart is not present in this repo; build/add it before deploying the API." >&2
    exit 1
fi
if [ -z "${API_IMAGE_REPOSITORY:-}" ]; then
    echo "ERROR: API_IMAGE_REPOSITORY is required. Build and push the API image, then rerun with API_IMAGE_REPOSITORY=<repo> API_IMAGE_TAG=<tag>." >&2
    echo "Example build: docker build -f services/api/Dockerfile -t <repo>:<tag> ." >&2
    exit 1
fi
helm upgrade --install api deploy/helm/api \
    --set image.repository="$API_IMAGE_REPOSITORY" \
    --set image.tag="${API_IMAGE_TAG:-latest}"

echo "✅ Cluster Bootstrap Complete! Monitor pods with: kubectl get pods"
