# pipelines/README.md

# Data Pipelines

This directory contains the asynchronous data ingestion and processing logic. This is the "factory" that populates our databases with knowledge.

## Contents

-   **`ingestion/`**: The main Ray Data pipeline. It is responsible for:
    1.  Reading raw files (PDF, DOCX, HTML) from S3.
    2.  Parsing and chunking the text.
    3.  Generating vector embeddings.
    4.  Extracting a knowledge graph (entities and relationships).
    5.  Indexing the data into Qdrant (vectors) and Neo4j (graph).
-   **`jobs/`**: Scripts and definitions for triggering these pipelines. The `s3_event_handler.py` is designed to be run as an AWS Lambda function that listens for S3 upload events and submits a job to the Ray cluster.

## Production execution

The deployed path is queue-backed:

1. S3 sends every object-created event from the documents bucket to SQS.
2. `deployment/ingestion-worker` long-polls that queue.
3. Supported files are submitted to `ingestion-ray` through the Ray Jobs API.
4. CPU Ray workers parse and chunk the document, call the existing embedding and
   LLM RayServices, and write to Qdrant and Neo4j.
5. The SQS message is deleted only after the Ray job succeeds. Failed messages
   are retried three times and then moved to the dead-letter queue.

Supported extensions are PDF, HTML, DOCX, TXT, PPT, and PPTX. Existing objects
do not generate events when the notification is first enabled; copy or re-upload
them, or submit a manual Ray job.

Build and publish the ingestion image before bootstrap:

```bash
bash scripts/build_push_image.sh ingestion
bash scripts/build_push_image.sh ingestion <git-sha>
bash scripts/sync_s3_to_ecr.sh ingestion <git-sha>
```

Deploy or update only the ingestion components in an existing cluster:

```bash
bash scripts/deploy_ingestion.sh
```

The script derives the latest ingestion image tag and Terraform queue/IRSA
outputs by default. Set `INGESTION_IMAGE_TAG`, `INGESTION_QUEUE_URL`, or
`INGESTION_ROLE_ARN` to override them explicitly.

Operational checks:

```bash
kubectl get raycluster ingestion-ray
kubectl logs -f deploy/ingestion-worker
kubectl port-forward svc/ingestion-ray-head-svc 8265:8265
ray job list --address http://localhost:8265
```

Manual submission remains available:

```bash
ray job submit --address http://localhost:8265 --working-dir . -- \
  python -m pipelines.ingestion.main s3://<documents-bucket>/<key> --no-init-ray
```
