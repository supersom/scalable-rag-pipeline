# pipelines/ingestion/main.py
import argparse
import logging
import os
import sys

import ray

from pipelines.ingestion.loaders.pdf import parse_pdf_bytes
from pipelines.ingestion.loaders.html import parse_html_bytes
from pipelines.ingestion.loaders.docx import parse_docx_bytes
from pipelines.ingestion.loaders.pptx import parse_pptx_bytes
from pipelines.ingestion.chunking.splitter import split_text
from pipelines.ingestion.embedding.compute import BatchEmbedder
from pipelines.ingestion.graph.extractor import GraphExtractor
from pipelines.ingestion.indexing.qdrant import QdrantIndexer
from pipelines.ingestion.indexing.neo4j import Neo4jIndexer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)


def process_batch(batch):
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

        ext = short_name.rsplit(".", 1)[-1].lower()
        try:
            if ext == "pdf":
                raw_text, metadata = parse_pdf_bytes(content, short_name)
            elif ext in ("html", "htm"):
                raw_text, metadata = parse_html_bytes(content, short_name)
            elif ext == "docx":
                raw_text, metadata = parse_docx_bytes(content, short_name)
            elif ext == "txt":
                raw_text = content.decode("utf-8", errors="replace")
                metadata = {"filename": short_name, "type": "txt"}
            elif ext in ("pptx", "ppt"):
                raw_text, metadata = parse_pptx_bytes(content, short_name)
            else:
                logger.warning(f"Skipping unsupported file type: {short_name}")
                continue
        except Exception as e:
            logger.error(f"Failed to parse {short_name}: {e}")
            continue

        metadata["source_path"] = filename
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
        address = os.environ.get("RAY_ADDRESS", "auto") if not init_ray else "auto"
        try:
            ray.init(address=address, runtime_env={"env_vars": worker_env})
            logger.info(f"Connected to Ray cluster at {address}.")
        except Exception:
            ray.init(runtime_env={"env_vars": worker_env})
            logger.info("Started local Ray cluster.")

    logger.info(f"Reading files from: {source}")

    # ── 1. Read files ─────────────────────────────────────────────────────────
    ds = ray.data.read_binary_files(source, include_paths=True)

    # ── 2. Parse & chunk (CPU) ────────────────────────────────────────────────
    chunked_ds = ds.map_batches(
        process_batch,
        batch_size=10,
        num_cpus=1,
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
