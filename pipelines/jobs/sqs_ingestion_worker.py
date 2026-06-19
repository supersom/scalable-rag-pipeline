"""Consume S3 object-created events from SQS and run ingestion as Ray jobs."""

import hashlib
import json
import logging
import os
import shlex
import time
from urllib.parse import quote, unquote_plus

import boto3
from ray.job_submission import JobStatus, JobSubmissionClient

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
logger = logging.getLogger(__name__)

QUEUE_URL = os.environ["INGESTION_QUEUE_URL"]
RAY_JOBS_ADDRESS = os.getenv(
    "RAY_JOBS_ADDRESS", "http://ingestion-ray-head-svc:8265"
)
SUPPORTED_EXTENSIONS = {"pdf", "html", "htm", "docx", "txt", "ppt", "pptx"}
VISIBILITY_SECONDS = int(os.getenv("INGESTION_VISIBILITY_SECONDS", "900"))
JOB_TIMEOUT_SECONDS = int(os.getenv("INGESTION_JOB_TIMEOUT_SECONDS", "7200"))

FORWARDED_ENV = (
    "AWS_REGION",
    "QDRANT_HOST",
    "QDRANT_PORT",
    "QDRANT_COLLECTION",
    "NEO4J_URI",
    "NEO4J_USER",
    "NEO4J_PASSWORD",
    "RAY_EMBED_ENDPOINT",
    "EMBED_MODEL_NAME",
    "RAY_LLM_ENDPOINT",
    "LLM_MODEL_NAME",
)


def _records(body: str) -> list[dict]:
    payload = json.loads(body)
    if "Message" in payload:  # SNS-wrapped S3 event
        payload = json.loads(payload["Message"])
    return payload.get("Records", [])


def _wait_for_job(client, sqs, receipt_handle: str, submission_id: str) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    next_extension = 0.0
    while time.monotonic() < deadline:
        status = client.get_job_status(submission_id)
        if status == JobStatus.SUCCEEDED:
            return
        if status in (JobStatus.FAILED, JobStatus.STOPPED):
            logs = client.get_job_logs(submission_id)
            raise RuntimeError(f"Ray job {submission_id} ended as {status}:\n{logs[-4000:]}")

        now = time.monotonic()
        if now >= next_extension:
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL,
                ReceiptHandle=receipt_handle,
                VisibilityTimeout=VISIBILITY_SECONDS,
            )
            next_extension = now + max(30, VISIBILITY_SECONDS // 3)
        time.sleep(10)
    raise TimeoutError(f"Ray job {submission_id} exceeded {JOB_TIMEOUT_SECONDS}s")


def _process_message(sqs, ray_client, message: dict) -> None:
    receive_count = message.get("Attributes", {}).get("ApproximateReceiveCount", "1")
    for index, record in enumerate(_records(message["Body"])):
        if record.get("eventSource") != "aws:s3":
            continue
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])
        extension = key.rsplit(".", 1)[-1].lower() if "." in key else ""
        if extension not in SUPPORTED_EXTENSIONS:
            logger.info("Skipping unsupported object s3://%s/%s", bucket, key)
            continue

        identity = f"{message['MessageId']}:{receive_count}:{index}:{bucket}:{key}"
        submission_id = "ingest-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        source = f"s3://{bucket}/{quote(key, safe='/')}"
        logger.info("Submitting %s as Ray job %s", source, submission_id)
        ray_client.submit_job(
            submission_id=submission_id,
            entrypoint=(
                "python -m pipelines.ingestion.main "
                f"{shlex.quote(source)} --no-init-ray"
            ),
            runtime_env={
                "env_vars": {
                    name: os.environ[name] for name in FORWARDED_ENV if name in os.environ
                }
            },
        )
        _wait_for_job(ray_client, sqs, message["ReceiptHandle"], submission_id)
        logger.info("Ingestion succeeded for %s", source)


def main() -> None:
    sqs = boto3.client("sqs", region_name=os.getenv("AWS_REGION", "us-east-1"))
    ray_client = JobSubmissionClient(RAY_JOBS_ADDRESS)
    logger.info("Consuming %s and submitting jobs to %s", QUEUE_URL, RAY_JOBS_ADDRESS)
    while True:
        response = sqs.receive_message(
            QueueUrl=QUEUE_URL,
            MaxNumberOfMessages=1,
            WaitTimeSeconds=20,
            VisibilityTimeout=VISIBILITY_SECONDS,
            AttributeNames=["ApproximateReceiveCount"],
        )
        for message in response.get("Messages", []):
            try:
                _process_message(sqs, ray_client, message)
            except Exception:
                logger.exception("Ingestion message failed; SQS will retry it")
                continue
            sqs.delete_message(
                QueueUrl=QUEUE_URL, ReceiptHandle=message["ReceiptHandle"]
            )


if __name__ == "__main__":
    main()
