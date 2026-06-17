#!/bin/bash
# scripts/cleanup.sh
set -euo pipefail

echo "⚠️  WARNING: THIS WILL DESTROY ALL CLOUD RESOURCES ⚠️"
echo "Includes: EKS Cluster, Databases (RDS/Neo4j/Redis), S3 Buckets, Load Balancers."
echo "Cost-saving measure for Dev/Test environments."
echo ""
read -p "Are you sure? Type 'DESTROY': " confirm

if [ "$confirm" != "DESTROY" ]; then
    echo "Aborted."
    exit 1
fi

# RayServices must go first — Karpenter won't terminate GPU nodes until Ray
# pods are gone, and GPU nodes must be gone before VPC deletion can succeed.
echo "🔹 1. Deleting RayServices (drains GPU nodes)..."
kubectl delete rayservice --all -n default --ignore-not-found

echo "🔹 2. Deleting dead nodeclaims..."
kubectl delete nodeclaim cpu-q2z2n --ignore-not-found || true

echo "🔹 3. Waiting for GPU nodeclaims to terminate (up to 10 min)..."
for i in $(seq 1 60); do
    remaining=$(kubectl get nodeclaim -o name 2>/dev/null | wc -l)
    [ "$remaining" -eq 0 ] && break
    echo "  ${remaining} nodeclaim(s) still present, waiting..."
    sleep 10
done

echo "🔹 4. Helm uninstall (API, Qdrant, Neo4j, KubeRay operator)..."
helm uninstall api            --ignore-not-found || true
helm uninstall qdrant         --ignore-not-found || true
helm uninstall neo4j-cluster  --ignore-not-found || true
helm uninstall kuberay-operator --ignore-not-found || true

echo "🔹 5. Deleting remaining kubectl resources..."
kubectl delete -f deploy/ray/ --ignore-not-found || true

echo "🔹 6. Deleting PVCs (prevents orphaned EBS volumes)..."
kubectl delete pvc --all -n default --ignore-not-found || true

echo "🔹 7. Waiting for LoadBalancers to deregister (60s)..."
sleep 60

echo "🔹 8. Running Terraform Destroy..."
cd "$(dirname "$0")/../infra/terraform"
terraform destroy -auto-approve

echo "✅ All resources destroyed."