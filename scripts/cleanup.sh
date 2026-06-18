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
DOCS_BUCKET=$(terraform -chdir="$(dirname "$0")/../infra/terraform" output -raw s3_documents_bucket_name 2>/dev/null || echo "")

# ── 1. RayServices first — triggers GPU node drain via Karpenter ──────────────
echo "🔹 1. Deleting RayServices (triggers GPU node drain)..."
kubectl delete rayservice --all -n default --ignore-not-found || true

# ── 2. Helm uninstall all app workloads ───────────────────────────────────────
# Do this BEFORE waiting for nodeclaims — CPU nodes won't drain until CPU pods are gone.
echo "🔹 2. Helm uninstall (api, qdrant, neo4j-cluster, kuberay-operator)..."
helm uninstall api              --ignore-not-found 2>/dev/null || true
helm uninstall qdrant           --ignore-not-found 2>/dev/null || true
helm uninstall neo4j-cluster    --ignore-not-found 2>/dev/null || true
helm uninstall kuberay-operator --ignore-not-found 2>/dev/null || true

# ── 3. Delete Karpenter NodePools — stops Karpenter refilling drained nodes ───
echo "🔹 3. Deleting Karpenter NodePools and EC2NodeClass..."
kubectl delete nodepool --all --ignore-not-found || true
kubectl delete ec2nodeclass --all --ignore-not-found || true

# ── 4. Force-delete all remaining NodeClaims ──────────────────────────────────
echo "🔹 4. Deleting all NodeClaims (forces node termination)..."
kubectl delete nodeclaim --all --ignore-not-found || true

# ── 5. Wait for all nodes provisioned by Karpenter to terminate ───────────────
echo "🔹 5. Waiting for NodeClaims to clear (up to 10 min)..."
for i in $(seq 1 60); do
    remaining=$(kubectl get nodeclaim -o name 2>/dev/null | wc -l)
    [ "$remaining" -eq 0 ] && { echo "  All NodeClaims gone."; break; }
    echo "  ${remaining} nodeclaim(s) still present, waiting 10s..."
    sleep 10
done

# ── 6. Uninstall Karpenter controller ─────────────────────────────────────────
# Must be done before terraform destroy removes Karpenter's IAM roles.
echo "🔹 6. Uninstalling Karpenter Helm release..."
helm uninstall karpenter -n kube-system --ignore-not-found 2>/dev/null || true

# ── 7. Delete PVCs — prevents orphaned EBS volumes ────────────────────────────
echo "🔹 7. Deleting PVCs..."
kubectl delete pvc --all -n default --ignore-not-found || true

# ── 8. Delete remaining manifests and secrets ─────────────────────────────────
echo "🔹 8. Deleting remaining k8s resources..."
kubectl delete -f deploy/ray/  --ignore-not-found 2>/dev/null || true
kubectl delete -f deploy/k8s/  --ignore-not-found 2>/dev/null || true
kubectl delete secret app-env-secret --ignore-not-found || true

# ── 9. (No-op) Documents S3 bucket — force_destroy=true handles cleanup ───────
# force_destroy=true on aws_s3_bucket.documents means terraform destroy empties
# the bucket (including all versions and delete markers) automatically.
echo "🔹 9. Documents S3 bucket will be emptied by terraform destroy (force_destroy=true)."

# ── 10. Wait for Load Balancers to deregister ─────────────────────────────────
echo "🔹 10. Waiting 60s for Load Balancers to deregister..."
sleep 60

# ── 11. Terraform destroy ─────────────────────────────────────────────────────
echo "🔹 11. Running Terraform destroy..."
cd "$(dirname "$0")/../infra/terraform"
terraform destroy -auto-approve

echo "✅ All resources destroyed."
