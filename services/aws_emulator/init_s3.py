import os
import time
from collections.abc import Callable
from typing import Any

import boto3
from botocore.exceptions import ClientError, EndpointConnectionError


ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://localhost:4566")
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")
BUCKET = os.getenv("EVAL_EVIDENCE_BUCKET", "eval-traces")


def _client():
    return boto3.client(
        "s3",
        endpoint_url=ENDPOINT_URL,
        region_name=REGION,
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID", "test"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY", "test"),
    )


def initialize(
    client: Any | None = None,
    sleep: Callable[[float], None] | None = None,
) -> None:
    if client is None:
        client = _client()
    if sleep is None:
        sleep = time.sleep
    for attempt in range(60):
        try:
            client.list_buckets()
            break
        except EndpointConnectionError:
            if attempt == 59:
                raise
            sleep(0.25)

    try:
        client.create_bucket(Bucket=BUCKET)
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") not in {
            "BucketAlreadyExists",
            "BucketAlreadyOwnedByYou",
        }:
            raise

    client.put_bucket_versioning(
        Bucket=BUCKET,
        VersioningConfiguration={"Status": "Enabled"},
    )


if __name__ == "__main__":
    initialize()
