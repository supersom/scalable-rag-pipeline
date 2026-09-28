import json
import os
from pathlib import Path
import shutil
import subprocess

import boto3
import pytest
from moto import mock_aws


REPO_ROOT = Path(__file__).resolve().parents[2]
# Mirrors the Makefile's EVIDENCE_COMPOSE; the project is overridable for an isolated stack.
EVIDENCE_COMPOSE = [
    "docker", "compose", "-p", os.getenv("LOCAL_COMPOSE_PROJECT", "scalable-rag-eval"), "--profile", "eval-local", "--env-file", ".env.example",
    "-f", "docker-compose.yml", "-f", "docker-compose.evaluation.yml",
]


@pytest.fixture
def sqs():
    with mock_aws():
        yield boto3.client("sqs", region_name="us-east-1", aws_access_key_id="test", aws_secret_access_key="test")


def _redrive(sqs, queue_url):
    attrs = sqs.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["RedrivePolicy"])["Attributes"]
    return json.loads(attrs["RedrivePolicy"])


def test_queue_initialization_is_idempotent(sqs):
    from services.aws_emulator.init_eval_queue import ensure_queues
    first = ensure_queues(sqs, "eval-runs", "eval-runs-dlq")
    assert ensure_queues(sqs, "eval-runs", "eval-runs-dlq") == first
    attrs = sqs.get_queue_attributes(QueueUrl=first["queue_url"],
                                     AttributeNames=["RedrivePolicy"])["Attributes"]
    assert "RedrivePolicy" in attrs


def test_redrive_targets_the_dead_letter_queue(sqs):
    from services.aws_emulator.init_eval_queue import MAX_RECEIVE_COUNT, ensure_queues
    queues = ensure_queues(sqs, "eval-runs", "eval-runs-dlq")

    assert set(queues) == {"queue_url", "queue_arn", "dlq_url", "dlq_arn"}
    assert queues["dlq_arn"].endswith(":eval-runs-dlq")
    policy = _redrive(sqs, queues["queue_url"])
    assert policy["deadLetterTargetArn"] == queues["dlq_arn"]
    assert int(policy["maxReceiveCount"]) == MAX_RECEIVE_COUNT
    assert "RedrivePolicy" not in sqs.get_queue_attributes(
        QueueUrl=queues["dlq_url"], AttributeNames=["All"])["Attributes"]


def test_existing_queue_without_redrive_is_repaired(sqs):
    from services.aws_emulator.init_eval_queue import ensure_queues
    sqs.create_queue(QueueName="eval-runs")

    queues = ensure_queues(sqs, "eval-runs", "eval-runs-dlq")

    assert _redrive(sqs, queues["queue_url"])["deadLetterTargetArn"] == queues["dlq_arn"]


def test_check_rejects_missing_queues_and_accepts_initialized_ones(sqs):
    from services.aws_emulator.init_eval_queue import QueueNotReady, check_queues, ensure_queues
    with pytest.raises(QueueNotReady):
        check_queues(sqs, "eval-runs", "eval-runs-dlq")
    sqs.create_queue(QueueName="eval-runs-dlq")
    sqs.create_queue(QueueName="eval-runs")
    with pytest.raises(QueueNotReady):
        check_queues(sqs, "eval-runs", "eval-runs-dlq")

    ensure_queues(sqs, "eval-runs", "eval-runs-dlq")

    check_queues(sqs, "eval-runs", "eval-runs-dlq")


def test_cli_prints_only_safe_status(sqs, capsys):
    from services.aws_emulator import init_eval_queue
    assert init_eval_queue.main([], client=sqs) == 0
    assert init_eval_queue.main(["--check"], client=sqs) == 0

    output = capsys.readouterr()
    assert output.out.splitlines() == [
        "evaluation queues ready: queue=eval-runs dlq=eval-runs-dlq redrive=configured",
        "evaluation queues ready: queue=eval-runs dlq=eval-runs-dlq redrive=configured",
    ]
    for unsafe in ("http", "arn:", "123456789012", "test"):
        assert unsafe not in output.out
    assert output.err == ""


def test_cli_check_reports_not_ready_without_details(sqs, capsys):
    from services.aws_emulator import init_eval_queue
    assert init_eval_queue.main(["--check"], client=sqs) == 1
    assert capsys.readouterr().err.strip() == "evaluation queues not ready: queue=eval-runs dlq=eval-runs-dlq"


def test_emulator_installs_sqs_explicitly():
    requirements = (REPO_ROOT / "services/aws_emulator/requirements.txt").read_text().split()
    assert "moto[server,s3,sqs]==5.0.12" in requirements
    assert "boto3==1.34.34" in requirements


def test_entrypoint_initializes_queues_after_s3_readiness():
    lines = [line.strip() for line in (REPO_ROOT / "services/aws_emulator/entrypoint.sh").read_text().splitlines()]
    server = lines.index('moto_server -H 0.0.0.0 -p 4566 &')
    s3 = lines.index("python -m services.aws_emulator.init_s3")
    queues = lines.index("python -m services.aws_emulator.init_eval_queue")
    wait = lines.index('wait "${server_pid}"')
    assert server < s3 < queues < wait
    assert sum("init_" in line for line in lines) == 2


def _compose_services(command):
    result = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]


@pytest.mark.skipif(os.getenv("RUN_LOCAL_EVIDENCE_TESTS") != "1",
                    reason="set RUN_LOCAL_EVIDENCE_TESTS=1 after make local-evidence-up")
def test_running_evidence_stack_smoke_contract():
    assert shutil.which("docker"), "Docker Compose is required after opting in"
    containers = {c["Service"]: c for c in _compose_services(EVIDENCE_COMPOSE + ["ps", "-a", "--format", "json"])}
    assert containers["evidence-migrate"]["State"] == "exited"
    assert containers["evidence-migrate"]["ExitCode"] == 0
    assert containers["evidence-delivery"]["State"] == "running"
    assert containers["api-local"]["Health"] == "healthy"
    assert containers["s3-emulator"]["Health"] == "healthy"

    endpoint = os.getenv("S3_ENDPOINT_URL", "http://127.0.0.1:14566")
    sqs = boto3.client("sqs", endpoint_url=endpoint, region_name="us-east-1",
                       aws_access_key_id="test", aws_secret_access_key="test")
    from services.aws_emulator.init_eval_queue import check_queues
    check_queues(sqs, "eval-runs", "eval-runs-dlq")
