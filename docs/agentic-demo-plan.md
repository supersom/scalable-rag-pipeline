# Agentic properties — test and demo plan

A plan for demonstrating and empirically testing the agentic behavior of this RAG
platform on the local Ollama stack. Companion to
[`system-overview.md`](system-overview.md).

## What counts as "agentic" here — and what doesn't

Be precise in the demo framing. What the system genuinely demonstrates:

1. **LLM-driven routing** — a planner LLM decides per query between answering
   directly, calling a tool, or retrieving, with structural safeguards against
   malformed/hallucinated decisions.
2. **Tool selection and use** — one tool call per query from a registry (calculator,
   web_search, graph_search, sandbox), with the tool result grounding the final answer.
3. **Hybrid retrieval** — parallel vector (Qdrant) + knowledge-graph (Neo4j) search,
   merged, with source citation and honest "not in my documents" refusal.
4. **A live reasoning trace** — routing/status/answer NDJSON events streamed to the
   client, rendered in the Streamlit UI.
5. **Agent-aware caching** — per-tool cacheability (web results never cached) and
   failure-answer suppression.

What it does **not** do (state this up front; it motivates the port design):
multi-step tool chains, re-planning after a failed tool call, tool + retrieval
combined in one query, step budgets, or multi-turn coreference in routing (the planner
sees only the latest message).

---

## Setup (once)

```bash
docker compose up -d                      # Postgres, Redis, Qdrant, Neo4j
ollama pull llama3.2:3b && ollama pull nomic-embed-text
export TAVILY_API_KEY=...                 # optional but needed for the web_search demo
make dev                                  # FastAPI on :8000

# Ingest a small, demo-friendly corpus (vectors + graph so both retrieval arms fire)
python scripts/ingest_local.py ./data/docs/

# UI (mints its own JWT from JWT_SECRET_KEY and shows the event trace)
streamlit run ui/streamlit_app.py
```

Verify state before demoing: `http://localhost:6333/collections/rag_collection`
(point count > 0) and in the Neo4j browser (`:7474`) `MATCH (n) RETURN count(n)` > 0.

For curl-based demos, mint a token:

```bash
TOKEN=$(python - <<'EOF'
import time, os
from jose import jwt
print(jwt.encode({"sub":"demo","role":"admin","permissions":["chat"],
                  "iat":int(time.time()),"exp":int(time.time())+3600},
                 os.environ["JWT_SECRET_KEY"], algorithm="HS256"))
EOF
)
curl -sN localhost:8000/api/v1/chat/stream \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"message": "sqrt(144)"}'
```

The NDJSON stream itself is the demo artifact — the `routing` event is the agent's
decision, visible before the answer arrives.

---

## Cloud deployment (one-time, EKS)

The same demo runs against the EKS stack. Canonical reference is
[`runbook-eks.md`](runbook-eks.md) (First-time bring-up); this is the condensed
demo-oriented sequence. **Cost note:** the standing stack includes a g5.2xlarge
on-demand GPU node (~$1.21/hr) plus EKS control plane, NAT, and DB baseline — spin up
for the demo window and tear down after (`scripts/cleanup.sh` mirrors the bootstrap
stages).

### 1. Infrastructure (Terraform)

```bash
cd infra/terraform && terraform init && terraform apply   # db_password via terraform.tfvars
```

Capture the outputs bootstrap needs:

```bash
terraform output -raw db_endpoint                # → DB_ENDPOINT
terraform output -raw db_secret_arn              # → DB_SECRET_ARN
terraform output -raw redis_primary_endpoint     # → REDIS_URL (prepend rediss://)
terraform output -raw s3_documents_bucket_name   # → S3_BUCKET
terraform output -raw s3_models_bucket_name      # → MODEL_CACHE_BUCKET
```

### 2. Build and push images to ECR

```bash
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
aws ecr get-login-password --region us-east-1 \
  | docker login --username AWS --password-stdin ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com
GIT_SHA=$(git rev-parse --short HEAD)

# API image
docker build -t ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/api:${GIT_SHA} \
  -f services/api/Dockerfile . && docker push ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/api:${GIT_SHA}

# Ray Serve GPU image (~28 GB — CUDA + vLLM; layer-cached, first build is the slow one)
docker build -t ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:${GIT_SHA} \
  -f services/models/Dockerfile . && docker push ${ACCOUNT_ID}.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:${GIT_SHA}

# Ingestion image (needed for the corpus — this is the graph_search/retrieve demo's data path)
bash scripts/build_push_image.sh ingestion && bash scripts/sync_s3_to_ecr.sh ingestion ${GIT_SHA}
```

(`build_push_image.sh` / `sync_s3_to_ecr.sh` relay via S3 — useful on a slow uplink;
the direct docker push above is equivalent for api/ray-serve.)

**Image freshness caveat:** the ingestion fixes through `ace240e` (NLTK corpora — PDFs
silently produce zero vectors without it) must be in the image you deploy. Build from
current HEAD, not an old tag.

### 3. Populate the S3 model weight cache

```bash
python scripts/cache_models_s3.py    # ~18 GB HF → S3, one-time per bucket
```

Must run before the RayServices are applied, or GPU pods fall back to HuggingFace via
NAT (slower, costs transfer).

### 4. Bootstrap the cluster

```bash
export API_IMAGE_TAG=${GIT_SHA} RAY_IMAGE_TAG=${GIT_SHA}
export DB_SECRET_ARN=... DB_ENDPOINT=... REDIS_URL=rediss://... S3_BUCKET=...
export JWT_SECRET_KEY=$(openssl rand -hex 32)

bash scripts/bootstrap_cluster.sh    # stages 1–14; --start/--end to re-run a range
```

This installs Karpenter + NodePools, KubeRay, Qdrant, Neo4j, creates the
`rag_collection`/`semantic_cache` collections at **1024 dims** (BGE-M3) and the Neo4j
`entity_index` (stage 10 — the cloud equivalent of "making the index"), writes
`app-env-secret` (stage 11), and deploys the API, the LLM/embed RayServices, and the
queue-backed ingestion worker (stages 12–14).

**Demo-specific addition:** stage 11 fetches `TAVILY_API_KEY` from a Secrets Manager
secret that Terraform only provisions if `tavily_api_key` was set in
`terraform.tfvars` (`infra/terraform/secrets.tf`) — without it, the web_search demo
shows the "disabled" degradation path. To demo live search:

```bash
# infra/terraform/terraform.tfvars — uncomment and fill in:
#   tavily_api_key = "tvly-..."
cd infra/terraform && terraform apply   # provisions the Secrets Manager secret
cd ../..

export API_IMAGE_TAG=<current ECR tag>
bash scripts/bootstrap_cluster.sh --start 11 --end 12
```

Stage 11 recreates `app-env-secret` with the real value (fetched by ARN from
Secrets Manager, never typed into a shell), and stage 12 re-applies the Helm chart
so the API Deployment's pod template actually mounts the `secretEnvKeys` entry
(`values.yaml` lists key *names* only). Re-applying the chart naturally rolls the
pods — no separate restart needed. The key lives only in the gitignored
`terraform.tfvars` and Secrets Manager; it's never passed as a shell env var or
committed anywhere.

Then wait for GPU inference (Karpenter provisions the g5.2xlarge in ~2 min; LLM loads
~5 min after):

```bash
kubectl get nodeclaims -w
kubectl get rayservice        # done when llm-service + embed-service are Running
bash scripts/check_ray_health.sh
```

### 5. Ingest the demo corpus

In the cloud the ingestion trigger is S3, not a local script — which is itself
demoable (event-driven, zero query-latency impact):

```bash
aws s3 cp ./data/docs/ s3://${S3_BUCKET}/demo/ --recursive
kubectl logs -f deploy/ingestion-worker            # watch SQS → Ray job submission
```

Only objects uploaded **after** Terraform applied the bucket notification are queued —
re-copy pre-existing objects to enqueue them. Verify exactly as locally: Qdrant point
count and Neo4j node count (port-forward `svc/qdrant 6333` and the Neo4j service, or
`kubectl exec` a curl).

### 6. Access and token

```bash
kubectl port-forward svc/api-service 8000:80 &
TOKEN=$(kubectl exec deploy/api -- python3 -c \
  "from jose import jwt; import time, os; print(jwt.encode({'sub':'demo','role':'admin','permissions':['chat'],'iat':int(time.time()),'exp':int(time.time())+3600}, os.environ['JWT_SECRET_KEY'], algorithm='HS256'))")
```

The Streamlit UI also works against the cluster: point `API_BASE` at the
port-forward (or ingress URL) and set `JWT_SECRET_KEY` to the cluster's value so its
minted tokens validate.

From here, all demo scripts below run unchanged — same endpoint shape, same NDJSON
events. Note the `sandbox` tool is still unavailable: the hardened container exists in
`services/sandbox/` but no K8s Deployment/Service manifest for it exists in `deploy/`
and the bootstrap never deploys it, so `sandbox-service:8080` resolves nowhere even
in-cluster. Treat it the same as locally (skip, or demo graceful tool failure);
deploying it is a backlog item.

---

## Demo script 1 — routing matrix (the core agentic behavior)

Run these in order; for each, call out the `routing` event, then the answer.

| # | Query | Expected route | What it demonstrates |
|---|---|---|---|
| 1 | `hi there` | `direct_answer` | planner short-circuits social queries — no retrieval cost |
| 2 | `sqrt(144) + 15% of 200` | `tool_use / calculator` | deterministic tool grounding; LLM presents `12 + 30` style result |
| 3 | `what's the weather in Oslo today` | `tool_use / web_search` | real-time info routed away from static documents (needs `TAVILY_API_KEY`; without it, shows graceful "disabled" degradation — itself demoable) |
| 4 | a question answerable from the ingested corpus | `retrieve` | hybrid retrieval + `[Source: filename]` citations |
| 5 | `what is <entity from corpus> connected to?` | `tool_use / graph_search` | entity extraction → parameterized Cypher; relationship facts vector search would miss |
| 6 | a question about something *not* in the corpus | `retrieve` | grounded refusal: "I don't have that information in my documents" — and note it is **not cached** |

Pick queries 4–6 against whatever is in `eval/datasets/digested/` so both Qdrant and
Neo4j actually contain the material.

Sandbox caveat: `sandbox` is registered, but its URL hardcodes K8s DNS
(`sandbox-service:8080`), so locally it fails with a connection error. Either skip it,
demo it as "graceful tool-failure handling" (planner routes correctly, tool errors,
responder still answers), or fix the URL to an env var and run
`services/sandbox/runner.py` locally first.

## Demo script 2 — safeguards and degradation

These show the agent is *engineered*, not just prompted:

- **Hallucinated tool** — ask something tool-flavored with no matching tool
  ("translate this to French"): planner may emit a bogus tool name; the safeguard
  coerces it to `retrieve`. Show the API log line `Plan derived: ...` vs the routing
  event.
- **Graph outage** — `docker compose stop neo4j`, re-ask query 4: retrieval degrades
  to vector-only, answer still cited. Restart Neo4j after.
- **LLM planner failure** — (optional) stop Ollama mid-demo: planner exception path
  defaults to `retrieve` rather than erroring the request.

## Demo script 3 — semantic cache as an agentic cost lever

1. Ask query 4; note the multi-second agent path in the event trace.
2. Ask it again *verbatim* → instant answer, no `routing`/`status` events (cache path
   emits only `answer`).
3. Ask a **paraphrase** → still a hit (0.95 cosine threshold) — semantic, not string,
   matching.
4. Ask the weather query twice → second call still routes through `web_search`
   (`cacheable: false`), proving per-tool cache policy.

## Demo script 4 — the known limitation, on purpose

Two-turn conversation: "What is Kubernetes?" → "Who maintains *it*?" The second turn's
routing/refined query degrades because the planner only sees the last message. This is
the honest segue to the roadmap (query rewriter exists in `app/enhancers/` but is
unwired) and to the multi-step design in the agentic_search port plan.

---

## Scripted testing (beyond the live demo)

### Routing-accuracy eval (recommended — cheap and high-signal)

The `routing` event makes planner quality directly measurable. Build
`eval/datasets/routing_eval.jsonl` (~50–80 rows):

```json
{"query": "sqrt(81)", "expected_action": "tool_use", "expected_tool": "calculator"}
{"query": "thanks, bye", "expected_action": "direct_answer", "expected_tool": null}
{"query": "how does the ingestion pipeline work", "expected_action": "retrieve", "expected_tool": null}
```

A small script POSTs each query, captures the first `routing` event, and reports a
confusion matrix (action accuracy, tool accuracy). Suggested distribution: ~15
direct_answer (including one-word inputs, the historical weak spot), ~15 calculator,
~10 web_search, ~10 graph_search, ~25 retrieve, plus ~5 adversarial (tool-shaped
queries with no matching tool) to exercise the safeguards. This gives a regression
gate for any future planner-prompt or model change — and the same expected/actual
framing transfers directly to the agentic_search planner eval.

### End-answer quality

- `eval/judges/llm_judge.py` — grade answers 1–5 against ground truth for a QA set
  drawn from the ingested corpus.
- `eval/ragas/run.py` — faithfulness / relevancy / context precision / recall on the
  same set (needs a judge backend configured).

### Load behavior

`scripts/load_test.py` (Locust) against `/api/v1/chat/stream` — worth running once
with cache warmed vs cold to quantify the semantic-cache lever with numbers.

---

## Success criteria

- Routing accuracy ≥ ~90% on the eval set (action-level), with zero unhandled
  exceptions on the adversarial rows.
- All four demo scripts reproducible from a fresh `docker compose up` + one ingestion
  run, in under ~10 minutes of setup.
- Cache demo shows an order-of-magnitude latency difference between agent path and
  cache hit.
