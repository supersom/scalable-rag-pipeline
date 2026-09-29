# Shared EKS Cluster for Staging Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make staging evidence deployment depend explicitly on the shared `rag-platform-cluster`, with cluster creation owned only by `infra/terraform` and no Karpenter/KubeRay installation in the staging flow.

**Architecture:** Keep `infra/terraform` as the sole owner of EKS and keep `infra/terraform-staging` as a consumer that looks up the shared cluster. Add an early active-cluster preflight to the staging deploy script, then document the reviewed owner-state plan/apply and kubeconfig sequence before staging Terraform and namespace deployment.

**Tech Stack:** Terraform 1.5+, AWS CLI, EKS, Bash, pytest.

**Spec:** [Shared EKS Cluster for Staging Evidence Validation](../specs/2026-09-29-shared-eks-cluster-for-staging-design.md)

## Global Constraints

- `infra/terraform` remains the canonical owner of the shared cluster.
- `infra/terraform-staging` remains a consumer. Its EKS, VPC, OIDC, and subnet lookups are retained; it does not declare an EKS resource or try to fall back from a failed data lookup to creating one.
- Staging evidence resources and workloads use that same EKS cluster, AWS account, and region, isolated in the `staging-evidence` Kubernetes namespace.
- Keep Karpenter and KubeRay controllers and their provisioner resources out of this staging workflow.
- Do not automatically apply Terraform from `deploy_staging_evidence.sh`.
- Do not change the existing baseline EKS configuration or resize its managed node group.
- Each Terraform apply and Kubernetes deployment remains an explicit operator action after review.

## Files and Responsibilities

- `scripts/deploy_staging_evidence.sh`: verify the expected EKS cluster is `ACTIVE` before ECR lookups or Kubernetes mutations; fail with a fixed, actionable error and no raw AWS exception text.
- `tests/local_env/test_staging_cluster_prerequisite.py`: use temporary AWS CLI shims to prove missing, non-active, access-denied, and active-cluster behavior without contacting AWS or Kubernetes.
- `scripts/bootstrap_cluster.sh`: no behavior change expected; its existing `--start 1 --end 1` selection configures kubeconfig only. A test will pin that Karpenter and KubeRay stages are not invoked by this selection.
- `docs/evaluation_implementation/evidence-delivery-operations.md`: document the canonical cluster owner, explicit plan/apply sequence, kubeconfig step, skipped bootstrap stages, and staging command order.
- `docs/superpowers/plans/2026-09-28-pr5a-production-evidence-activation.md`: update Task 13 so it provisions/reconciles the shared cluster from its canonical state before planning/applying staging resources.
- `infra/terraform-staging/data.tf` and `infra/terraform-staging/variables.tf`: remain unchanged; their current lookup-only definitions are retained. No EKS resource is added here.

## Review Focus

- Missing cluster: staging exits before any ECR request or Kubernetes mutation and directs the operator to the canonical Terraform owner plan.
- Cluster in `CREATING` or another non-`ACTIVE` state: staging exits without attempting deployment and reports only the state, not raw CLI errors.
- AWS access denied or wrong region/account: staging exits with a fixed message that directs the operator to check identity, permissions, and region without exposing exception text.
- Active cluster: preflight passes and the deploy flow reaches the namespace creation step.
- Bootstrap selection `--start 1 --end 1`: only kubeconfig configuration runs; no Karpenter, NodePool, or KubeRay command runs.

---

### Task 1: Guard staging deployment on the shared active cluster

**Files:**
- Modify: `scripts/deploy_staging_evidence.sh`
- Create: `tests/local_env/test_staging_cluster_prerequisite.py`

**Interfaces:**
- Produces: `deploy_staging_evidence.sh` checks `aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION"` before ECR lookups or Kubernetes commands. `CLUSTER_NAME` defaults to `rag-platform-cluster`; only status `ACTIVE` passes.
- Consumes: AWS credentials and the existing `REGION` environment setting. The staging Terraform root continues to consume the same cluster through its current data source.

- [ ] **Step 1: Write the failing preflight tests**

Create pytest helpers that put temporary fake `aws` and `kubectl` executables first on `PATH` and record each argument vector to a temp log. The AWS shim returns an account ID for `sts get-caller-identity`, returns `staging` for both ECR image-tag lookups, prints a chosen EKS status or a simulated `AccessDeniedException` for `eks describe-cluster`, and exits nonzero for the missing/denied cases. The kubectl shim logs the call and exits with a sentinel status so tests stop at the first Kubernetes boundary.

Use this concrete helper for staging deployment tests:

```python
ROOT = Path(__file__).resolve().parents[2]


def _write_executable(path, source):
    path.write_text(source)
    path.chmod(0o755)


def run_staging_deploy_with_aws(tmp_path, *, describe_result):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    _write_executable(bin_dir / "aws", '''#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$COMMAND_LOG"
if [[ "$1 $2" == "sts get-caller-identity" ]]; then echo 764988411032; exit 0; fi
if [[ "$1 $2" == "ecr describe-images" ]]; then echo staging; exit 0; fi
if [[ "$1 $2" == "eks describe-cluster" ]]; then
  case "$FAKE_EKS_RESULT" in
    ACTIVE|CREATING) echo "$FAKE_EKS_RESULT"; exit 0 ;;
    missing) echo "ResourceNotFoundException" >&2; exit 254 ;;
    denied) echo "AccessDeniedException" >&2; exit 254 ;;
  esac
fi
exit 92
''')
    _write_executable(bin_dir / "kubectl", '''#!/usr/bin/env bash
printf 'kubectl %s\\n' "$*" >> "$COMMAND_LOG"
exit 91
''')
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
        "FAKE_EKS_RESULT": describe_result,
        "REGION": "us-east-1",
    }
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/deploy_staging_evidence.sh")],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )
    calls = [shlex.split(line) for line in log.read_text().splitlines()]
    return result, calls
```

Import `os`, `shlex`, `subprocess`, and `Path` at the test module's top. For the bootstrap test, use a separate fake AWS script that logs calls, returns an account ID for STS and success for `eks update-kubeconfig`, and exits nonzero for any other AWS command; set `PATH` to put it first and invoke the script with `--start 1 --end 1`.

```python
def test_missing_cluster_fails_before_ecr_or_kubectl(tmp_path):
    result, calls = run_staging_deploy_with_aws(tmp_path, describe_result="missing")
    assert result.returncode != 0
    assert any(call[:3] == ["aws", "eks", "describe-cluster"] for call in calls)
    assert "rag-platform-cluster" in result.stderr
    assert "infra/terraform" in result.stderr
    assert not any(call[:3] == ["aws", "ecr", "describe-images"] for call in calls)
    assert not any(call[0] == "kubectl" for call in calls)


def test_non_active_cluster_fails_before_kubectl(tmp_path):
    result, calls = run_staging_deploy_with_aws(tmp_path, describe_result="CREATING")
    assert result.returncode != 0
    assert "CREATING" in result.stderr
    assert not any(call[0] == "kubectl" for call in calls)


def test_cluster_access_error_is_reported_without_raw_aws_error(tmp_path):
    result, calls = run_staging_deploy_with_aws(tmp_path, describe_result="denied")
    assert result.returncode != 0
    assert "check AWS account, region, and EKS access" in result.stderr
    assert "AccessDeniedException" not in result.stderr
    assert not any(call[0] == "kubectl" for call in calls)


def test_active_cluster_passes_preflight_to_namespace_creation(tmp_path):
    result, calls = run_staging_deploy_with_aws(tmp_path, describe_result="ACTIVE")
    assert any(call[:3] == ["aws", "eks", "describe-cluster"] for call in calls)
    assert any(call[:3] == ["kubectl", "create", "namespace"] for call in calls)
    assert "not ACTIVE" not in result.stderr
    aws_calls = [index for index, call in enumerate(calls) if call[0] == "aws"]
    describe_index = next(index for index in aws_calls if calls[index][1:3] == ["eks", "describe-cluster"])
    ecr_indices = [index for index in aws_calls if calls[index][1:3] == ["ecr", "describe-images"]]
    assert ecr_indices and describe_index < min(ecr_indices)
```

The fake `kubectl` records the first call and returns a sentinel nonzero code; do not run real AWS, Terraform, Helm, or Kubernetes commands in these tests.

- [ ] **Step 2: Run the tests and verify the expected failure**

Run: `pytest tests/local_env/test_staging_cluster_prerequisite.py -q`

Expected: the tests fail because the current script proceeds to ECR/Kubernetes setup without calling `eks describe-cluster`.

- [ ] **Step 3: Add the active-cluster preflight**

In `scripts/deploy_staging_evidence.sh`, immediately after resolving `ACCOUNT_ID` and `ECR_BASE`, add:

```bash
CLUSTER_NAME="${CLUSTER_NAME:-rag-platform-cluster}"
if ! CLUSTER_STATUS="$(aws eks describe-cluster --name "$CLUSTER_NAME" --region "$REGION" \
  --query 'cluster.status' --output text 2>/dev/null)"; then
  echo "ERROR: cannot verify EKS cluster ${CLUSTER_NAME}; check AWS account, region, and EKS access. If it is not provisioned, review and apply the canonical infra/terraform plan first." >&2
  exit 1
fi
if [[ "$CLUSTER_STATUS" != "ACTIVE" ]]; then
  echo "ERROR: EKS cluster ${CLUSTER_NAME} is ${CLUSTER_STATUS}, not ACTIVE; wait for the canonical infra/terraform owner state to finish before deploying staging." >&2
  exit 1
fi
```

Keep the output fixed and non-sensitive; do not print AWS CLI exception text. This is a preflight only: it must not run `terraform apply` or any Kubernetes command.

- [ ] **Step 4: Run the tests and verify they pass**

Run: `pytest tests/local_env/test_staging_cluster_prerequisite.py -q`

Expected: all four cases pass; missing, non-active, and access-error paths make no ECR or Kubernetes call, while `ACTIVE` reaches the namespace command.

- [ ] **Step 5: Pin the kubeconfig-only bootstrap selection**

Add this test in the same file:

```python
def test_bootstrap_stage_one_only_updates_kubeconfig(tmp_path):
    result, calls = run_bootstrap_with_aws_shim(tmp_path)
    assert result.returncode == 0, result.stderr
    assert any(call[:2] == ["sts", "get-caller-identity"] for call in calls)
    assert any(call[:2] == ["eks", "update-kubeconfig"] for call in calls)
    assert len(calls) == 2
```

Implement `run_bootstrap_with_aws_shim` in the same test module:

```python
def run_bootstrap_with_aws_shim(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "bootstrap-commands.log"
    _write_executable(bin_dir / "aws", '''#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$COMMAND_LOG"
if [[ "$1 $2" == "sts get-caller-identity" ]]; then echo 764988411032; exit 0; fi
if [[ "$1 $2" == "eks update-kubeconfig" ]]; then exit 0; fi
exit 92
''')
    for command in ("helm", "kubectl"):
        _write_executable(bin_dir / command, f'''#!/usr/bin/env bash
printf '{command} %s\\n' "$*" >> "$COMMAND_LOG"
exit 91
''')
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/bootstrap_cluster.sh"), "--start", "1", "--end", "1"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=20,
    )
    return result, [shlex.split(line) for line in log.read_text().splitlines()]
```

Because the selected bootstrap stage contains no Helm or kubectl commands and the AWS shim fails on any other AWS subcommand, the call assertions prove Karpenter/KubeRay stages were skipped.

- [ ] **Step 6: Run the test and relevant syntax checks**

Run: `pytest tests/local_env/test_staging_cluster_prerequisite.py -q`

Expected: all five tests pass.

Run: `bash -n scripts/deploy_staging_evidence.sh`

Expected: exit 0 with no output.

Run: `bash -n scripts/bootstrap_cluster.sh`

Expected: exit 0 with no output.

- [ ] **Step 7: Run the existing local configuration suite**

Run: `pytest tests/local_env/test_local_configuration.py -q`

Expected: all tests pass; Docker-dependent cases may skip only when Docker is unavailable.

- [ ] **Step 8: Commit**

```bash
git add scripts/deploy_staging_evidence.sh tests/local_env/test_staging_cluster_prerequisite.py
git commit -m "fix(staging): require the shared EKS cluster before deployment"
```

### Task 2: Document canonical cluster provisioning and staging order

**Files:**
- Modify: `docs/evaluation_implementation/evidence-delivery-operations.md`
- Modify: `docs/superpowers/plans/2026-09-28-pr5a-production-evidence-activation.md`

**Interfaces:**
- Consumes: Task 1's preflight contract and the existing owner/consumer split: `infra/terraform` creates/reconciles; `infra/terraform-staging` only reads the shared EKS cluster.
- Produces: an operator runbook that never applies infrastructure implicitly and explicitly excludes bootstrap stages 2, 3, and 7.

- [ ] **Step 1: Document how to provision or reconcile the cluster**

Add a “Shared EKS prerequisite for staging” section to `docs/evaluation_implementation/evidence-delivery-operations.md` containing this sequence and safety note:

```bash
terraform -chdir=infra/terraform init
terraform -chdir=infra/terraform plan
# Review the complete plan; it may include non-EKS resources owned by this root.
# Only after review/approval:
terraform -chdir=infra/terraform apply

bash scripts/bootstrap_cluster.sh --start 1 --end 1
terraform -chdir=infra/terraform-staging init
terraform -chdir=infra/terraform-staging plan
# Review the staging plan, then apply it explicitly:
terraform -chdir=infra/terraform-staging apply

bash scripts/deploy_staging_evidence.sh
```

Explain that `infra/terraform` is the only state that owns EKS; staging Terraform only reads it; if a cluster exists outside the canonical state, stop and import/reconcile deliberately rather than letting a second state adopt it; and bootstrap stages 2–3 (Karpenter) and 7 (KubeRay) are intentionally not run. State that `--start 1 --end 1` only configures kubeconfig, and that the existing baseline two-node `m6i.large` managed node group remains active.

- [ ] **Step 2: Update PR 5a Task 13 prerequisites**

In Task 13 of `docs/superpowers/plans/2026-09-28-pr5a-production-evidence-activation.md`, insert canonical-owner provisioning before staging Terraform initialization. Keep the task's existing live-action approval gates: review the canonical plan before applying; then update kubeconfig with `--start 1 --end 1`; then initialize/plan/apply `infra/terraform-staging`; only then deploy the staging namespace. Record that the staging deploy script preflight requires cluster status `ACTIVE` and that the shared cluster is not automatically created by the staging deploy script.

- [ ] **Step 3: Check the documentation against the approved spec**

Run: `rg -n 'infra/terraform|infra/terraform-staging|--start 1 --end 1|Karpenter|KubeRay|ACTIVE' docs/evaluation_implementation/evidence-delivery-operations.md docs/superpowers/plans/2026-09-28-pr5a-production-evidence-activation.md`

Expected: both documents describe the same owner/consumer flow, the active-cluster preflight, explicit plan review, and skipped bootstrap stages; no instruction tells staging Terraform to create or import EKS.

- [ ] **Step 4: Verify Terraform remains valid and lookup-only in staging**

Run: `terraform -chdir=infra/terraform fmt -check evidence.tf`

Expected: exit 0 with no formatting output.

Run: `terraform -chdir=infra/terraform validate`

Expected: `Success! The configuration is valid.` No production apply is run as part of implementation.

Run: `terraform -chdir=infra/terraform-staging fmt -check evidence.tf`

Expected: exit 0 with no formatting output.

Run: `terraform -chdir=infra/terraform-staging validate`

Expected: `Success! The configuration is valid.` No EKS resource is added to the staging root.

- [ ] **Step 5: Run the final local suites and inspect the diff**

Run: `pytest tests/local_env/test_staging_cluster_prerequisite.py tests/local_env/test_local_configuration.py -q`

Expected: all runnable tests pass; only Docker-dependent tests may skip if Docker is unavailable.

Run: `git diff --check`

Expected: no whitespace errors.

- [ ] **Step 6: Commit**

```bash
git add docs/evaluation_implementation/evidence-delivery-operations.md docs/superpowers/plans/2026-09-28-pr5a-production-evidence-activation.md
git commit -m "docs(staging): describe shared cluster provisioning workflow"
```


## Live Task 13 Boundary

This implementation plan does not apply AWS or Kubernetes changes. After code and documentation are reviewed, Task 13 still requires live execution against the configured account: the operator must review and explicitly approve the `infra/terraform` plan, apply it, verify the cluster becomes `ACTIVE`, then review and approve the staging Terraform plan/apply and deployment steps. If the owner-state plan contains unexpected changes, stop rather than applying it.
