"""Evaluation delivery queue and dead-letter queue for the local emulator.

Moto's server mode serves SQS on the same port as S3, so this reuses the emulator endpoint.
Queue retention stays at emulator defaults: this is transport testing, not a production policy.
"""
import argparse
import json
import os
import sys
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError

from services.aws_emulator.init_s3 import ENDPOINT_URL, REGION


QUEUE_NAME = os.getenv("EVAL_QUEUE_NAME", "eval-runs")
DLQ_NAME = os.getenv("EVAL_DLQ_NAME", "eval-runs-dlq")
MAX_RECEIVE_COUNT = 5


class QueueNotReady(Exception):
    pass


def _client():
    return boto3.client(
        "sqs",
        endpoint_url=ENDPOINT_URL,
        region_name=REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def _arn(client: Any, queue_url: str) -> str:
    return client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]


def _redrive(client: Any, queue_url: str) -> dict | None:
    attributes = client.get_queue_attributes(QueueUrl=queue_url, AttributeNames=["RedrivePolicy"])
    policy = attributes.get("Attributes", {}).get("RedrivePolicy")
    return json.loads(policy) if policy else None


def _redrive_matches(policy: dict | None, dlq_arn: str) -> bool:
    # SQS may return maxReceiveCount as a number or a string.
    return (policy is not None and policy.get("deadLetterTargetArn") == dlq_arn
            and str(policy.get("maxReceiveCount")) == str(MAX_RECEIVE_COUNT))


def ensure_queues(client: Any, queue_name: str, dlq_name: str) -> dict[str, str]:
    # create_queue without attributes is idempotent by name; the redrive policy is compared and set
    # separately so an existing queue converges instead of failing with QueueAlreadyExists.
    dlq_url = client.create_queue(QueueName=dlq_name)["QueueUrl"]
    dlq_arn = _arn(client, dlq_url)
    queue_url = client.create_queue(QueueName=queue_name)["QueueUrl"]
    if not _redrive_matches(_redrive(client, queue_url), dlq_arn):
        client.set_queue_attributes(QueueUrl=queue_url, Attributes={"RedrivePolicy": json.dumps(
            {"deadLetterTargetArn": dlq_arn, "maxReceiveCount": str(MAX_RECEIVE_COUNT)})})
    return {"queue_url": queue_url, "queue_arn": _arn(client, queue_url), "dlq_url": dlq_url, "dlq_arn": dlq_arn}


def check_queues(client: Any, queue_name: str, dlq_name: str) -> None:
    try:
        dlq_url = client.get_queue_url(QueueName=dlq_name)["QueueUrl"]
        queue_url = client.get_queue_url(QueueName=queue_name)["QueueUrl"]
        ready = _redrive_matches(_redrive(client, queue_url), _arn(client, dlq_url))
    except ClientError as error:
        raise QueueNotReady from error
    if not ready:
        raise QueueNotReady


def initialize(
    client: Any | None = None,
    sleep: Callable[[float], None] | None = None,
) -> dict[str, str]:
    if client is None:
        client = _client()
    if sleep is None:
        sleep = time.sleep
    for attempt in range(60):
        try:
            client.list_queues()
            break
        except EndpointConnectionError:
            if attempt == 59:
                raise
            sleep(0.25)
    return ensure_queues(client, QUEUE_NAME, DLQ_NAME)


def main(argv: list[str] | None = None, client: Any | None = None) -> int:
    parser = argparse.ArgumentParser(description="Initialize or check the local evaluation queues.")
    parser.add_argument("--check", action="store_true", help="verify only; create nothing")
    args = parser.parse_args(argv)
    if client is None:
        client = _client()
    names = f"queue={QUEUE_NAME} dlq={DLQ_NAME}"
    try:
        if args.check:
            check_queues(client, QUEUE_NAME, DLQ_NAME)
        else:
            initialize(client=client)
    except (QueueNotReady, ClientError, EndpointConnectionError):
        # Status lines carry only queue names: never URLs, ARNs, account IDs or credentials.
        print(f"evaluation queues not ready: {names}", file=sys.stderr)
        return 1
    print(f"evaluation queues ready: {names} redrive=configured")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
