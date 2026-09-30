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
    assert {"api-local", "fake-models", "otel-collector", "s3-emulator", "qdrant-init"} <= services.keys()
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
    qdrant_init = services["qdrant-init"]
    assert qdrant_init["command"] == ["python", "-m", "services.api.app.local_qdrant_init"]
    assert qdrant_init["environment"]["EMBED_DIM"] == api["environment"]["EMBED_DIM"]
    assert api["depends_on"]["qdrant-init"]["condition"] == "service_completed_successfully"
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


EVIDENCE_FLAGS = ("EVAL_EVIDENCE_ENABLED", "EVAL_CAPTURE_CONTENT")
MANIFEST = "deploy/evaluation/restricted-synthetic-v1.json"
MANIFEST_IN_CONTAINER = "/app/deploy/evaluation/restricted-synthetic-v1.json"


def _compose_config(*files: str) -> dict:
    command = ["docker", "compose", "-p", "scalable-rag-eval", "--profile", "eval-local",
               "--env-file", ".env.example"]
    for name in files:
        command += ["-f", name]
    result = subprocess.run(command + ["config", "--format", "json"], cwd=REPO_ROOT,
                            env=_isolated_environment(), capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _example_environment() -> dict[str, str]:
    values = {}
    for line in (REPO_ROOT / ".env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, _, value = line.partition("=")
            values[key] = value
    return values


def test_example_environment_keeps_evidence_delivery_disabled():
    values = _example_environment()
    assert values["EVAL_EVIDENCE_ENABLED"] == "false"
    assert values["EVAL_CAPTURE_CONTENT"] == "false"
    assert "EVAL_IDENTITY_HASH_KEY" not in values
    assert values["EVAL_QUEUE_NAME"] == "eval-runs"
    assert values["EVAL_DLQ_NAME"] == "eval-runs-dlq"
    assert values["EVAL_QUEUE_URL"] == "http://localhost:14566/123456789012/eval-runs"
    assert values["EVAL_S3_BUCKET"] == values["EVAL_EVIDENCE_BUCKET"] == "eval-traces"
    assert values["EVAL_S3_ENDPOINT_URL"] == values["S3_ENDPOINT_URL"]
    assert values["EVAL_KMS_KEY_ID"]
    assert values["S3_BUCKET_NAME"] == "rag-platform-docs-dev"


def test_example_environment_settings_leave_delivery_disabled(tmp_path):
    shutil.copy(REPO_ROOT / ".env.example", tmp_path / ".env")
    script = """
import json
from services.api.app.config import settings
from services.api.app.evaluation.delivery_config import DeliveryConfig
config = DeliveryConfig.from_settings(settings)
print(json.dumps([config.enabled, config.capture_content]))
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path, env=_isolated_environment(),
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [False, False]


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_default_profile_has_no_evidence_services_or_flags():
    services = _compose_config()["services"]
    assert not {"evidence-migrate", "evidence-delivery"} & services.keys()
    for service in services.values():
        assert not set(EVIDENCE_FLAGS) & set(service.get("environment") or {})
    assert services["s3-emulator"]["healthcheck"]["test"] == [
        "CMD", "python", "-m", "services.aws_emulator.healthcheck"]
    emulator_env = services["s3-emulator"]["environment"]
    assert emulator_env["EVAL_QUEUE_NAME"] == "eval-runs"
    assert emulator_env["EVAL_DLQ_NAME"] == "eval-runs-dlq"
    assert services["api-local"]["environment"]["S3_BUCKET_NAME"] == "rag-platform-docs-dev"
    assert services["api-local"]["environment"]["ENV"] == "dev"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_evidence_override_orders_migrations_before_api_and_worker():
    services = _compose_config("docker-compose.yml", "docker-compose.evaluation.yml")["services"]
    migrate, worker, api = services["evidence-migrate"], services["evidence-delivery"], services["api-local"]

    assert migrate["command"] == ["python", "-m", "services.api.app.evaluation.migrate"]
    assert worker["command"] == ["python", "-m", "services.evaluation_delivery.worker"]
    for service in (migrate, worker):
        assert service["build"] == api["build"]
        assert service["profiles"] == ["eval-local"]
        assert not service.get("ports")
        assert service["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert migrate["restart"] == "no"
    for dependant in (api, worker):
        assert dependant["depends_on"]["evidence-migrate"]["condition"] == "service_completed_successfully"
        assert dependant["depends_on"]["s3-emulator"]["condition"] == "service_healthy"
    assert set(worker["depends_on"]) == {"postgres", "s3-emulator", "evidence-migrate"}
    assert set(migrate["depends_on"]) == {"postgres"}

    emulator_check = services["s3-emulator"]["healthcheck"]["test"]
    assert "services.aws_emulator.healthcheck" in " ".join(emulator_check)
    assert "services.aws_emulator.init_eval_queue --check" in " ".join(emulator_check)


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_evidence_override_enables_flags_only_with_local_dummy_configuration():
    compose = _compose_config("docker-compose.yml", "docker-compose.evaluation.yml")
    services = compose["services"]
    assert "amazonaws.com" not in json.dumps(compose)
    for name in ("api-local", "evidence-delivery"):
        env = services[name]["environment"]
        assert env["ENV"] == "local"
        assert env["EVAL_EVIDENCE_ENABLED"] == "true"
        assert env["EVAL_CAPTURE_CONTENT"] == "true"
        assert env["EVAL_QUEUE_URL"] == "http://s3-emulator:4566/123456789012/eval-runs"
        assert env["EVAL_S3_ENDPOINT_URL"] == "http://s3-emulator:4566"
        assert env["EVAL_S3_BUCKET"] == "eval-traces"
        assert env["EVAL_KMS_KEY_ID"] == "local-evidence-kms-key"
        assert env["EVAL_IDENTITY_HASH_KEY"] and env["EVAL_IDENTITY_HASH_KEY"] != env["JWT_SECRET_KEY"]
        assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == "test"
        assert env["RAY_LLM_ENDPOINT"] == "http://fake-models:8000/v1/chat/completions"
        assert env["RAY_EMBED_ENDPOINT"] == "http://fake-models:8000/api/embeddings"
        assert env["S3_BUCKET_NAME"] == "rag-platform-docs-dev"
        assert env["EVAL_CONTENT_POLICY"] == "restricted-synthetic-v1"
        assert env["EVAL_FIXTURE_MANIFEST"] == MANIFEST_IN_CONTAINER
        mounts = {(v["source"], v["target"], v.get("read_only")) for v in services[name].get("volumes", [])}
        assert (str(REPO_ROOT / MANIFEST), MANIFEST_IN_CONTAINER, True) in mounts
    # Migrations need only the database; the runner never reads evidence flags or keys.
    assert not {*EVIDENCE_FLAGS, "EVAL_IDENTITY_HASH_KEY"} & set(services["evidence-migrate"]["environment"])

    for name, service in services.items():
        for port in service.get("ports", []):
            assert port["host_ip"] == "127.0.0.1", name
    # _isolated_environment exports non-dummy AWS keys; the override must not pass them through.
    for name in ("s3-emulator", "api-local", "evidence-migrate", "evidence-delivery"):
        env = services[name]["environment"]
        assert env["AWS_ACCESS_KEY_ID"] == env["AWS_SECRET_ACCESS_KEY"] == "test", name
    assert services["s3-emulator"]["environment"]["EVAL_EVIDENCE_BUCKET"] == "eval-traces"


@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker Compose is unavailable")
def test_evidence_override_selects_a_case_present_in_the_task_two_manifest():
    from services.api.app.evaluation.provenance import SyntheticManifest

    env = _compose_config("docker-compose.yml", "docker-compose.evaluation.yml")[
        "services"]["api-local"]["environment"]
    manifest = SyntheticManifest.load(REPO_ROOT / MANIFEST)
    assert manifest.policy_version == env["EVAL_CONTENT_POLICY"]
    tenants = {source.tenant_id for source in manifest.sources if source.case_id == env["EVAL_FIXTURE_CASE"]}
    assert tenants == {"synthetic-tenant", "synthetic-tenant-2"}


def _make_recipe(target: str) -> list[str]:
    lines = (REPO_ROOT / "Makefile").read_text().splitlines()
    start = lines.index(f"{target}:") + 1
    recipe = []
    for line in lines[start:]:
        if not line.startswith("\t"):
            break
        recipe.append(line.strip())
    return recipe


def test_default_make_targets_are_unchanged():
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "LOCAL_ENV_FILE := $(if $(wildcard .env.evidence),.env.evidence,.env.example)" in makefile
    assert _make_recipe("local-up") == ["$(LOCAL_COMPOSE) up -d --build --wait"]
    assert _make_recipe("local-down") == ["$(LOCAL_COMPOSE) down"]
    assert _make_recipe("local-check") == ["$(LOCAL_COMPOSE) config --quiet", "pytest tests/local_env -q"]
    assert _make_recipe("local-smoke") == [
        "RUN_LOCAL_STACK_TESTS=1 pytest tests/local_env/test_running_stack.py tests/local_env/test_otel_collector.py -q"]


def test_evidence_make_targets_use_the_override_and_keep_volumes():
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert ("EVIDENCE_COMPOSE = $(LOCAL_COMPOSE) -f docker-compose.yml "
            "-f docker-compose.evaluation.yml\n") in makefile
    recipes = {target: _make_recipe(target) for target in (
        "local-evidence-up", "local-evidence-down", "local-evidence-check", "local-evidence-smoke")}
    assert all(recipes.values())
    up = " ".join(recipes["local-evidence-up"])
    assert "$(EVIDENCE_COMPOSE) up -d --build --wait --wait-timeout" in up
    assert recipes["local-evidence-down"] == ["$(EVIDENCE_COMPOSE) down"]
    assert "$(EVIDENCE_COMPOSE) config --quiet" in recipes["local-evidence-check"]
    assert "RUN_LOCAL_EVIDENCE_TESTS=1" in " ".join(recipes["local-evidence-smoke"])
    for recipe in recipes.values():
        for line in recipe:
            assert " -v" not in line and "--volumes" not in line
