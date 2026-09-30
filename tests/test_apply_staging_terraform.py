"""Exercise the one-time bucket choice without AWS or Terraform access."""

import os
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "apply_staging_terraform.sh"
KEY_ARN = "arn:aws:kms:us-east-1:123456789012:key/test-key"


def _run_with_fake_tools(tmp_path, *, state_resources="", pinned_mode="", bucket_count="1"):
    terraform = tmp_path / "terraform"
    terraform.write_text(
        """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if 'init' in args:
    sys.exit(0)
if 'state' in args:
    print(os.environ['MOCK_STATE_RESOURCES'])
    sys.exit(0)
if 'output' in args:
    mode = os.environ['MOCK_PINNED_MODE']
    if not mode:
        sys.exit(1)
    print(mode)
    sys.exit(0)
if 'plan' in args or 'apply' in args:
    print('TF_ARGS=' + ' '.join(args))
    sys.exit(0)
sys.exit(2)
"""
    )
    terraform.chmod(0o755)
    aws = tmp_path / "aws"
    aws.write_text(
        """#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if 'list-buckets' in args:
    print(os.environ['MOCK_BUCKET_COUNT'])
    sys.exit(0)
if 'get-bucket-encryption' in args:
    if os.environ['MOCK_BUCKET_COUNT'] != '1':
        sys.exit(1)
    print(os.environ['MOCK_KMS_KEY_ARN'])
    sys.exit(0)
sys.exit(2)
"""
    )
    aws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "MOCK_STATE_RESOURCES": state_resources,
        "MOCK_PINNED_MODE": pinned_mode,
        "MOCK_BUCKET_COUNT": bucket_count,
        "MOCK_KMS_KEY_ARN": KEY_ARN,
    }
    return subprocess.run(["bash", str(SCRIPT), "plan"], env=env, capture_output=True, text=True)


@pytest.mark.parametrize(
    ("state_resources", "pinned_mode", "bucket_count", "expected_mode"),
    [
        ("", "", "1", "existing"),
        ("", "", "0", "create"),
        ("aws_s3_bucket.staging_evidence[0]", "create", "1", "create"),
        ("", "existing", "1", "existing"),
    ],
)
def test_bucket_choice_stays_pinned(
    tmp_path, state_resources, pinned_mode, bucket_count, expected_mode
):
    result = _run_with_fake_tools(
        tmp_path,
        state_resources=state_resources,
        pinned_mode=pinned_mode,
        bucket_count=bucket_count,
    )
    assert result.returncode == 0, result.stderr
    assert f"({expected_mode}; persisted in Terraform state after apply)" in result.stdout
    assert f"-var=staging_evidence_bucket_mode={expected_mode}" in result.stdout
    if expected_mode == "existing":
        assert f"-var=staging_evidence_existing_kms_key_arn={KEY_ARN}" in result.stdout


def test_missing_pinned_bucket_does_not_flip_to_create(tmp_path):
    result = _run_with_fake_tools(tmp_path, pinned_mode="existing", bucket_count="0")
    assert result.returncode != 0
    assert "TF_ARGS=" not in result.stdout
