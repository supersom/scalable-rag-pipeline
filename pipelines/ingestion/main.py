# pipelines/ingestion/main.py
import argparse
import hashlib
import logging
import os
import signal
import sys

import ray

from pipelines.ingestion.chunking.splitter import split_text
from pipelines.ingestion.embedding.compute import BatchEmbedder
from pipelines.ingestion.graph.extractor import GraphExtractor
from pipelines.ingestion.indexing.qdrant import QdrantIndexer
from pipelines.ingestion.indexing.neo4j import Neo4jIndexer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# Some malformed PDFs make pdfminer's content-stream interpreter loop for
# minutes to hours (seen on real files with corrupt pattern-colorspace
# operators). A per-file wall-clock cap keeps one bad file from wedging an
# entire block. SIGALRM only interrupts the main thread, which is fine here —
# Ray Data actors run map_batches on the worker's main thread.
PARSE_TIMEOUT_SECONDS = int(os.getenv("INGESTION_PARSE_TIMEOUT_SECONDS", "90"))

# Points written before content_hash tracking existed have no hash to compare
# against. When enabled, a source_path match alone (no hash available) is
# trusted as "unchanged" rather than being re-parsed/re-embedded. Off by
# default since it can't actually detect a change in that case — it's a
# one-time bridge for backfilling this feature onto an already-ingested
# corpus, not a substitute for real hash comparison.
TRUST_PATH_WITHOUT_HASH = os.getenv(
    "INGESTION_TRUST_PATH_MATCH_WITHOUT_HASH", "false"
).lower() in ("1", "true", "yes")

_NOT_INGESTED = object()  # sentinel: distinguishes "never seen" from "seen, hash=None"


class _ParseTimeout(BaseException):
    """Deliberately not Exception — unstructured/pdfminer have their own
    broad `except Exception:` around individual extraction steps (seen live:
    it swallowed this as a normal parse failure and kept going with the
    alarm already consumed, leaving the rest of the file unprotected).
    Subclassing BaseException lets this pass through those handlers
    untouched, the same reason KeyboardInterrupt/SystemExit do."""


def _raise_parse_timeout(signum, frame):
    raise _ParseTimeout()


def process_batch(batch, known_hashes: dict, trust_path_without_hash: bool):
    """
    Ray Data transformation function.
    Receives a batch of file contents (binary bytes + path).
    Returns chunked text rows ready for embedding/graph stages.
    """
    results = []

    for i, content in enumerate(batch["bytes"]):
        filename = batch["path"][i]
        # Keep only the basename for cleaner metadata
        short_name = filename.split("/")[-1]

        content_hash = hashlib.sha256(content).hexdigest()
        known_hash = known_hashes.get(filename, _NOT_INGESTED)
        if known_hash == content_hash:
            continue  # unchanged since last run — skip parse/embed/write entirely
        if known_hash is None and trust_path_without_hash:
            # Legacy point, ingested before content_hash existed. No hash to
            # compare, but the toggle says to trust the path match anyway.
            continue
        if known_hash is not _NOT_INGESTED:
            # Previously ingested under this path with different (or
            # untrusted-missing) content — clear the stale version first so
            # its old chunks don't linger alongside the new ones.
            QdrantIndexer().delete_by_source_path(filename)

        ext = short_name.rsplit(".", 1)[-1].lower()
        previous_handler = signal.signal(signal.SIGALRM, _raise_parse_timeout)
        signal.alarm(PARSE_TIMEOUT_SECONDS)
        try:
            if ext == "pdf":
                from pipelines.ingestion.loaders.pdf import parse_pdf_bytes

                raw_text, metadata = parse_pdf_bytes(content, short_name)
            elif ext in ("html", "htm"):
                from pipelines.ingestion.loaders.html import parse_html_bytes

                raw_text, metadata = parse_html_bytes(content, short_name)
            elif ext == "docx":
                from pipelines.ingestion.loaders.docx import parse_docx_bytes

                raw_text, metadata = parse_docx_bytes(content, short_name)
            elif ext == "txt":
                raw_text = content.decode("utf-8", errors="replace")
                metadata = {"filename": short_name, "type": "txt"}
            elif ext in ("pptx", "ppt"):
                from pipelines.ingestion.loaders.pptx import parse_pptx_bytes

                raw_text, metadata = parse_pptx_bytes(content, short_name)
            else:
                logger.warning(f"Skipping unsupported file type: {short_name}")
                continue
        except _ParseTimeout:
            logger.error(
                f"Timed out parsing {short_name} after {PARSE_TIMEOUT_SECONDS}s, skipping"
            )
            continue
        except Exception as e:
            logger.error(f"Failed to parse {short_name}: {e}")
            continue
        finally:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, previous_handler)

        metadata["source_path"] = filename
        metadata["content_hash"] = content_hash
        chunks = split_text(raw_text, chunk_size=512, overlap=50)

        for chunk in chunks:
            chunk["metadata"].update(metadata)
            results.append(chunk)

    return {
        "text":     [r["text"]     for r in results],
        "metadata": [r["metadata"] for r in results],
    }


def main(source: str, extract_graph: bool = True, init_ray: bool = True):
    """
    Main orchestration flow.

    Args:
        source:        Local directory path or s3://bucket/prefix
        extract_graph: Whether to run graph extraction into Neo4j
    """
    # ── Ray init ──────────────────────────────────────────────────────────────
    # address="auto"  → connects to an existing cluster (prod / K8s)
    # no address      → starts an in-process local cluster (local dev)
    # Forward pipeline-relevant env vars to Ray worker processes.
    # Workers are subprocesses and don't inherit the parent shell's environment.
    worker_env = {k: os.environ[k] for k in (
        "QDRANT_HOST", "QDRANT_PORT", "QDRANT_COLLECTION",
        "NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD",
        "RAY_EMBED_ENDPOINT", "EMBED_MODEL_NAME",
        "RAY_LLM_ENDPOINT", "LLM_MODEL_NAME",
    ) if k in os.environ}

    if ray.is_initialized():
        logger.info("Using already-initialised Ray cluster.")
    else:
        if init_ray:
            ray.init(address="local", runtime_env={"env_vars": worker_env})
            logger.info("Started a new local Ray cluster.")
        else:
            # Running as a Ray job: env vars are already set by the job submission's
            # runtime_env. Passing them again here causes a conflict that Ray rejects.
            address = os.environ.get("RAY_ADDRESS", "auto")
            ray.init(address=address)
            logger.info("Connected to Ray cluster at %s.", address)

    logger.info(f"Reading files from: {source}")

    known_hashes = QdrantIndexer().iter_known_hashes()
    logger.info(
        f"Found {len(known_hashes)} already-ingested source paths "
        f"(trust_path_without_hash={TRUST_PATH_WITHOUT_HASH})"
    )

    # ── 1. Read files ─────────────────────────────────────────────────────────
    # Explicit parallelism avoids Ray autodetecting too few blocks when few CPUs
    # are free at submit time: each block becomes one PyArrow binary array, and
    # a too-large corpus packed into one block trips Arrow's ~2GB array cap.
    ds = ray.data.read_binary_files(
        source,
        include_paths=True,
        file_extensions=["pdf", "PDF"],
        parallelism=200,
    )

    # ── 2. Parse & chunk (CPU) ────────────────────────────────────────────────
    chunked_ds = ds.map_batches(
        process_batch,
        batch_size=10,
        num_cpus=1,
        fn_kwargs={
            "known_hashes": known_hashes,
            "trust_path_without_hash": TRUST_PATH_WITHOUT_HASH,
        },
    )

    # ── 3a. Embed (calls Ollama locally, Ray Serve in prod) ───────────────────
    vector_ds = chunked_ds.map_batches(
        BatchEmbedder,
        concurrency=2,
        batch_size=20,
    )

    # ── 4a. Index vectors into Qdrant ─────────────────────────────────────────
    logger.info("Writing vectors to Qdrant...")
    vector_ds.map_batches(
        lambda batch: (QdrantIndexer().write(
            [{"text": t, "metadata": m, "vector": v}
             for t, m, v in zip(batch["text"], batch["metadata"], batch["vector"])]
        ) or batch),
        batch_size=100,
    ).count()   # .count() triggers execution

    if extract_graph:
        # ── 3b. Graph extraction (calls Ollama locally, Ray Serve in prod) ────
        graph_ds = chunked_ds.map_batches(
            GraphExtractor,
            concurrency=2,
            batch_size=5,
        )

        # ── 4b. Index graph into Neo4j ────────────────────────────────────────
        logger.info("Writing graph to Neo4j...")
        graph_ds.map_batches(
            lambda batch: (Neo4jIndexer().write(
                [{"graph_nodes": n, "graph_edges": e}
                 for n, e in zip(batch["graph_nodes"], batch["graph_edges"])]
            ) or batch),
            batch_size=50,
        ).count()

    logger.info("Ingestion complete.")
    if init_ray:
        ray.shutdown()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="RAG ingestion pipeline (Ray Data)")
    parser.add_argument(
        "source",
        help="Local directory path (e.g. data/docs/) or S3 URI (e.g. s3://my-bucket/prefix/)",
    )
    parser.add_argument(
        "--no-graph",
        action="store_true",
        help="Skip Neo4j graph extraction",
    )
    parser.add_argument(
        "--no-init-ray",
        action="store_true",
        help="Connect to an existing Ray cluster (RAY_ADDRESS env var) instead of starting one",
    )
    args = parser.parse_args()

    main(args.source, extract_graph=not args.no_graph, init_ray=not args.no_init_ray)
