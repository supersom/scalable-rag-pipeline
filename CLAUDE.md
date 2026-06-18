# scalable-rag-pipeline

RAG platform originally designed for K8s/Ray Serve production deployment (forked from FareedKhan-dev/scalable-rag-pipeline). Ongoing work is adapting it to run locally with Ollama for development and evaluation, while keeping the code deployable to K8s unchanged.

## Architecture

```
User → FastAPI (services/api) → LangGraph agent → Qdrant (vectors) + Neo4j (graph)
                                                 → Ollama / Ray Serve (LLM + embeddings)
Ingestion: local files or S3 → Ray Data pipeline → Qdrant + Neo4j
```

**Key principle:** all endpoints are env-var-driven so the same code hits Ollama locally and Ray Serve in prod. Never hardcode K8s DNS names.

## Services & ports (local docker-compose)

| Service    | Port  | Purpose                        |
|------------|-------|-------------------------------|
| FastAPI    | 8000  | REST API                       |
| Qdrant     | 6333  | Vector DB (REST) — gRPC 6334 not exposed |
| Neo4j      | 7474  | Graph DB browser               |
| Neo4j Bolt | 7687  | Graph DB driver                |
| Postgres   | 5432  | Chat history                   |
| Redis      | 6379  | Semantic cache                 |
| Ollama     | 11434 | Local LLM + embeddings         |

Start infrastructure: `docker compose up -d`

## Key env vars (.env)

```
LLM_MODEL_NAME=llama3.2:3b
EMBED_MODEL_NAME=nomic-embed-text
RAY_LLM_ENDPOINT=http://localhost:11434/v1/chat/completions
RAY_EMBED_ENDPOINT=http://localhost:11434/api/embeddings
QDRANT_HOST=localhost
QDRANT_PORT=6333
NEO4J_URI=bolt://localhost:7687
```

## Ingestion

Production uploads use S3 → SQS → `ingestion-worker` → CPU `ingestion-ray` jobs.
The Ray Data jobs call the GPU embedding/LLM RayServices and index Qdrant/Neo4j.
Only objects uploaded after Terraform applies the bucket notification are queued.

Two ways to ingest locally:

```bash
# Lightweight (no Ray) — PDF, TXT, MD
python scripts/ingest_local.py ./data/docs/
python scripts/ingest_local.py ./data/docs/ --no-graph       # skip Neo4j
python scripts/ingest_local.py ./data/docs/ --graph-only     # Neo4j only (first 10 chunks)

# Full Ray pipeline — all formats including PPTX, DOCX, HTML
python -m pipelines.ingestion.main ./data/docs/
python -m pipelines.ingestion.main ./data/docs/ --no-graph
```

Batch eval ingestion (processes `eval/datasets/noisy_data/` in batches of 20):
```bash
python scripts/batch_ingest_loop.py
python scripts/batch_ingest_loop.py --graph-only   # graph extraction on already-ingested files
```

Eval dataset directories:
- `eval/datasets/noisy_data/` — pending ingestion
- `eval/datasets/ingested/` — vector-indexed; awaiting graph extraction
- `eval/datasets/digested/` — fully processed (vectors + graph)
- `eval/datasets/failed/` — failed ingestion

## API endpoints

| Method | Path | Auth |
|--------|------|------|
| POST | `/api/v1/chat/stream` | Bearer JWT |
| POST | `/api/v1/upload/generate-presigned-url` | Bearer JWT |
| GET | `/health/liveness` | None |
| GET | `/health/readiness` | None |

JWT is validated in `services/api/app/auth/jwt.py`. Token is passed via `AUTH_TOKEN` env var in scripts.

## Check ingestion status

- **Qdrant:** `http://localhost:6333/collections/rag_collection`
- **Neo4j:** `http://localhost:7474` (browser), run `MATCH (n) RETURN count(n)`

## Source layout

```
pipelines/ingestion/     # Ray Data ingestion pipeline
  loaders/               # PDF, DOCX, HTML, TXT, PPTX parsers
  chunking/              # Text splitter
  embedding/compute.py   # BatchEmbedder (Ollama /api/embed)
  graph/extractor.py     # GraphExtractor (LLM → SPO triples → Neo4j)
  indexing/              # Qdrant + Neo4j writers
  main.py                # CLI entrypoint (local dir or S3)

services/api/
  main.py                # FastAPI app, lifespan, router registration
  app/agents/            # LangGraph agent (planner, retriever nodes)
  app/clients/           # Qdrant, Neo4j, Ray LLM, Ray embed, Redis
  app/routes/            # chat, upload, health
  app/auth/jwt.py        # JWT validation

scripts/
  ingest_local.py        # Lightweight local ingestion (no Ray)
  batch_ingest_loop.py   # Batch eval ingestion with retry + validation
  load_test.py           # Locust load test

eval/
  judges/llm_judge.py    # LLM-as-judge scoring
  ragas/run.py           # RAGAS evaluation harness
```
