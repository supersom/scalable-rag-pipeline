#!/bin/bash
# scripts/check_ray_health.sh
#
# Checks health of the LLM and embedding Ray Serve services.
# Covers: RayService CRD status, ray status on head pods, and live HTTP probes
# against both Serve endpoints from within the cluster.
#
# Usage:
#   bash scripts/check_ray_health.sh
#   bash scripts/check_ray_health.sh --endpoints-only        # skip ray status (faster)
#   bash scripts/check_ray_health.sh --watch                 # repeat every 30s
#   bash scripts/check_ray_health.sh --watch --interval 10   # repeat every 10s
#   bash scripts/check_ray_health.sh --watch --endpoints-only

set -euo pipefail

ENDPOINTS_ONLY=false
WATCH=false
INTERVAL=30

while [[ $# -gt 0 ]]; do
  case "$1" in
    --endpoints-only) ENDPOINTS_ONLY=true ;;
    --watch)          WATCH=true ;;
    --interval)       INTERVAL="${2:?--interval requires a value}"; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
  shift
done

# In watch mode, re-exec the same script (without --watch) on a loop.
if [[ "$WATCH" == true ]]; then
  PASSTHROUGH=()
  [[ "$ENDPOINTS_ONLY" == true ]] && PASSTHROUGH+=(--endpoints-only)
  while true; do
    clear
    echo "Ray health check — $(date '+%Y-%m-%d %H:%M:%S')  (refreshing every ${INTERVAL}s, Ctrl-C to stop)"
    bash "$0" "${PASSTHROUGH[@]}" || true
    sleep "$INTERVAL"
  done
  exit 0
fi

PASS="[OK]"
FAIL="[FAIL]"
WARN="[WARN]"

overall=0

section() { echo; echo "══ $* ══"; }

pass()  { echo "  ${PASS} $*"; }
fail()  { echo "  ${FAIL} $*"; overall=1; }
warn()  { echo "  ${WARN} $*"; }

# ── 1. RayService CRD status ─────────────────────────────────────────────────

section "RayService status"

for svc in llm-service embed-service; do
  state=$(kubectl get rayservice "$svc" \
    -o jsonpath='{.status.serviceStatus}' 2>/dev/null || echo "NOT_FOUND")

  case "$state" in
    Running)
      pass "${svc}: ${state}"
      ;;
    FailedToUpdateService|FailedToGetServeDeploymentStatus)
      # Known KubeRay 1.0.0 issues: port reconciliation failures and transient
      # status fetch errors. Active cluster and deployments are still healthy.
      warn "${svc}: ${state} (KubeRay 1.0.0 — check active cluster below)"
      ;;
    NOT_FOUND)
      fail "${svc}: not found"
      ;;
    *)
      fail "${svc}: ${state}"
      ;;
  esac

  cluster_state=$(kubectl get rayservice "$svc" \
    -o jsonpath='{.status.activeServiceStatus.rayClusterStatus.state}' 2>/dev/null || echo "unknown")
  if [[ "$cluster_state" == "ready" ]]; then
    pass "${svc} active cluster: ${cluster_state}"
  else
    fail "${svc} active cluster: ${cluster_state}"
  fi
done

# ── 2. ray status on head pods ───────────────────────────────────────────────

if [[ "$ENDPOINTS_ONLY" == false ]]; then
  section "Ray cluster internals"

  for svc in llm-service embed-service; do
    active_cluster=$(kubectl get rayservice "$svc" \
      -o jsonpath='{.status.activeServiceStatus.rayClusterName}' 2>/dev/null || true)

    if [[ -z "$active_cluster" ]]; then
      warn "${svc}: could not determine active cluster name"
      continue
    fi

    head_pod=$(kubectl get pod \
      -l "ray.io/cluster=${active_cluster},ray.io/node-type=head" \
      -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

    if [[ -z "$head_pod" ]]; then
      warn "${svc}: could not locate head pod in cluster ${active_cluster}"
      continue
    fi

    echo "  ${svc} head: ${head_pod}"
    kubectl exec "$head_pod" -c ray-head -- ray status 2>/dev/null | sed 's/^/    /' \
      || warn "${svc}: ray status failed"

    echo "  ${svc} serve status:"
    kubectl exec "$head_pod" -c ray-head -- serve status 2>/dev/null | sed 's/^/    /' \
      || warn "${svc}: serve status failed"
  done
fi

# ── 3. Live HTTP endpoint probes ─────────────────────────────────────────────

section "Serve endpoint probes"

# Probe via the API pod (httpx already installed) to avoid ephemeral pod overhead.
API_POD=$(kubectl get pod -l app.kubernetes.io/name=api -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)

if [[ -z "$API_POD" ]]; then
  warn "No API pod found — skipping endpoint probes"
else
  echo "  Probing via: ${API_POD}"

  LLM_STATUS=$(kubectl exec "$API_POD" -- python3 -c "
import httpx, sys
try:
    r = httpx.post(
        'http://llm-service-head-svc:8000/llm/chat/completions',
        json={'model':'llama3','messages':[{'role':'user','content':'ping'}],'max_tokens':1},
        timeout=15,
    )
    print(r.status_code)
except Exception as e:
    print(f'ERR:{e}')
" 2>/dev/null || echo "ERR:exec failed")

  if [[ "$LLM_STATUS" == "200" ]]; then
    pass "llm-service HTTP ${LLM_STATUS}"
  else
    fail "llm-service: ${LLM_STATUS}"
  fi

  EMBED_STATUS=$(kubectl exec "$API_POD" -- python3 -c "
import httpx, sys
try:
    r = httpx.post(
        'http://embed-service-head-svc:8000/embed/embeddings',
        json={'input':'health check','model':'bge-m3'},
        timeout=15,
    )
    print(r.status_code)
except Exception as e:
    print(f'ERR:{e}')
" 2>/dev/null || echo "ERR:exec failed")

  if [[ "$EMBED_STATUS" == "200" ]]; then
    pass "embed-service HTTP ${EMBED_STATUS}"
  else
    fail "embed-service: ${EMBED_STATUS}"
  fi
fi

# ── 4. Summary ───────────────────────────────────────────────────────────────

section "Summary"
if [[ "$overall" -eq 0 ]]; then
  pass "All checks passed"
else
  fail "One or more checks failed"
fi
exit "$overall"
