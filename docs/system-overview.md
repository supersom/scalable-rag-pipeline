# System overview — scalable-rag-pipeline

Consolidated documentation of everything implemented in this repository as of 2026-07:
the agentic RAG platform (FastAPI orchestrator, LangGraph agent, hybrid Qdrant+Neo4j
retrieval, Ray-based model serving and ingestion), the local-dev Ollama adaptation, and
the AWS EKS infrastructure.

Origin: forked from FareedKhan-dev/scalable-rag-pipeline. Ongoing work adapts the
K8s-only design to run locally with Ollama for development and evaluation, while keeping
the code deployable to EKS unchanged. The key principle throughout: **all model and
database endpoints are env-var-driven** — the same code hits Ollama locally and
Ray Serve (vLLM + BGE-M3) in production. No K8s DNS names are hardcoded in the query
path (one known violation is listed under gaps).

---

## System at a glance

The product is an **agentic RAG chat API**: a FastAPI service that routes each user
message through a LangGraph state machine — an LLM planner classifies the query, then
either answers directly, calls exactly one tool, or performs hybrid retrieval
(vector + knowledge graph) before an LLM responder synthesizes a cited answer. Everything
around it exists to ingest documents at scale (Ray Data), serve models elastically
(Ray Serve on GPU nodes), and operate the whole thing on spot-friendly EKS.

```
                    ┌────────────────────────────────────────────────┐
                    │       Client (Streamlit UI / curl / SDK)       │
                    └──────────────────────┬─────────────────────────┘
                                           │ POST /api/v1/chat/stream (JWT)
                                           ▼
   ┌───────────────────────────────────────────────────────────────────────────┐
   │  FastAPI orchestrator (services/api)                                      │
   │                                                                           │
   │  1. JWT auth          2. Semantic cache check (Qdrant, ≥0.95 cosine)      │
   │  3. Load history (Postgres, last 6 turns)                                 │
   │  4. LangGraph agent:                                                      │
   │       ┌─────────┐   direct_answer  ┌───────────┐                          │
   │       │ PLANNER │─────────────────▶│ RESPONDER │──▶ answer                │
   │       │ (1 LLM  │   tool_use  ┌────┴──────┐  ▲                            │
   │       │  call)  │────────────▶│ TOOL NODE │──┘  (calculator, web_search,  │
   │       └────┬────┘             └───────────┘      graph_search, sandbox)   │
   │            │ retrieve   ┌───────────────┐  ▲                              │
   │            └───────────▶│ RETRIEVER     │──┘                              │
   │                         │ Qdrant ∥ Neo4j│  (parallel hybrid retrieval)    │
   │                         └───────────────┘                                 │
   │  5. Stream NDJSON events (routing / status / answer)                      │
   │  6. Persist turn to Postgres; write answer to semantic cache              │
   └───────────┬──────────────────┬───────────────────┬───────────────────────┘
               │ embeddings/LLM   │ vectors           │ graph
               ▼                  ▼                   ▼
   ┌───────────────────┐  ┌──────────────┐  ┌──────────────────┐
   │ Ollama (local) or │  │ Qdrant        │  │ Neo4j            │
   │ Ray Serve on GPU: │  │ rag_collection│  │ entities + rels  │
   │ vLLM Llama-3-8B + │  │ + semantic_   │  │ fulltext index   │
   │ BGE-M3 (1024-dim) │  │   cache       │  │ entity_index     │
   └───────────────────┘  └──────────────┘  └──────────────────┘

   Ingestion (async, never touches query latency):

   S3 upload ──▶ SQS ──▶ ingestion-worker ──▶ Ray job on ingestion RayCluster
     Ray Data: read_binary_files → parse (PDF/DOCX/HTML/TXT/PPTX) → chunk (512/50)
       ├─▶ BatchEmbedder (Ollama /api/embed or Ray Serve) ─▶ QdrantIndexer
       └─▶ GraphExtractor (LLM → SPO triples)             ─▶ Neo4jIndexer
```

### Tech stack

| Layer | Technology |
|---|---|
| API / orchestration | Python, FastAPI (async), LangGraph state machine, httpx with backoff |
| LLM (local) | Ollama `llama3.2:3b` via OpenAI-compatible `/v1/chat/completions` |
| LLM (prod) | vLLM on Ray Serve — Llama-3-8B-Instruct (70B config exists, blocked on GPU quota) |
| Embeddings | local: `nomic-embed-text` (768-dim); prod: BGE-M3 (1024-dim), FP16 |
| Vector DB | Qdrant (`rag_collection` + `semantic_cache` collections) |
| Graph DB | Neo4j (Bolt; fulltext `entity_index`; Helm chart in EKS, container locally) |
| Chat memory | Postgres (local container) / Aurora Serverless v2 (prod), SQLAlchemy async |
| Cache infra | Redis client wired in lifespan; semantic cache itself is Qdrant-backed |
| Ingestion | Ray Data pipeline; `unstructured` for PDF, python-docx, BeautifulSoup, python-pptx |
| Serving platform | Ray Serve via KubeRay RayServices (LLM + embed), Ray Jobs for ingestion |
| Auth | JWT (HS256) validated per-request; scripts pass `AUTH_TOKEN` |
| Ingress | Nginx / Kong manifests with Lua rate limiting |
| Infra as code | Terraform: VPC, EKS 1.30, Karpenter, Aurora, ElastiCache, S3, ECR, SQS, IRSA |
| Node autoscaling | Karpenter — CPU pool (m6i/c6i/r6i, spot) + GPU pool (g5.2xlarge, on-demand) |
| Evaluation | LLM-as-judge (1–5 grading), RAGAS harness, Locust load test |

### Repository layout

```
services/api/            FastAPI app: routes, LangGraph agent, tools, clients, auth
  app/agents/            graph.py (StateGraph), state.py, nodes/{planner,retriever,tool,responder}
  app/tools/             registry.py + calculator, web_search, graph_search, sandbox, vector_search
  app/clients/           qdrant, neo4j, ray_llm (OpenAI-compatible), ray_embed (Ollama-style)
  app/cache/             redis client + semantic cache (Qdrant-backed)
  app/memory/            Postgres chat history (SQLAlchemy async, create_all at startup)
  app/enhancers/         hyde.py, query_rewriter.py — implemented but NOT wired into the graph
  app/models/            vllm_engine.py, embedding_engine.py (Ray Serve deployments)
services/models/serve.py Local/cloud Ray Serve entrypoint mirroring Ollama's API on :8001
services/ingestion/      Dockerfile + constraints for the ingestion Ray image
services/sandbox/        Hardened code-exec container (runner, limits, network policy)
services/gateway/        rate_limit.lua for the ingress
pipelines/ingestion/     Ray Data pipeline: loaders, chunking, embedding, graph, indexing
pipelines/jobs/          sqs_ingestion_worker.py — SQS consumer submitting Ray jobs
scripts/                 ingest_local.py, batch_ingest_loop.py, bootstrap_cluster.sh,
                         build/push image scripts, check_ray_health.sh, load_test.py, …
models/                  Model configs: llama-7b/70b.yaml, bge-m3.yaml, bge-reranker.yaml
deploy/                  Helm (api, qdrant, neo4j), RayService/RayCluster YAMLs, ingress,
                         monitoring PodMonitors, external-secrets
infra/                   Terraform stack + Karpenter NodePools + ARCHITECTURE.md
eval/                    llm_judge.py, ragas/run.py, datasets/ (noisy → ingested → digested)
ui/streamlit_app.py      Chat UI: mints JWTs, streams NDJSON events, shows routing trace
docker-compose.yml       Local infra: Postgres, Redis, Qdrant, Neo4j, MinIO
```

---

## Agent design

The "agentic" core is a LangGraph `StateGraph` (`services/api/app/agents/graph.py`)
over a typed `AgentState` (messages, documents, current_query, action, tool_name,
tool_input, plan). Entry point is the planner; routing is a single conditional edge.

### Planner — LLM classification with structural safeguards

`nodes/planner.py` makes one LLM call (temperature 0, 300 max tokens) with a prompt
built dynamically from the tool registry, asking for a strict JSON object:

```json
{"action": "direct_answer | tool_use | retrieve",
 "tool_name": "...", "reasoning": "...", "refined_query": "..."}
```

Because the local model is small (llama3.2:3b), the planner defends against malformed
output structurally rather than hoping the prompt holds:

- `_extract_json` strips markdown fences and decodes the **last** valid JSON object in
  the output (the model sometimes echoes prompt examples before answering).
- Invalid `action` → fall back to `retrieve`.
- Valid `tool_name` with wrong `action` → coerce to `tool_use`.
- Hallucinated tool name → fall back to `retrieve`.
- Any exception → `retrieve` with the raw user query.

`refined_query` is the standalone search string — the planner is also the de-facto
query rewriter for the retrieval path.

### Routing and execution — single-hop, not a loop

The planner picks exactly one path; there is no iteration, budget, or re-planning:

- **`direct_answer`** → responder answers conversationally (greetings, small talk).
- **`tool_use`** → `tool_node` dispatches to the registry handler (sync or async), and
  the tool's string output becomes the responder's context.
- **`retrieve`** → hybrid retrieval, then responder.

This matters for framing: the platform demonstrates LLM-driven *routing and tool
selection*, one tool call per query — not a multi-step tool-use loop.

### Retriever — parallel hybrid retrieval

`nodes/retriever.py` embeds the refined query (Ray/Ollama embed endpoint), then runs
**Qdrant vector search (top-5)** and a **Neo4j fulltext + 1-hop neighborhood Cypher
query (top-5)** concurrently via `asyncio.gather`, merging and deduplicating. Vector
hits carry `[Source: filename]` provenance; graph hits are rendered as
`node RELATION neighbor` strings. Graph failures degrade to vector-only.

### Tool layer

`tools/registry.py` maps tool name → `{description, handler, cacheable}`. Descriptions
are injected into the planner prompt, so adding a tool is one registry entry.

| Tool | Implementation | Safety mechanism |
|---|---|---|
| `calculator` | `simpleeval` AST evaluation | no eval/builtins/imports; 100-char cap |
| `web_search` | Tavily API (top-3, basic depth) | disabled gracefully without `TAVILY_API_KEY`; `cacheable: false` |
| `graph_search` | LLM extracts entities → **parameterized** Cypher fulltext lookup | LLM never writes Cypher — injection-proof by construction |
| `sandbox` | HTTP POST to hardened sandbox service (no network, CPU/mem limits, 5 s timeout) | isolation at the container level |
| `vector_search` | exists in `tools/vector_search.py` | **not registered** — retrieval is a graph path, not a tool |

### Responder

One LLM call with an action-specific prompt: conversational for `direct_answer`,
"present the tool result directly" for `tool_use`, and a cited-RAG prompt for
`retrieve` (cite `[Source: Filename]`, say "I don't have that information in my
documents" when the context lacks the answer, stay concise).

### Chat flow, caching, and the event stream

`routes/chat.py` (`POST /api/v1/chat/stream`) orchestrates around the graph:

1. **JWT auth** (`app/auth/jwt.py`), session ID assignment.
2. **Semantic cache fast path** — embed the query, search Qdrant `semantic_cache` with
   a 0.95 score threshold; on hit, stream the cached answer and skip the agent entirely.
3. **History** — last 6 turns from Postgres prepended to the message list.
4. **`agent_app.astream`** — each completed node emits an NDJSON event:
   `{"type": "routing", "action", "tool"}` after the planner, `{"type": "status",
   "node"}` per node, `{"type": "answer", "content"}` from the responder. This is a
   built-in reasoning trace, consumed live by the Streamlit UI.
5. **Post-processing** — persist both turns to Postgres; cache the answer *only if* the
   tool used is marked `cacheable` (web_search is not — its answers go stale) *and* the
   answer doesn't match `NO_ANSWER_PHRASES` (failure answers are never cached).

Dependency injection via FastAPI `Depends` wrappers keeps cache/memory/LLM mockable in
tests.

### Implemented but unwired

`app/enhancers/hyde.py` (hypothetical document embeddings) and
`app/enhancers/query_rewriter.py` (coreference-resolving rewrite) exist as modules but
are referenced nowhere in the graph or routes. The README describes them as active —
that is aspirational. Related: the planner classifies on the **latest message only**, so
multi-turn coreference ("how much does *it* cost?") routes poorly until the rewriter is
wired in.

---

## Models and serving

### Local development (current daily driver)

- **Ollama** serves both LLM (`llama3.2:3b`) and embeddings (`nomic-embed-text`,
  768-dim) on :11434. `RAY_LLM_ENDPOINT` / `RAY_EMBED_ENDPOINT` point at it.
- **Alternative:** `make ray-serve` runs `services/models/serve.py` — a local Ray Serve
  app on :8001 that *mirrors Ollama's API shape* (`/v1/chat/completions`,
  `/api/embeddings`), serving Llama-3.2-1B-AWQ + nomic-embed on a fractional local GPU.
  Switching backends is a `.env` change only.

### Production (EKS)

- **LLM RayService** (`deploy/ray/ray-serve-llm.yaml`): vLLM `AsyncEngine`, default
  model `NousResearch/Meta-Llama-3-8B-Instruct` (ungated mirror; override via
  `MODEL_ID`). `models/llm/llama-70b.yaml` (TP=4, g5.12xlarge) is configured but
  deferred pending a GPU vCPU quota increase.
- **Embedding RayService**: BGE-M3, 1024-dim, FP16, batch 32.
- Both deployments sync weights from an **S3 model cache**
  (`s3://<bucket>/models/<org>/<name>/`) to node-local disk at startup — the S3 Gateway
  VPC endpoint makes this free and NAT-independent. `scripts/cache_models_s3.py`
  populates the bucket (~18 GB).
- Ray Serve autoscaling (`min_replicas: 1, max_replicas: 4`) rides on Karpenter node
  autoscaling; routes are `/llm/chat/completions` and `/embed/embeddings`.
- **Dimension gotcha:** local nomic-embed is 768-dim, prod BGE-M3 is 1024-dim
  (`EMBED_DIM`). Qdrant collection dimensions are immutable — switching embedders
  requires dropping and re-creating collections and re-ingesting.

---

## Data and ingestion

### Pipeline (`pipelines/ingestion/`)

A Ray Data pipeline, identical code for a local dir or `s3://` prefix:

1. `ray.data.read_binary_files(source, include_paths=True)`
2. **Parse + chunk** (`process_batch`): per-extension loaders (PDF via `unstructured`,
   DOCX, HTML, TXT, PPTX), imported lazily inside each branch so a missing optional dep
   only affects that format. Chunking: 512 chars with 50 overlap.
3. **Embed** (`BatchEmbedder`, concurrency 2): batch call to Ollama `/api/embed` or
   per-text calls against other endpoint shapes.
4. **Index vectors** → `QdrantIndexer` (text + metadata + vector).
5. **Graph extraction** (optional, `GraphExtractor`, concurrency 2): one LLM call per
   chunk extracts `(subject, predicate, object)` triples as JSON → `Neo4jIndexer`.

Parse failures are logged and skipped (partial-batch resilience) — see gaps for why
this bit three times.

### Production trigger path

S3 `ObjectCreated` → SQS → `pipelines/jobs/sqs_ingestion_worker.py` (K8s Deployment):
validates extension, percent-encodes the S3 URI (spaces/parens break PyArrow), submits
a Ray job to the CPU ingestion RayCluster with pipeline env vars in the job
`runtime_env`, extends SQS visibility while polling, and deletes the message only on
`SUCCEEDED`. Only objects uploaded after Terraform applies the bucket notification are
queued.

### Local ingestion and eval datasets

```bash
python scripts/ingest_local.py ./data/docs/          # lightweight, no Ray (PDF/TXT/MD)
python -m pipelines.ingestion.main ./data/docs/      # full Ray pipeline, all formats
python scripts/batch_ingest_loop.py                  # batch eval ingestion w/ retry
```

Eval corpus state machine: `eval/datasets/noisy_data/` (pending) → `ingested/`
(vectors done) → `digested/` (vectors + graph) → `failed/`.

---

## Infrastructure (AWS EKS)

Terraform-managed (`infra/terraform/`), documented in depth in `infra/ARCHITECTURE.md`.

- **VPC** 10.0.0.0/16, 3 AZs, single NAT gateway, plus S3 Gateway and ECR/STS/Secrets
  Manager interface endpoints to keep traffic off the NAT.
- **EKS 1.30** (an upgrade plan to 1.35 is drafted in `docs/upgrade-k8s-1.30-to-1.35.md`),
  managed system node group (m6i.large, CriticalAddonsOnly taint), EBS CSI, gp3 storage
  class.
- **Karpenter**: CPU NodePool (m6i/c6i/r6i, spot or on-demand, consolidation after
  30 s) and GPU NodePool (pinned to g5.2xlarge on-demand after quota constraints;
  `nvidia.com/gpu` taint). Consolidation has evicted Ray heads and stateful pods —
  `do-not-disrupt` annotations are a tracked follow-up.
- **Data services**: Aurora Serverless v2 Postgres (chat history, managed master
  password), ElastiCache Redis, Neo4j via Helm (gp3 PVC, no public LB), Qdrant via
  Helm (3-replica StatefulSet).
- **Images**: built locally, tarball'd to S3, then synced to ECR
  (`build_push_image.sh` / `sync_s3_to_ecr.sh`, with post-push cleanup of stale
  tarballs and tags). Tags are git-SHA based.
- **Bootstrap**: `scripts/bootstrap_cluster.sh` runs the full cluster bring-up as
  numbered stages with `--start/--end` range flags for partial reruns; `cleanup.sh`
  mirrors it for teardown.
- **Security**: JWT on all API routes; DBs in private subnets; IRSA least-privilege
  (ingestion can write S3, API cannot); sandbox execution in a no-network hardened
  container; secrets via External Secrets / Secrets Manager.
- **Ingress**: Nginx/Kong with a Lua rate limiter (`services/gateway/rate_limit.lua`).
- **Observability**: Prometheus PodMonitors for Ray heads/workers are written
  (`deploy/monitoring/`) but not yet deployed; metric objects exist in
  `libs/observability/metrics.py` but no `/metrics` route is wired. Health checking is
  `scripts/check_ray_health.sh` (CRD status → `ray status` → live endpoint probes).

**Cost posture** (`docs/cost_model.md`): GPU instances and external LLM calls dominate;
levers are spot (~70% off, CPU pool only right now), semantic caching, scale-to-zero on
Ray Serve + Karpenter, and the S3 model cache avoiding NAT egress.

---

## Evaluation and testing

- **LLM-as-judge** (`eval/judges/llm_judge.py`): grades (question, ground truth,
  system answer) 1–5 with reasoning, via the same env-driven LLM client.
- **RAGAS** (`eval/ragas/run.py`): faithfulness, answer relevancy, context
  precision/recall → CSV report. Assumes an OpenAI-compatible judge backend.
- **`scripts/eval_pipeline.py`** drives end-to-end evaluation; `scripts/load_test.py`
  is a Locust load test against the chat endpoint.
- **UI** (`ui/streamlit_app.py`): mints its own JWTs from `JWT_SECRET_KEY`, streams
  the chat, and renders the routing/status event trace per turn — the main manual
  test surface.

---

## Known gaps and stale spots

Carried from DEVLOG/backlog so the next design phase sees them:

- **Silent ingestion failures**: three consecutive incidents (missing `pkg_resources`,
  `matplotlib`, NLTK corpora) where PDF parsing failed per-file, the Ray job exited 0,
  and SQS messages were deleted — zero vectors written with "Ingestion succeeded" logs.
  A parse-failure metric/structured log is still unbuilt; PDFs ingested under the bad
  images need re-queuing after the `ace240e` image deploys.
- **`ensure_collection()` has no retry** — Qdrant unreachable at API startup crashes
  the lifespan and K8s crash-loops the pod.
- **Enhancers unwired** — HyDE and the query rewriter exist but are dead code; planner
  sees only the latest message, so multi-turn coreference routes poorly.
- **`SANDBOX_URL` hardcodes K8s DNS** (`http://sandbox-service:8080/execute`) —
  violates the env-var-endpoint principle; the sandbox tool cannot work locally.
- **`/metrics` route missing**; PodMonitors and kube-prometheus-stack install pending.
- **768 vs 1024 embedding dims** between local and EKS; Qdrant collections are
  immutable — re-create before cross-environment re-ingestion.
- **KubeRay v1.0.0 `serveService` port bug** — worked around by patching ports in
  bootstrap; operator upgrade tracked.
- **RayService rolling updates need brief downtime** under the current two-GPU
  capacity (no surge headroom).
- **`chat_history` via `create_all()`** at startup — fine for dev; Alembic migrations
  are the production path. A cold-start race against Aurora scale-from-zero exists.
- **README overstates** the wired feature set (HyDE/rewriter, "multi-step reasoning");
  this document is the accurate inventory.
