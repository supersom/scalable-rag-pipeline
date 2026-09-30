import os
from pathlib import Path
import shutil
import subprocess

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _dry_run(tmp_path: Path, with_private_file: bool) -> str:
    shutil.copy(REPO_ROOT / "Makefile", tmp_path / "Makefile")
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env.example")
    if with_private_file:
        (tmp_path / ".env.evidence").write_text("LLM_API_ENDPOINT=https://example.invalid/v1/chat/completions\n")
    environment = dict(os.environ)
    environment["LLM_API_ENDPOINT"] = "https://exported.invalid/v1/chat/completions"
    environment["LLM_API_KEY"] = "exported-secret"
    result = subprocess.run(["make", "-n", "local-up"], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, check=True)
    return result.stdout


def test_local_stack_uses_safe_sample_and_clears_exported_hosted_settings(tmp_path):
    command = _dry_run(tmp_path, with_private_file=False)
    assert "--env-file .env.example" in command
    assert "env -u LLM_API_ENDPOINT" in command
    assert "-u LLM_API_KEY" in command


def test_local_stack_prefers_private_file_without_clearing_hosted_settings(tmp_path):
    command = _dry_run(tmp_path, with_private_file=True)
    assert "--env-file .env.evidence" in command
    assert "env -u LLM_API_ENDPOINT" not in command


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_fallback_compose_stays_on_fake_models_despite_exported_provider_settings():
    environment = dict(os.environ)
    environment.update({
        "LLM_API_ENDPOINT": "https://exported.invalid/v1/chat/completions",
        "LLM_API_KEY": "exported-secret",
        "EMBED_API_ENDPOINT": "https://exported.invalid/v1/embeddings",
        "EMBED_API_KEY": "exported-secret",
        "EMBED_DIM": "999",
        "RERANK_API_ENDPOINT": "https://exported.invalid/v1/rerank",
    })
    make = subprocess.run(["make", "-n", "LOCAL_ENV_FILE=.env.example", "local-check"],
                          cwd=REPO_ROOT, env=environment, capture_output=True, text=True, check=True)
    compose_command = make.stdout.splitlines()[0].replace("config --quiet", "config --format json")
    result = subprocess.run(compose_command, shell=True, cwd=REPO_ROOT, env=environment,
                            capture_output=True, text=True, check=True)
    import json
    services = json.loads(result.stdout)["services"]
    api = services["api-local"]["environment"]
    assert api["LLM_API_ENDPOINT"] == api["EMBED_API_ENDPOINT"] == api["RERANK_API_ENDPOINT"] == ""
    assert api["LLM_API_KEY"] == api["EMBED_API_KEY"] == ""
    assert api["EMBED_DIM"] == services["qdrant-init"]["environment"]["EMBED_DIM"] == "1024"


def test_all_private_env_files_are_ignored_but_example_is_not():
    for path in (".env", ".env.evidence", ".env.local", "services/api/.env.production"):
        result = subprocess.run(["git", "check-ignore", "--no-index", "-q", path],
                                cwd=REPO_ROOT)
        assert result.returncode == 0, path
    result = subprocess.run(["git", "check-ignore", "--no-index", "-q", ".env.example"],
                            cwd=REPO_ROOT)
    assert result.returncode == 1
