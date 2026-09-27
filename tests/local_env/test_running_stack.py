import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(
    os.getenv("RUN_LOCAL_STACK_TESTS") != "1",
    reason="set RUN_LOCAL_STACK_TESTS=1 after make local-up",
)
def test_running_local_stack_smoke_contract():
    result = subprocess.run(
        [sys.executable, "scripts/smoke_local_env.py"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "api": "ok",
        "collector": "ok",
        "evidence_bucket": "versioned",
        "fake_models": "ok",
        "stream": "ok",
    }
