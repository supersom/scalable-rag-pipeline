# Deployment handoff - 2026-09-16

## Stop point

We halted during AWS/EKS deployment recovery after Redis and API were repaired, while Ray Serve model deployments were still not healthy.

Current failing point before shutdown:

- `embed-service` and `llm-service` RayServices reached `WaitForServeDeploymentReady`.
- Ray head and GPU worker pods came up on newly provisioned GPU nodes.
- Both model Serve deployments failed in their constructors because the Ray Serve image did not include the `aws` executable:
  - `FileNotFoundError: [Errno 2] No such file or directory: 'aws'`
  - Failing code path: `_resolve_model_path()` in `services/api/app/models/embedding_engine.py` and `services/api/app/models/vllm_engine.py`
  - The code calls `aws s3 sync s3://rag-platform-models-prod-7649/models/<model-id>/ /model-cache/<model-id>/ --no-progress`

A follow-up Docker build attempted to add `awscli` to the Ray Serve image, but the local Docker environment ran out of disk space during the vLLM/Torch dependency layer:

- Image tag attempted: `764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli`
- Build failed with: `OSError: [Errno 28] No space left on device`
- No new `fastapi109-awscli` image was pushed.

## What was successfully fixed

1. Redis / ElastiCache

- Original failure: ElastiCache Redis replication group `rag-redis-prod` was stuck in `create-failed` because AWS lacked capacity in `us-east-1c`.
- Mitigation: updated `infra/terraform/redis.tf` to pin Redis cluster placement to `us-east-1a` and `us-east-1b`:
  - `preferred_cache_cluster_azs = ["${var.aws_region}a", "${var.aws_region}b"]`
- Deleted the failed replication group and reapplied Terraform.
- Terraform recreated Redis successfully.
- Redis primary endpoint became:
  - `master.rag-redis-prod.3jagvy.use1.cache.amazonaws.com`
- Patched Kubernetes `app-env-secret` to set:
  - `REDIS_URL=rediss://master.rag-redis-prod.3jagvy.use1.cache.amazonaws.com:6379`

2. API deployment

- Restarted the API deployment after patching Redis.
- API pods became healthy: `2/2 Running`.
- Re-ran bootstrap stage 12 successfully:
  - `API_IMAGE_TAG=34fe28e ./scripts/bootstrap_cluster.sh --start 12 --end 12`
- Helm API release revision 2 reached `STATUS: deployed`.

3. Ray Serve serialization issue

- Ray Serve initially failed before reaching model loading with FastAPI serialization errors:
  - `cannot pickle '_thread.lock' object`
- Root cause: unpinned FastAPI/Starlette drift in the Ray image, incompatible with Ray 2.9.0 Serve ingress serialization.
- Built and pushed a corrected Ray Serve image:
  - Tag: `34fe28e-fastapi109`
  - ECR image: `764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109`
  - ECR digest: `sha256:5dfe7b94b9cbacd3a04de8cbe670cdd5b27ced2fc25196920a96854c69dd55cb`
- That image fixed the FastAPI/Ray serialization failure and allowed Ray head/worker pods to start.

## Local code changes made during recovery

### `infra/terraform/redis.tf`

- Added Redis preferred AZ placement:
  - `preferred_cache_cluster_azs = ["${var.aws_region}a", "${var.aws_region}b"]`
- Terraform formatting also aligned several attributes.

### `services/models/Dockerfile`

Current local Dockerfile includes two categories of fixes:

- FastAPI/Ray compatibility fix:
  - Pins `fastapi==0.109.0`.
  - Keeps Starlette at the compatible `0.35.x` line selected by FastAPI.
  - Patches Ray 2.9 Serve proxy code from `middleware.options` to `middleware.kwargs`.
  - Adds import smoke tests for both Ray Serve app symbols.
- Pending AWS CLI fix:
  - Adds `awscli>=1.32,<2`.
  - Adds a build-time `aws --version` check.
  - This change has not yet produced a pushed image because Docker ran out of disk space locally.

## Other working tree changes observed

These files had unrelated or pre-existing local changes and were not intentionally reverted:

- `CLAUDE.md`
- `docs/diagrams/current_architecture.md`
- `services/api/main.py`
- untracked `.codegraph/`, `.codex`, `.learnings/`, `docs/diagrams/infra_topology.md`, `docs/production-hardening.md`

## AWS/Kubernetes state before wind-down

Important workload state before shutdown:

- API: `deployment/api` was `2/2 Running`.
- Qdrant: `statefulset/qdrant` was `3/3 Running`.
- Neo4j: `statefulset/neo4j-cluster` was `1/1 Running`.
- KubeRay operator: `deployment/kuberay-operator` was `1/1 Running`.
- RayServices:
  - `embed-service`: `WaitForServeDeploymentReady`
  - `llm-service`: `WaitForServeDeploymentReady`
- GPU Karpenter NodeClaims:
  - `gpu-pggw4`: `g5.2xlarge`, on-demand, `us-east-1c`, instance `i-07aa3ad6018b0134c`
  - `gpu-pw766`: `g5.2xlarge`, on-demand, `us-east-1c`, instance `i-0a6fbf6fa96e678ea`
- CPU Karpenter NodeClaims:
  - `cpu-4j2ms`, `cpu-82kq4`, `cpu-drk7f`, `cpu-ksw7d`

## Resume tomorrow

Recommended next steps:

1. Free Docker disk space locally, then rebuild the Ray Serve image from the current `services/models/Dockerfile`:
   - `docker build -t 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli -f services/models/Dockerfile .`
2. Push that image to ECR.
3. Re-run Ray bootstrap with the new tag:
   - `RAY_IMAGE_TAG=34fe28e-fastapi109-awscli ./scripts/bootstrap_cluster.sh --start 13 --end 13`
4. Verify both RayServices reach healthy Serve deployment status.
5. Test internal endpoints:
   - `http://embed-service-head-svc:8000/embed/embeddings`
   - `http://llm-service-head-svc:8000/llm/chat/completions`
6. Continue with ingestion bootstrap stage 14 after model endpoints are healthy.

## Wind-down actions

This section will be updated after the EC2-backed workloads are scaled down.

## Wind-down actions completed

Executed after the stop point above:

1. Deleted Ray model services:
   - `kubectl delete rayservice embed-service llm-service --wait=true --timeout=5m`
   - Result: RayServices removed.
   - Result: both GPU EC2 instances terminated:
     - `i-07aa3ad6018b0134c` (`g5.2xlarge`) -> `terminated`
     - `i-0a6fbf6fa96e678ea` (`g5.2xlarge`) -> `terminated`

2. Scaled default application workloads to zero:
   - `deployment/api` -> `0/0`
   - `deployment/kuberay-operator` in `default` namespace -> `0/0`
   - `statefulset/qdrant` -> `0/0`
   - `statefulset/neo4j-cluster` -> `0/0`

3. Deleted Karpenter NodeClaims:
   - Deleted prior CPU/GPU NodeClaims.
   - The older CPU spot instance `i-0a92a712403cc08c5` was confirmed `terminated`.

4. Scaled the managed EKS node group to zero:
   - Node group: `system-nodes-20260915200734300100000023`
   - Final scaling config observed:
     - `minSize=0`
     - `desiredSize=0`
     - `maxSize=5`
   - One managed node terminated normally: `i-0241066c901f12f8a`.
   - The last managed node `i-013ffb7bcbf954922` remained running after desired size reached zero, so it was explicitly terminated and then confirmed by `aws ec2 wait instance-terminated`.

## Remaining wind-down anomaly

After all app workloads and the managed node group were scaled down, Karpenter created a new replacement CPU spot node for kube-system add-on pods:

- New NodeClaim: `cpu-8mq24`
- Instance: `i-0418f98d4f880e6c6`
- Type: `m6i.large`
- State at last check: `running`
- Private IP: `10.0.101.241`

Cause:

- Kube-system add-ons (`coredns`, `ebs-csi-controller`, Karpenter, daemonsets) still had desired replicas/pods.
- With the managed node group at zero, Karpenter provisioned a spot node to satisfy those system pods.

Not completed without explicit approval:

- Scaling kube-system critical deployments to zero:
  - `deployment/karpenter`
  - `deployment/coredns`
  - `deployment/ebs-csi-controller`
- This was intentionally stopped because it disables cluster DNS/storage/node lifecycle services. If the goal is a fully workerless EKS cluster overnight, approve scaling those kube-system deployments to zero and then terminate/delete the replacement NodeClaim/instance.

## Current practical state

- Expensive GPU EC2 capacity is off.
- Application workloads are off.
- Data-plane app pods are off.
- Managed node group desired/min is zero.
- One small Karpenter-created `m6i.large` spot worker remains because cluster add-ons are still scheduled.

## Full cluster scale-down approved and completed

After explicit approval to scale down the cluster, the kube-system add-on controllers were also scaled to zero:

- `deployment/karpenter` in `kube-system` -> `0/0`
- `deployment/coredns` in `kube-system` -> `0/0`
- `deployment/ebs-csi-controller` in `kube-system` -> `0/0`

The replacement Karpenter spot node was then removed:

- NodeClaim: `cpu-8mq24`
- Instance: `i-0418f98d4f880e6c6`
- Action: explicitly terminated with `aws ec2 terminate-instances`
- Result: `aws ec2 wait instance-terminated` completed successfully.

Final AWS check returned no active EC2 instances matching the EKS cluster tag in states `pending`, `running`, `stopping`, `stopped`, or `shutting-down`.

Note for resume:

- The EKS control plane still exists.
- Worker EC2 capacity is scaled down to zero.
- To resume, restore kube-system controllers first or run the Terraform/bootstrap path that recreates/scales nodes and add-ons, then scale/redeploy app workloads.

## Final zero-worker verification

After the replacement node was terminated, Kubernetes still had a stale node record with a `karpenter.sh/termination` finalizer. Because Karpenter was already scaled to zero as part of the approved cluster shutdown, that finalizer could not clear itself.

Cleanup performed:

- Removed the stale node finalizer from `ip-10-0-101-241.ec2.internal`.
- Deleted the stale Kubernetes node record.
- Force-deleted stale pending DaemonSet pods bound to that node.

Final checks:

- `kubectl get nodeclaims,nodes,pods -A -o wide` returned `No resources found`.
- All relevant deployments/statefulsets are scaled to `0/0`:
  - `default/api`
  - `default/kuberay-operator`
  - `default/qdrant`
  - `default/neo4j-cluster`
  - `kube-system/karpenter`
  - `kube-system/coredns`
  - `kube-system/ebs-csi-controller`
- Managed node group `system-nodes-20260915200734300100000023` remains `minSize=0`, `desiredSize=0`, `maxSize=5`.
- Final EC2 query for active instances tagged to `rag-platform-cluster` returned no rows.
