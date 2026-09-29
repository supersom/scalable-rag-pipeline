import os
import shlex
import subprocess
from pathlib import Path


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


def test_bootstrap_stage_one_only_updates_kubeconfig(tmp_path):
    result, calls = run_bootstrap_with_aws_shim(tmp_path)
    assert result.returncode == 0, result.stderr
    assert any(call[:2] == ["sts", "get-caller-identity"] for call in calls)
    assert any(call[:2] == ["eks", "update-kubeconfig"] for call in calls)
    assert len(calls) == 2
