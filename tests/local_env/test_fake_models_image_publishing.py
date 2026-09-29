import os
import shlex
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _write_executable(path, source):
    path.write_text(source)
    path.chmod(0o755)


def _run_script(tmp_path, script, args, *, aws_script, docker_script):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "commands.log"
    _write_executable(bin_dir / "aws", aws_script)
    _write_executable(bin_dir / "docker", docker_script)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "COMMAND_LOG": str(log)}
    result = subprocess.run(
        ["bash", str(ROOT / script), *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    calls = [shlex.split(line) for line in log.read_text().splitlines()] if log.exists() else []
    return result, calls


def test_build_push_image_archives_fake_models_to_expected_s3_key(tmp_path):
    result, calls = _run_script(
        tmp_path,
        "scripts/build_push_image.sh",
        ["fake-models"],
        aws_script='''#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$COMMAND_LOG"
if [[ "$1 $2" == "sts get-caller-identity" ]]; then echo 764988411032; exit 0; fi
if [[ "$1 $2" == "s3 ls" ]]; then exit 0; fi
if [[ "$1 $2" == "s3 cp" ]]; then cat >/dev/null; exit 0; fi
exit 92
''',
        docker_script='''#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$COMMAND_LOG"
if [[ "$1" == "save" ]]; then printf 'fake docker archive'; fi
''',
    )
    expected_sha = subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    assert result.returncode == 0, result.stderr
    assert ["docker", "build", "-t", f"services/fake-models:{expected_sha}", "-f",
            "services/fake_models/Dockerfile", "."] in calls
    assert ["aws", "s3", "cp", "-",
            f"s3://rag-platform-models-prod-7649/images/services/fake-models/{expected_sha}.tar.gz",
            "--region", "us-east-1", "--no-progress"] in calls


def test_sync_s3_to_ecr_promotes_fake_models_image(tmp_path):
    result, calls = _run_script(
        tmp_path,
        "scripts/sync_s3_to_ecr.sh",
        ["fake-models", "abc1234"],
        aws_script='''#!/usr/bin/env bash
printf 'aws %s\\n' "$*" >> "$COMMAND_LOG"
if [[ "$1 $2" == "sts get-caller-identity" ]]; then echo 764988411032; exit 0; fi
if [[ "$1 $2" == "ecr get-login-password" ]]; then echo token; exit 0; fi
if [[ "$1 $2" == "s3 cp" ]]; then python3 -c 'import gzip,sys; sys.stdout.buffer.write(gzip.compress(b"docker archive"))'; exit 0; fi
if [[ "$1 $2" == "ecr describe-images" ]]; then exit 0; fi
exit 92
''',
        docker_script='''#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$COMMAND_LOG"
case "$1" in
  login|load) cat >/dev/null ;;
esac
''',
    )

    assert result.returncode == 0, result.stderr
    assert ["docker", "tag", "services/fake-models:abc1234",
            "764988411032.dkr.ecr.us-east-1.amazonaws.com/services/fake-models:abc1234"] in calls
    assert ["docker", "push",
            "764988411032.dkr.ecr.us-east-1.amazonaws.com/services/fake-models:abc1234"] in calls
