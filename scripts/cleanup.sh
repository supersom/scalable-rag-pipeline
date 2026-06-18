#!/bin/bash
# scripts/cleanup.sh
set -euo pipefail

echo "⚠️  WARNING: THIS WILL DESTROY ALL CLOUD RESOURCES ⚠️"
echo "Includes: EKS Cluster, Databases (RDS/Aurora/Neo4j/Redis), S3 Buckets, Load Balancers."
echo "Cost-saving measure for Dev/Test environments."
echo ""
read -p "Are you sure? Type 'DESTROY': " confirm

if [ "$confirm" != "DESTROY" ]; then
    echo "Aborted."
    exit 1
fi

CLUSTER_NAME="rag-platform-cluster"
REGION="us-east-1"

# ── 1. Stop ingestion before deleting its RayCluster ─────────────────────────
# Delete the queue consumer first so it cannot submit new jobs while the cluster
# is terminating. RayClusters must be deleted while the KubeRay operator exists.
echo "🔹 1. Deleting ingestion worker and RayCluster..."
kubectl delete deployment ingestion-worker -n default --ignore-not-found || true
kubectl delete raycluster ingestion-ray -n default --ignore-not-found || true
kubectl delete serviceaccount ingestion-worker -n default --ignore-not-found || true

# ── 2. RayServices — triggers GPU node drain via Karpenter ────────────────────
echo "🔹 2. Deleting RayServices (triggers GPU node drain)..."
kubectl delete rayservice --all -n default --ignore-not-found || true

# ── 3. Helm uninstall all app workloads ───────────────────────────────────────
# Do this BEFORE waiting for nodeclaims — CPU nodes won't drain until CPU pods are gone.
echo "🔹 3. Helm uninstall (api, qdrant, neo4j-cluster)..."
helm uninstall api              --ignore-not-found 2>/dev/null || true
helm uninstall qdrant           --ignore-not-found 2>/dev/null || true
helm uninstall neo4j-cluster    --ignore-not-found 2>/dev/null || true

# ── 4. Delete remaining Ray manifests before removing KubeRay ────────────────
echo "🔹 4. Deleting remaining Ray resources..."
kubectl delete -f deploy/ray/ --ignore-not-found 2>/dev/null || true

echo "  Uninstalling KubeRay operator..."
helm uninstall kuberay-operator --ignore-not-found 2>/dev/null || true

# ── 5. Delete Karpenter NodePools — stops Karpenter refilling drained nodes ───
echo "🔹 5. Deleting Karpenter NodePools and EC2NodeClass..."
kubectl delete nodepool --all --ignore-not-found || true
kubectl delete ec2nodeclass --all --ignore-not-found || true

# ── 6. Force-delete all remaining NodeClaims ──────────────────────────────────
echo "🔹 6. Deleting all NodeClaims (forces node termination)..."
kubectl delete nodeclaim --all --ignore-not-found || true

# ── 7. Wait for all nodes provisioned by Karpenter to terminate ───────────────
echo "🔹 7. Waiting for NodeClaims to clear (up to 10 min)..."
for i in $(seq 1 60); do
    remaining=$(kubectl get nodeclaim -o name 2>/dev/null | wc -l)
    [ "$remaining" -eq 0 ] && { echo "  All NodeClaims gone."; break; }
    echo "  ${remaining} nodeclaim(s) still present, waiting 10s..."
    sleep 10
done

# ── 8. Uninstall Karpenter controller ─────────────────────────────────────────
# Must be done before terraform destroy removes Karpenter's IAM roles.
echo "🔹 8. Uninstalling Karpenter Helm release..."
helm uninstall karpenter -n kube-system --ignore-not-found 2>/dev/null || true

# ── 9. Delete PVCs — prevents orphaned EBS volumes ────────────────────────────
echo "🔹 9. Deleting PVCs..."
kubectl delete pvc --all -n default --ignore-not-found || true

# ── 10. Delete remaining manifests and secrets ────────────────────────────────
echo "🔹 10. Deleting remaining k8s resources..."
kubectl delete -f deploy/k8s/  --ignore-not-found 2>/dev/null || true
kubectl delete secret app-env-secret --ignore-not-found || true

# ── 11. (No-op) Documents S3 bucket — force_destroy=true handles cleanup ──────
# force_destroy=true on aws_s3_bucket.documents means terraform destroy empties
# the bucket (including all versions and delete markers) automatically.
echo "🔹 11. Documents S3 bucket will be emptied by terraform destroy (force_destroy=true)."

# ── 12. Wait for Load Balancers to deregister ─────────────────────────────────
echo "🔹 12. Waiting 60s for Load Balancers to deregister..."
sleep 60

# ── 13. Terraform destroy ─────────────────────────────────────────────────────
# Removes the ingestion S3 notification, SQS queue/DLQ, IAM policy/role updates,
# and the force-deletable services/ingestion ECR repository.
echo "🔹 13. Running Terraform destroy..."
cd "$(dirname "$0")/../infra/terraform"
terraform destroy -auto-approve

echo "✅ All resources destroyed."
