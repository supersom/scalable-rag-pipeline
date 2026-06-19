#!/bin/bash
# scripts/cleanup.sh
set -euo pipefail

CLUSTER_NAME="rag-platform-cluster"
REGION="us-east-1"

# ── Stage range ───────────────────────────────────────────────────────────────
# --start N --end M  : run stages N through M inclusive
# --start N --end N  : run stage N only
# --start N          : run stage N only (end defaults to start)
# (no flags)         : run all stages

MAX_STAGE=13
START=1
END=$MAX_STAGE
_start_set=false
_end_set=false

while [[ $# -gt 0 ]]; do
  case $1 in
    --start) START=$2; _start_set=true; shift 2 ;;
    --end)   END=$2;   _end_set=true;   shift 2 ;;
    *) echo "ERROR: unknown option: $1" >&2; exit 1 ;;
  esac
done

if $_start_set && ! $_end_set; then
  END=$START
fi

if (( START < 1 || END > MAX_STAGE || START > END )); then
  echo "ERROR: invalid stage range --start $START --end $END (valid: 1–$MAX_STAGE)" >&2
  exit 1
fi

stage_active() { (( $1 >= START && $1 <= END )); }

# ── Confirmation ──────────────────────────────────────────────────────────────
echo "WARNING: THIS WILL DESTROY CLOUD RESOURCES"
if [[ $START -eq 1 && $END -eq $MAX_STAGE ]]; then
  echo "Includes: EKS Cluster, Databases (RDS/Aurora/Neo4j/Redis), S3 Buckets, Load Balancers."
else
  [[ $START -eq $END ]] \
    && echo "Running stage $START of $MAX_STAGE only." \
    || echo "Running stages $START–$END of $MAX_STAGE."
fi
echo "Cost-saving measure for Dev/Test environments."
echo ""
read -rp "Are you sure? Type 'DESTROY': " confirm

if [ "$confirm" != "DESTROY" ]; then
  echo "Aborted."
  exit 1
fi

# ── Stages ────────────────────────────────────────────────────────────────────

if stage_active 1; then
  # Delete the queue consumer first so it cannot submit new jobs while the cluster
  # is terminating. RayClusters must be deleted while the KubeRay operator exists.
  echo "--- 1. Deleting ingestion worker and RayCluster ---"
  kubectl delete deployment ingestion-worker -n default --ignore-not-found || true
  kubectl delete raycluster ingestion-ray -n default --ignore-not-found || true
  kubectl delete serviceaccount ingestion-worker -n default --ignore-not-found || true
fi

if stage_active 2; then
  echo "--- 2. Deleting RayServices (triggers GPU node drain) ---"
  kubectl delete rayservice --all -n default --ignore-not-found || true
fi

if stage_active 3; then
  # Do this BEFORE waiting for nodeclaims — CPU nodes won't drain until CPU pods are gone.
  echo "--- 3. Helm uninstall (api, qdrant, neo4j-cluster) ---"
  helm uninstall api           --ignore-not-found 2>/dev/null || true
  helm uninstall qdrant        --ignore-not-found 2>/dev/null || true
  helm uninstall neo4j-cluster --ignore-not-found 2>/dev/null || true
fi

if stage_active 4; then
  echo "--- 4. Deleting remaining Ray resources ---"
  kubectl delete -f deploy/ray/ --ignore-not-found 2>/dev/null || true
  echo "  Uninstalling KubeRay operator..."
  helm uninstall kuberay-operator --ignore-not-found 2>/dev/null || true
fi

if stage_active 5; then
  echo "--- 5. Deleting Karpenter NodePools and EC2NodeClass ---"
  kubectl delete nodepool --all --ignore-not-found || true
  kubectl delete ec2nodeclass --all --ignore-not-found || true
fi

if stage_active 6; then
  echo "--- 6. Deleting all NodeClaims (forces node termination) ---"
  kubectl delete nodeclaim --all --ignore-not-found || true
fi

if stage_active 7; then
  echo "--- 7. Waiting for NodeClaims to clear (up to 10 min) ---"
  for i in $(seq 1 60); do
    remaining=$(kubectl get nodeclaim -o name 2>/dev/null | wc -l)
    [ "$remaining" -eq 0 ] && { echo "  All NodeClaims gone."; break; }
    echo "  ${remaining} nodeclaim(s) still present, waiting 10s..."
    sleep 10
  done
fi

if stage_active 8; then
  # Must be done before terraform destroy removes Karpenter's IAM roles.
  echo "--- 8. Uninstalling Karpenter Helm release ---"
  helm uninstall karpenter -n kube-system --ignore-not-found 2>/dev/null || true
fi

if stage_active 9; then
  echo "--- 9. Deleting PVCs ---"
  kubectl delete pvc --all -n default --ignore-not-found || true
fi

if stage_active 10; then
  echo "--- 10. Deleting remaining k8s resources ---"
  kubectl delete -f deploy/k8s/ --ignore-not-found 2>/dev/null || true
  kubectl delete secret app-env-secret --ignore-not-found || true
fi

if stage_active 11; then
  # force_destroy=true on aws_s3_bucket.documents means terraform destroy empties
  # the bucket (including all versions and delete markers) automatically.
  echo "--- 11. Documents S3 bucket will be emptied by terraform destroy (force_destroy=true) ---"
fi

if stage_active 12; then
  echo "--- 12. Waiting 60s for Load Balancers to deregister ---"
  sleep 60
fi

if stage_active 13; then
  # Removes the ingestion S3 notification, SQS queue/DLQ, IAM policy/role updates,
  # and the force-deletable services/ingestion ECR repository.
  echo "--- 13. Running Terraform destroy ---"
  ( cd "$(dirname "$0")/../infra/terraform" && terraform destroy -auto-approve )
fi

echo ""
[[ $START -eq 1 && $END -eq $MAX_STAGE ]] \
  && echo "All resources destroyed." \
  || { [[ $START -eq $END ]] \
    && echo "Stage $START complete." \
    || echo "Stages $START–$END complete."; }
