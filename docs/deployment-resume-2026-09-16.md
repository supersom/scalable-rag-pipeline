# Cluster Resume Log - 2026-09-16

This document records the work performed to restore the production EKS cluster
after the shutdown documented in `docs/deployment-handoff-2026-09-16.md`.
Commands are listed in the order they were used. Times are Pacific Time unless
otherwise noted.

## Starting position

- EKS control plane: running.
- Managed system node group: scaled to zero.
- Application deployments and stateful sets: scaled to zero.
- Persistent EBS volumes: retained.
- Ray Serve model services: not running.
- Intended baseline: two managed system nodes, API 2, Qdrant 3, Neo4j 1,
  KubeRay operator 1, and one GPU worker for each model service.

## 1. Restore the managed system node group

The managed node group was restored to its Terraform baseline of minimum 2,
desired 2, maximum 5:

```bash
aws eks update-nodegroup-config \
  --cluster-name rag-platform-cluster \
  --nodegroup-name system-nodes-20260915200734300100000023 \
  --region us-east-1 \
  --scaling-config minSize=2,maxSize=5,desiredSize=2

aws eks wait nodegroup-active \
  --cluster-name rag-platform-cluster \
  --nodegroup-name system-nodes-20260915200734300100000023 \
  --region us-east-1
```

## 2. Restore cluster controllers

CoreDNS, EBS CSI, and Karpenter were restored to two replicas each:

```bash
kubectl scale deployment/coredns -n kube-system --replicas=2
kubectl scale deployment/ebs-csi-controller -n kube-system --replicas=2
kubectl scale deployment/karpenter -n kube-system --replicas=2

kubectl rollout status deployment/coredns -n kube-system
kubectl rollout status deployment/ebs-csi-controller -n kube-system
kubectl rollout status deployment/karpenter -n kube-system
```

Important command behavior discovered during recovery: when multiple resources
are passed to one `kubectl scale` invocation, a repeated `--replicas` flag does
not assign a separate count to each resource. The final value applies to all of
them. Separate commands were therefore used for different replica counts.

## 3. Restore stateful and application workloads

The application workloads were restored with their intended counts:

```bash
kubectl scale deployment/kuberay-operator --replicas=1
kubectl scale statefulset/qdrant --replicas=3
kubectl scale statefulset/neo4j-cluster --replicas=1
kubectl scale deployment/api --replicas=2
```

Karpenter provisioned application nodes in the Availability Zones required by
the retained EBS volumes. Qdrant's volumes remained intact and reattached to the
new nodes.

## 4. Stabilize Karpenter and EBS placement

Karpenter consolidation moved stateful pods while Qdrant was still recovering.
This caused repeated EBS detach/attach cycles and one API replica to fail its
startup dependency check. AWS showed the affected volumes as detached and
available; Kubernetes subsequently cleared the stale `VolumeAttachment`
records and remounted them.

To stop further voluntary disruption during recovery, active application nodes
were temporarily annotated:

```bash
kubectl annotate node \
  ip-10-0-102-19.ec2.internal \
  ip-10-0-102-214.ec2.internal \
  ip-10-0-103-250.ec2.internal \
  karpenter.sh/do-not-disrupt=true --overwrite
```

After Qdrant returned to 3/3, the failed API pod restarted and the API returned
to 2/2.

## 5. Repair and publish the Ray Serve image

The previous Ray image had two independent startup problems:

1. Ray 2.9 expected `Middleware.options`, but Starlette exposes
   `Middleware.kwargs`.
2. Model startup calls `aws s3 sync`, but the image did not contain the AWS CLI.

`services/models/Dockerfile` was updated to pin FastAPI 0.109, install AWS CLI
v1, patch Ray's middleware access, and smoke-test both Serve applications.

The first corrected image was pushed directly from BuildKit because Docker
Desktop was 98% full and loading the roughly 25 GB uncompressed image into the
local image store would have duplicated the build cache:

```bash
aws ecr get-login-password --region us-east-1 | \
  docker login --username AWS --password-stdin \
    764988411032.dkr.ecr.us-east-1.amazonaws.com

docker buildx build --progress=plain --push \
  -t 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli \
  -f services/models/Dockerfile .
```

Published image:

- Tag: `34fe28e-fastapi109-awscli`
- Digest: `sha256:56830763b4a514211e3fa54d3e937258b534d582c154a7992d8fae600e33e26b`
- Compressed size: 9,635,688,105 bytes
- Pushed: 2026-09-16 10:19:52 PDT

The AWS CLI then ran, but model sync failed because Ray runs as the non-root
`ray` user and `/model-cache` did not exist with writable ownership. The
canonical Dockerfile was updated to create it as `ray:users`, and a small layer
was pushed over the existing image:

```bash
docker buildx build --progress=plain --push \
  -t 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli-cachefix \
  -f - . <<'EOF'
FROM 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli
USER root
RUN mkdir -p /model-cache && chown ray:users /model-cache
USER ray
EOF
```

Published image:

- Tag: `34fe28e-fastapi109-awscli-cachefix`
- Digest: `sha256:d959c924249f352d6d630ba62e1e894f05816facc9c65077d5b4028069ba5309`
- Compressed size: 9,635,688,721 bytes
- Pushed: 2026-09-16 10:34:27 PDT

Registry metadata was verified with:

```bash
aws ecr describe-images \
  --repository-name services/ray-serve \
  --image-ids imageTag=34fe28e-fastapi109-awscli-cachefix \
  --region us-east-1
```

## 6. Start Ray Serve and GPU capacity

Stage 13 was applied with the corrected image:

```bash
RAY_IMAGE_TAG=34fe28e-fastapi109-awscli-cachefix \
  ./scripts/bootstrap_cluster.sh --start 13 --end 13
```

Karpenter initially provisioned two on-demand `g5.2xlarge` instances. During a
subsequent Ray generation change, both GPU pods were briefly absent. Because
the GPU NodePool consolidates empty nodes after 30 seconds, Karpenter terminated
both instances before replacement pods were ready.

The pending replacement GPU workers then hit the account's 16-vCPU On-Demand G
instance quota while the old instances were still in `shutting-down`:

```text
VcpuLimitExceeded: requested capacity exceeds the current vCPU limit of 16
```

The old instances were allowed to terminate:

```bash
aws ec2 wait instance-terminated \
  --instance-ids i-08fed8d7dc8b993e9 i-056e5545881b2d846 \
  --region us-east-1
```

Karpenter caches failed instance offerings temporarily. Once that cache expired,
it launched the replacement nodes:

- `gpu-skb7f`: `g5.2xlarge`, `us-east-1a`, `i-07fabdb8883d92122`
- `gpu-dgt5m`: `g5.2xlarge`, `us-east-1b`, `i-02f49a426aa60b435`

The NVIDIA device plugin became healthy on both nodes. First image pulls took
approximately 3 minutes 10 seconds per node.

To preserve those nodes and their warm image cache during further recovery,
they were temporarily pinned:

```bash
kubectl annotate node \
  ip-10-0-101-233.ec2.internal \
  ip-10-0-102-216.ec2.internal \
  karpenter.sh/do-not-disrupt=true --overwrite
```

## 7. Add model-cache credentials with IRSA

After the filesystem fix, an interactive sync exposed the next error:

```bash
kubectl exec embed-service-raycluster-hrfd8-gpu-workers-worker-8gldk \
  -n default -c ray-worker -- \
  aws s3 sync \
    s3://rag-platform-models-prod-7649/models/BAAI/bge-m3/ \
    /model-cache/BAAI--bge-m3 --no-progress
```

Result:

```text
fatal error: Unable to locate credentials
```

Terraform had granted model-bucket read access to the Karpenter EC2 node role,
but the `EC2NodeClass` correctly uses IMDSv2 hop limit 1. Pods therefore cannot
inherit node credentials. The previous comment claiming they could was
incorrect.

The durable fix added:

- A dedicated Terraform IRSA role named `rag-ray-models-role`.
- Trust restricted to `system:serviceaccount:default:ray-models`.
- The existing read-only model-cache policy attachment.
- A `ray_models_irsa_role_arn` Terraform output.
- Idempotent creation/annotation of `ray-models` in bootstrap stage 13.
- `serviceAccountName: ray-models` on both Ray GPU worker templates.

Validation and the targeted plan:

```bash
terraform -chdir=infra/terraform init -input=false
terraform -chdir=infra/terraform fmt iam.tf outputs.tf
terraform -chdir=infra/terraform validate
bash -n scripts/bootstrap_cluster.sh

terraform -chdir=infra/terraform plan \
  -input=false \
  -target=module.ray_models_irsa_role \
  -out=/tmp/ray-models-irsa.tfplan
```

The plan contained exactly 2 additions, 0 changes, and 0 destroys. It was
applied with:

```bash
terraform -chdir=infra/terraform apply \
  -input=false /tmp/ray-models-irsa.tfplan
```

Stage 13 was then rerun:

```bash
RAY_IMAGE_TAG=34fe28e-fastapi109-awscli-cachefix \
  ./scripts/bootstrap_cluster.sh --start 13 --end 13
```

Both workers were verified to use and assume the dedicated role:

```bash
kubectl get pod <worker-pod> -n default \
  -o jsonpath='{.spec.serviceAccountName}{"\n"}'

kubectl exec <worker-pod> -n default -c ray-worker -- \
  aws sts get-caller-identity --output json
```

Observed role:

```text
arn:aws:sts::764988411032:assumed-role/rag-ray-models-role/...
```

The embedding model synchronized to 4.3 GB, the LLM model cache reached 9.0 GB,
and both had `config.json` present. `/model-cache` was verified as:

```text
ray:users 755 /model-cache
```

## 8. Ray/FastAPI route-prefix anomaly

The embedding Serve deployment became `RUNNING` and `HEALTHY`, but a smoke test
against `POST /embed/embeddings` returned 404. The live route table correctly
contained `/embed`, and FastAPI's OpenAPI document correctly contained
`POST /embeddings`.

The failure was reproduced directly in Starlette's matcher. Ray 2.9 strips the
Serve route prefix and also sets `root_path`; Starlette 0.35 expects the full
path when `root_path` is set. The incompatible request scope was:

```text
path=/embeddings, root_path=/embed
```

The working scope is:

```text
path=/embed/embeddings, root_path=/embed
```

`services/models/Dockerfile` now patches Ray's proxy from:

```python
proxy_request.set_path(route_path.replace(route_prefix, "", 1))
```

to:

```python
proxy_request.set_path(route_path)
```

### Publish and deploy the route fix

The final compatibility layer was pushed with:

```bash
docker buildx build --progress=plain --push \
  -t 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli-cachefix-routefix \
  -f - . <<'EOF'
FROM 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:34fe28e-fastapi109-awscli-cachefix
USER root
RUN python - <<'PY'
from pathlib import Path
import shutil

proxy = Path('/home/ray/anaconda3/lib/python3.10/site-packages/ray/serve/_private/proxy.py')
text = proxy.read_text()
old = 'proxy_request.set_path(route_path.replace(route_prefix, "", 1))'
new = 'proxy_request.set_path(route_path)'
if old not in text:
    raise SystemExit(f'expected Ray proxy route rewrite not found in {proxy}')
proxy.write_text(text.replace(old, new))
shutil.rmtree(proxy.parent / '__pycache__', ignore_errors=True)
compile(proxy.read_text(), str(proxy), 'exec')
PY
USER ray
EOF
```

Published image:

- Tag: `34fe28e-fastapi109-awscli-cachefix-routefix`
- Digest: `sha256:60e43467c338c10497a53fcc6e3da4be2065f28643a87635852812c4af280539`
- Compressed size: 9,635,702,736 bytes
- Pushed: 2026-09-16 11:51:57 PDT

It was applied with:

```bash
RAY_IMAGE_TAG=34fe28e-fastapi109-awscli-cachefix-routefix \
  ./scripts/bootstrap_cluster.sh --start 13 --end 13
```

KubeRay performed a blue-green replacement and retained the old healthy
RayClusters while starting the new heads. The 16-vCPU G-family quota prevented
the two new GPU workers from coexisting with the old GPU workers. After both
new heads were ready, only the superseded RayClusters were deleted so the new
workers could reuse the two pinned GPU nodes:

```bash
kubectl delete raycluster \
  embed-service-raycluster-hrfd8 \
  llm-service-raycluster-f7tlg \
  -n default
```

Karpenter also consolidated the two temporary CPU nodes that first hosted the
new Ray heads. This restarted both heads once. The replacement heads scheduled
on existing protected nodes, and all current application, Ray head, and GPU
nodes were annotated `karpenter.sh/do-not-disrupt=true` before final checks.

Final status was inspected with:

```bash
kubectl get rayservices,pods,nodeclaims,nodes -o wide
kubectl exec <embed-head-pod> -c ray-head -- serve status
kubectl exec <llm-head-pod> -c ray-head -- serve status
```

Both RayServices reported `Running`; both Serve applications and deployments
reported `RUNNING`/`HEALTHY` with one running replica each.

The embedding endpoint was tested from an API pod:

```bash
kubectl exec api-9c66867cb-fhqxg -n default -- python -c \
  'import json,urllib.request; r=urllib.request.Request("http://embed-service:8000/embed/embeddings",data=json.dumps({"prompt":"cluster recovery smoke test"}).encode(),headers={"Content-Type":"application/json"}); b=json.load(urllib.request.urlopen(r,timeout=60)); print("embedding_dimensions=",len(b["embedding"]))'
```

Result:

```text
embedding_dimensions= 1024
```

The LLM endpoint was tested with:

```bash
kubectl exec api-9c66867cb-fhqxg -n default -- python -c \
  'import json,urllib.request; payload={"messages":[{"role":"user","content":"Reply with exactly: cluster ready"}],"temperature":0,"max_tokens":8}; req=urllib.request.Request("http://llm-service:8000/llm/chat/completions",data=json.dumps(payload).encode(),headers={"Content-Type":"application/json"}); print(json.dumps(json.load(urllib.request.urlopen(req,timeout=120))))'
```

Result:

```json
{"choices": [{"message": {"role": "assistant", "content": "cluster ready"}}]}
```

## Final state

- API: 2/2 running.
- Qdrant: 3/3 running with retained EBS data.
- Neo4j: 1/1 running.
- KubeRay operator: 1/1 running.
- Embedding RayService: `Running`; endpoint returned a 1024-value vector.
- LLM RayService: `Running`; endpoint returned the requested completion.
- GPU capacity: two on-demand `g5.2xlarge` nodes, one model worker per node.
- Ray image: `34fe28e-fastapi109-awscli-cachefix-routefix`.

## Temporary annotations to remove later

During a future maintenance window, after stateful placement and Ray rollout
behavior are hardened, remove the temporary protection annotations with:

```bash
kubectl annotate node \
  ip-10-0-102-19.ec2.internal \
  ip-10-0-102-214.ec2.internal \
  ip-10-0-103-250.ec2.internal \
  ip-10-0-101-233.ec2.internal \
  ip-10-0-102-216.ec2.internal \
  karpenter.sh/do-not-disrupt-
```

Node names are ephemeral; list current nodes and annotations before running the
removal command.

## Operational notes

- Two on-demand `g5.2xlarge` instances are active and incurring GPU cost.
- The account's On-Demand G-family quota is 16 vCPUs, exactly enough for those
  two instances. A replace-before-destroy GPU rollout cannot temporarily add a
  third node under the current quota.
- Karpenter's 30-second empty-node consolidation window is shorter than this
  Ray image's normal pod replacement interval. Use disruption budgets,
  `do-not-disrupt`, or a longer `consolidateAfter` for model rollouts.
- The API currently exits startup if Qdrant is unavailable; the Karpenter/EBS
  movement caused avoidable API crash loops. Retry/backoff work remains part of
  `docs/production-hardening.md`.
- Docker Desktop was 98% full during recovery. The local build cache should be
  pruned deliberately after confirming no needed local-only layers remain.

## UI deployment

The Streamlit UI was containerized and deployed as an internal-only Kubernetes
service. Terraform created the immutable ECR repository `services/ui`, and the
image was built and pushed with:

```bash
docker buildx build --progress=plain --push \
  -t 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ui:34fe28e \
  -f ui/Dockerfile .
```

Published image digest:

```text
sha256:6680445d008156d6675e9a5accaa659bcbf096d6ea4164b901427a292f838c86
```

The deployment was applied with:

```bash
UI_IMAGE_TAG=34fe28e ./scripts/deploy_ui.sh
```

This created `deployment/ui` and the ClusterIP service `ui-service`. The UI
runs as a non-root user with a read-only root filesystem and receives its JWT
signing secret from `app-env-secret`. Managed-auth mode keeps the signing
secret and token controls out of the browser UI. No ingress controller is
installed, so the service was intentionally not exposed to the public internet.

The rollout and in-cluster endpoints were verified with:

```bash
kubectl rollout status deployment/ui -n default --timeout=5m
kubectl exec deployment/api -n default -- python -c \
  'import urllib.request; r=urllib.request.urlopen("http://ui-service/_stcore/health", timeout=10); print(r.status, r.read().decode())'
kubectl exec deployment/api -n default -- python -c \
  'import urllib.request; r=urllib.request.urlopen("http://ui-service/", timeout=10); b=r.read(); print(r.status, len(b))'
```

Both endpoints returned HTTP 200. An authenticated request made with the UI
container's own `make_token` and `stream_chat_events` functions reached the API
and model stack and returned `routing`, `status`, `status`, and `answer` events.

Access the UI from an operator workstation with:

```bash
kubectl port-forward service/ui-service 8501:80 -n default
```

Then open `http://localhost:8501`. Stop the port-forward when finished.
