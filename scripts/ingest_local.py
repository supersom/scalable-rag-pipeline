"""
Local ingestion script — no Ray, no S3 required.

Usage:
    python scripts/ingest_local.py ./data/docs/
    python scripts/ingest_local.py ./data/docs/ --no-graph   # skip Neo4j graph extraction
    python scripts/ingest_local.py ./data/docs/ --glob "*.txt"

Supported file types: .pdf, .txt, .md
"""
import argparse
import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from qdrant_client import QdrantClient
from qdrant_client.http import models

# ── Project root on the path so we can import shared modules ──────────────────
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipelines.ingestion.chunking.splitter import split_text


# ── Config from env (matches .env defaults) ───────────────────────────────────
QDRANT_HOST       = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT       = int(os.getenv("QDRANT_PORT", 6333))
QDRANT_COLLECTION = os.getenv("QDRANT_COLLECTION", "rag_collection")

NEO4J_URI      = os.getenv("NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER     = os.getenv("NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.getenv("NEO4J_PASSWORD", "password")

EMBED_ENDPOINT = os.getenv("RAY_EMBED_ENDPOINT", "http://localhost:11434/api/embeddings")
EMBED_MODEL    = os.getenv("EMBED_MODEL_NAME", "nomic-embed-text")

LLM_ENDPOINT = os.getenv("RAY_LLM_ENDPOINT", "http://localhost:11434/v1/chat/completions")
LLM_MODEL    = os.getenv("LLM_MODEL_NAME", "llama3.2:3b")

CHUNK_SIZE    = 512
CHUNK_OVERLAP = 50


# ── File parsing ──────────────────────────────────────────────────────────────

def parse_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _parse_pdf(path)
    elif suffix in (".txt", ".md"):
        return path.read_text(errors="replace")
    else:
        raise ValueError(f"Unsupported file type: {suffix}")


def _parse_pdf(path: Path) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(path))
    pages = []
    for page in reader.pages:
        text = page.extract_text()
        if text:
            pages.append(text)
    return "\n\n".join(pages)


# ── Embedding ─────────────────────────────────────────────────────────────────

async def embed_texts(texts: list[str]) -> list[list[float]]:
    async with httpx.AsyncClient(timeout=60.0) as client:
        embeddings = []
        for text in texts:
            r = await client.post(
                EMBED_ENDPOINT,
                json={"model": EMBED_MODEL, "prompt": text}
            )
            r.raise_for_status()
            embeddings.append(r.json()["embedding"])
        return embeddings


# ── Graph extraction ──────────────────────────────────────────────────────────

GRAPH_PROMPT = """Extract entities and relationships from the text below.
Return ONLY valid JSON in this exact format, no other text:
{
  "nodes": [{"id": "Entity Name", "type": "Person|Organization|Concept|Location"}],
  "edges": [{"source": "Entity A", "target": "Entity B", "type": "RELATIONSHIP_TYPE"}]
}

Text:
"""

_llm_semaphore = asyncio.Semaphore(3)

async def extract_graph(text: str) -> dict[str, Any]:
    async with _llm_semaphore:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(
                LLM_ENDPOINT,
                json={
                    "model": LLM_MODEL,
                    "messages": [{"role": "user", "content": GRAPH_PROMPT + text}],
                    "temperature": 0.0,
                    "max_tokens": 512,
                }
            )
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            # Strip markdown code fences if the model wraps the JSON
            content = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            return json.loads(content)


# ── Qdrant indexing ───────────────────────────────────────────────────────────

def index_vectors(chunks: list[dict], embeddings: list[list[float]]):
    client = QdrantClient(host=QDRANT_HOST, port=QDRANT_PORT)
    points = [
        models.PointStruct(
            id=str(uuid.uuid4()),
            vector=vec,
            payload={
                "text": chunk["text"],
                "metadata": chunk["metadata"],
            }
        )
        for chunk, vec in zip(chunks, embeddings)
    ]
    client.upsert(collection_name=QDRANT_COLLECTION, points=points)
    return len(points)


# ── Neo4j indexing ────────────────────────────────────────────────────────────

def index_graph(all_nodes: list, all_edges: list):
    from neo4j import GraphDatabase
    all_nodes = [n for n in all_nodes if n.get("id")]
    all_edges = [e for e in all_edges if e.get("source") and e.get("target") and e.get("type")]
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        if all_nodes:
            session.run(
                "UNWIND $nodes AS n MERGE (e:Entity {name: n.id}) SET e.type = n.type",
                nodes=all_nodes
            )
        if all_edges:
            session.run(
                """UNWIND $edges AS e
                   MATCH (s:Entity {name: e.source})
                   MATCH (t:Entity {name: e.target})
                   MERGE (s)-[r:RELATED {type: e.type}]->(t)""",
                edges=all_edges
            )
    driver.close()


def ensure_neo4j_fulltext_index():
    """Creates the fulltext index the retriever queries if it doesn't exist."""
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    with driver.session() as session:
        session.run(
            "CREATE FULLTEXT INDEX entity_index IF NOT EXISTS FOR (n:Entity) ON EACH [n.name]"
        )
    driver.close()


# ── Main pipeline ─────────────────────────────────────────────────────────────

GRAPH_CHUNK_LIMIT = 10


async def ingest_file(path: Path, extract_graph_flag: bool, graph_only: bool = False):
    print(f"\n── {path.name}")
    steps = 3 if graph_only else 4

    # 1. Parse
    print(f"  [1/{steps}] Parsing...")
    try:
        text = parse_file(path)
    except (RecursionError, Exception) as e:
        print(f"  Skipped (parse error: {e})")
        return
    if not text.strip():
        print("  Skipped (empty)")
        return

    # 2. Chunk
    print(f"  [2/{steps}] Chunking...")
    chunks = split_text(text, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)
    if graph_only:
        chunks = chunks[:GRAPH_CHUNK_LIMIT]
    for chunk in chunks:
        chunk["metadata"]["filename"] = path.name
    print(f"        {len(chunks)} chunks")

    if not graph_only:
        # 3. Embed + index into Qdrant
        print(f"  [3/{steps}] Embedding + indexing into Qdrant...")
        texts = [c["text"] for c in chunks]
        embeddings = await embed_texts(texts)
        n = index_vectors(chunks, embeddings)
        print(f"        {n} vectors upserted")

    # 3 or 4. Graph extraction + index into Neo4j
    if extract_graph_flag:
        step = 3 if graph_only else 4
        print(f"  [{step}/{steps}] Graph extraction + indexing into Neo4j...")
        all_nodes, all_edges = [], []
        results = await asyncio.gather(
            *[extract_graph(chunk["text"]) for chunk in chunks],
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                print(f"        Graph extraction failed for a chunk: {r}")
            else:
                all_nodes.extend(r.get("nodes", []))
                all_edges.extend(r.get("edges", []))
        try:
            index_graph(all_nodes, all_edges)
            print(f"        {len(all_nodes)} nodes, {len(all_edges)} edges")
        except Exception as e:
            print(f"        Neo4j write failed: {e}")
    else:
        print(f"  [4/{steps}] Graph extraction skipped (--no-graph)")


async def main():
    parser = argparse.ArgumentParser(description="Ingest local documents into RAG pipeline")
    parser.add_argument("directory", help="Directory containing documents to ingest")
    parser.add_argument("--glob", default="*.*", help="File glob pattern (default: *.*)")
    parser.add_argument("--no-graph", action="store_true", help="Skip Neo4j graph extraction")
    parser.add_argument("--graph-only", action="store_true", help="Skip embedding/Qdrant; extract graph into Neo4j only (first 10 chunks per file)")
    args = parser.parse_args()

    if args.graph_only and args.no_graph:
        print("Error: --graph-only and --no-graph are mutually exclusive")
        sys.exit(1)

    doc_dir = Path(args.directory)
    if not doc_dir.exists():
        print(f"Error: directory {doc_dir} does not exist")
        sys.exit(1)

    files = [
        f for f in sorted(doc_dir.glob(args.glob))
        if f.is_file() and f.suffix.lower() in (".pdf", ".txt", ".md")
    ]
    if not files:
        print(f"No supported files (.pdf, .txt, .md) found in {doc_dir}")
        sys.exit(1)

    print(f"Found {len(files)} file(s) to ingest")
    if not args.graph_only:
        print(f"Embed endpoint : {EMBED_ENDPOINT}  model={EMBED_MODEL}")
        print(f"Qdrant         : {QDRANT_HOST}:{QDRANT_PORT}  collection={QDRANT_COLLECTION}")
    if not args.no_graph:
        print(f"Neo4j          : {NEO4J_URI}")
        ensure_neo4j_fulltext_index()

    for f in files:
        await ingest_file(f, extract_graph_flag=not args.no_graph, graph_only=args.graph_only)

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
