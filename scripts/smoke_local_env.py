#!/usr/bin/env python3
import json
import os
import time

import boto3
import httpx
from jose import jwt


API_BASE_URL = os.getenv("LOCAL_API_BASE_URL", "http://127.0.0.1:18000")
FAKE_MODELS_URL = os.getenv("LOCAL_FAKE_MODELS_URL", "http://127.0.0.1:8001")
COLLECTOR_HEALTH_URL = os.getenv("LOCAL_COLLECTOR_HEALTH_URL", "http://127.0.0.1:13134")
S3_ENDPOINT_URL = os.getenv("S3_ENDPOINT_URL", "http://127.0.0.1:14566")
EVIDENCE_BUCKET = os.getenv("EVAL_EVIDENCE_BUCKET", "eval-traces")
JWT_SECRET = os.getenv("JWT_SECRET_KEY", "local-dev-only-change-me")


def main() -> None:
    fake_health = httpx.get(f"{FAKE_MODELS_URL}/health", timeout=5)
    fake_health.raise_for_status()

    collector_health = httpx.get(COLLECTOR_HEALTH_URL, timeout=5)
    collector_health.raise_for_status()

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        region_name="us-east-1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
    )
    versioning = s3.get_bucket_versioning(Bucket=EVIDENCE_BUCKET)
    if versioning.get("Status") != "Enabled":
        raise RuntimeError(f"evidence bucket is not versioned: {versioning!r}")

    api_health = httpx.get(f"{API_BASE_URL}/health/liveness", timeout=5)
    api_health.raise_for_status()

    token = jwt.encode(
        {
            "sub": "local-smoke-user",
            "role": "user",
            "exp": int(time.time()) + 300,
        },
        JWT_SECRET,
        algorithm="HS256",
    )
    response = httpx.post(
        f"{API_BASE_URL}/api/v1/chat/stream",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "message": f"hello local smoke {time.time_ns()}",
            "session_id": "local-environment-smoke",
        },
        timeout=20,
    )
    response.raise_for_status()
    events = [json.loads(line) for line in response.text.splitlines() if line]
    if any(event.get("type") == "error" for event in events):
        raise RuntimeError(f"stream returned an error event: {events!r}")
    if not any(
        event.get("type") == "answer"
        and event.get("content") == "This is a deterministic local response."
        for event in events
    ):
        raise RuntimeError(f"stream returned no deterministic answer: {events!r}")

    print(
        json.dumps(
            {
                "api": "ok",
                "collector": "ok",
                "evidence_bucket": "versioned",
                "fake_models": "ok",
                "stream": "ok",
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
