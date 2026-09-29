"""Drain consumer entrypoint: poll the evidence queue, archive each envelope, delete on success.

A malformed message is left on the queue rather than deleted, so SQS's own redrive policy moves
it to the DLQ after its configured receive count — this process never makes that call itself.

Run: ``python -m services.evidence_drain_consumer.main``
"""
from __future__ import annotations

import asyncio
import logging
import signal
import sys

logger = logging.getLogger(__name__)

EXIT_RUNTIME_FAILURE = 1
EXIT_CONFIGURATION = 2


async def poll_once(consumer, sqs_client, *, queue_url: str) -> str | None:
    """One receive-and-process step; returns the outcome, or None when the queue was empty."""
    response = sqs_client.receive_message(QueueUrl=queue_url, MaxNumberOfMessages=1, WaitTimeSeconds=1)
    messages = response.get("Messages", [])
    if not messages:
        return None
    message = messages[0]
    outcome = await consumer.process_message(message["Body"].encode()
                                              if isinstance(message["Body"], str) else message["Body"])
    if outcome in ("archived", "conflict"):
        sqs_client.delete_message(QueueUrl=queue_url, ReceiptHandle=message["ReceiptHandle"])
    return outcome


async def serve(consumer, sqs_client, *, queue_url: str, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        outcome = await poll_once(consumer, sqs_client, queue_url=queue_url)
        if outcome is None:
            await asyncio.sleep(1)


def _load_config():
    import os
    queue_url = os.environ.get("EVIDENCE_QUEUE_URL")
    bucket = os.environ.get("EVIDENCE_ARCHIVE_BUCKET")
    kms_key_id = os.environ.get("EVIDENCE_ARCHIVE_KMS_KEY_ID")
    region = os.environ.get("AWS_REGION", "us-east-1")
    if not queue_url or not bucket or not kms_key_id:
        raise ValueError("EVIDENCE_QUEUE_URL, EVIDENCE_ARCHIVE_BUCKET and EVIDENCE_ARCHIVE_KMS_KEY_ID are required")
    return queue_url, bucket, kms_key_id, region


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s %(message)s")
    try:
        queue_url, bucket, kms_key_id, region = _load_config()
    except ValueError:
        logger.error("evidence drain consumer configuration invalid")
        return EXIT_CONFIGURATION
    try:
        import boto3
        from urllib.parse import urlsplit
        from services.api.app.evaluation.delivery_metrics import start_metrics_server
        from services.evidence_drain_consumer.consumer import DrainConsumer
        parts = urlsplit(queue_url)
        sqs_client = boto3.client("sqs", region_name=region, endpoint_url=f"{parts.scheme}://{parts.netloc}")
        s3_client = boto3.client("s3", region_name=region)
        consumer = DrainConsumer(s3_client, bucket=bucket, kms_key_id=kms_key_id)
        # Own process/pod from the delivery worker, so port 9090 doesn't collide. Serves the
        # default REGISTRY that DrainConsumer registers evidence_archive_conflicts_total onto.
        start_metrics_server(9090)
        stop_event = asyncio.Event()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop_event.set)
        logger.info("evidence drain consumer started")
        loop.run_until_complete(serve(consumer, sqs_client, queue_url=queue_url, stop_event=stop_event))
        logger.info("evidence drain consumer stopped")
        return 0
    except Exception:
        logger.error("evidence drain consumer failed")
        return EXIT_RUNTIME_FAILURE


if __name__ == "__main__":
    sys.exit(main())
