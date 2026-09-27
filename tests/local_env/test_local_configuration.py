import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


def _isolated_environment() -> dict[str, str]:
    keep = ("PATH", "SYSTEMROOT", "WINDIR")
    environment = {key: os.environ[key] for key in keep if key in os.environ}
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT),
            "AWS_EC2_METADATA_DISABLED": "true",
            "AWS_ACCESS_KEY_ID": "local-test-access-key",
            "AWS_SECRET_ACCESS_KEY": "local-test-secret-key",
        }
    )
    return environment


def test_example_environment_bootstraps_settings_and_uses_non_api_model_port(tmp_path):
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env")
    script = """
import json
from services.api.app.config import settings
print(json.dumps({
    "jwt": settings.JWT_SECRET_KEY,
    "llm": settings.RAY_LLM_ENDPOINT,
    "embed": settings.RAY_EMBED_ENDPOINT,
}))
"""

    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_isolated_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    loaded = json.loads(result.stdout)
    assert loaded == {
        "jwt": "local-dev-only-change-me",
        "llm": "http://localhost:8001/v1/chat/completions",
        "embed": "http://localhost:8001/api/embeddings",
    }


def test_api_module_imports_without_real_aws_credentials(tmp_path):
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from services.api.main import app; print(app.title)",
        ],
        cwd=tmp_path,
        env=_isolated_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "Enterprise RAG Platform"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_eval_local_compose_profile_is_self_contained():
    result = subprocess.run(
        [
            "docker",
            "compose",
            "--profile",
            "eval-local",
            "--env-file",
            ".env.example",
            "config",
            "--format",
            "json",
        ],
        cwd=REPO_ROOT,
        env=_isolated_environment(),
        capture_output=True,
        text=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    compose = json.loads(result.stdout)
    services = compose["services"]
    assert {"api-local", "fake-models", "otel-collector", "s3-emulator"} <= services.keys()
    assert "minio" not in services
    assert "localstack" not in services
    assert services["s3-emulator"]["build"]["dockerfile"] == "services/aws_emulator/Dockerfile"

    assert str(services["postgres"]["ports"][0]["published"]) == "15432"
    assert str(services["redis"]["ports"][0]["published"]) == "16379"
    assert str(services["qdrant"]["ports"][0]["published"]) == "16333"
    neo4j_ports = {str(port["published"]) for port in services["neo4j"]["ports"]}
    assert {"17474", "17687"} <= neo4j_ports

    published_services = (
        "api-local",
        "fake-models",
        "neo4j",
        "otel-collector",
        "postgres",
        "qdrant",
        "redis",
        "s3-emulator",
    )
    assert all(
        port["host_ip"] == "127.0.0.1"
        for service_name in published_services
        for port in services[service_name]["ports"]
    )

    fake_port = services["fake-models"]["ports"][0]
    assert str(fake_port["published"]) == "8001"
    assert str(fake_port["target"]) == "8000"

    api = services["api-local"]
    assert str(api["ports"][0]["published"]) == "18000"
    assert str(api["ports"][0]["target"]) == "8000"
    assert api["environment"]["RAY_LLM_ENDPOINT"] == "http://fake-models:8000/v1/chat/completions"
    assert api["environment"]["RAY_EMBED_ENDPOINT"] == "http://fake-models:8000/api/embeddings"
    assert api["environment"]["S3_ENDPOINT_URL"] == "http://s3-emulator:4566"
    assert api["environment"]["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://otel-collector:4317"

    s3_port = services["s3-emulator"]["ports"][0]
    assert str(s3_port["published"]) == "14566"
    assert str(s3_port["target"]) == "4566"

    collector_ports = {
        (str(port["published"]), str(port["target"]))
        for port in services["otel-collector"]["ports"]
    }
    assert {("14317", "4317"), ("14318", "4318")} <= collector_ports
