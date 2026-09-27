# Evaluation plane: current coverage and remaining gaps

Status: current-state assessment as of 2026-09-22.

This document compares the repository's current evaluation harness with the
target production evaluation system described below:

> Production produces evidence; a separate evaluation plane interprets it;
> deployment gates act on versioned, reproducible results.

The current implementation is a useful offline, in-process evaluation tool for
planner routing, retrieval and answer behavior. It is not yet an event-driven
evaluation plane for production traffic.

## Target production evaluation system

The production target is a separate **evaluation plane**. The FastAPI agent
serves users and produces reconstructable evidence; independent evaluation
services interpret that evidence for automated scoring, human review, replay
and release decisions. Expensive evaluation must not execute synchronously in
the API pod.

### 1. Instrument every production run

Every agent invocation must have a stable `run_id` and a trace that represents
the actual execution path:

```text
run_id
├── request
├── retrieval
│   ├── rewritten query
│   ├── vector search
│   └── reranking
├── generation
├── tool call 1
├── tool call 2
└── final response
```

The completed-run event should contain enough information to identify the
request, reproduce the configuration and locate the detailed payloads:

```json
{
  "run_id": "run-123",
  "timestamp": "2026-09-22T12:00:00Z",
  "tenant_id": "tenant-7",
  "user_role": "support-agent",
  "use_case": "policy-question",
  "input_ref": "s3://eval-traces/run-123/input.json",
  "output_ref": "s3://eval-traces/run-123/output.json",
  "retrieval": {
    "query": "rewritten query",
    "document_ids": ["doc-8:v4", "doc-12:v2"],
    "chunk_ids": ["chunk-81", "chunk-125"],
    "scores": [0.91, 0.83],
    "acl_filter_version": "acl-17",
    "index_snapshot": "index-2026-09-18-03"
  },
  "trajectory": [
    {
      "step": 1,
      "action": "search_policy",
      "arguments_ref": "s3://eval-traces/run-123/tool-1-input.json",
      "result_ref": "s3://eval-traces/run-123/tool-1-output.json",
      "latency_ms": 420
    }
  ],
  "configuration": {
    "agent_version": "agent-2.4.1",
    "prompt_hash": "sha256:...",
    "model": "model-name-version",
    "embedding_model": "embedding-v3",
    "chunking_version": "chunker-12",
    "reranker_version": "reranker-4",
    "tool_schema_version": "tools-8",
    "policy_version": "policy-31"
  },
  "operations": {
    "latency_ms": 12400,
    "input_tokens": 8200,
    "output_tokens": 640,
    "estimated_cost": 0.043,
    "status": "success"
  }
}
```

Large prompts, retrieved documents and tool results should live in encrypted
object storage. Events and trace indexes should carry references rather than
becoming large-payload stores. Redaction must occur before export. Sensitive raw
traces need stricter access controls and shorter, explicit retention.

### 2. Keep evaluation off the request path

The synchronous request path should contain only controls required to return a
response safely:

- Tenant and document ACL enforcement.
- Tool authorization and argument validation.
- Output-schema validation.
- DLP and critical safety checks.
- Citation-existence checks.

Quality graders, broad LLM judges and replay should run asynchronously:

```text
FastAPI agent
    ├── return or stream the answer
    └── publish run-completed event
                    ↓
               evaluation queue
                    ↓
              evaluation workers
```

Best-effort event delivery is sufficient when occasional telemetry loss is an
accepted tradeoff. When every run is an audit record, use a transactional
outbox:

1. Commit the run record and outbox row in PostgreSQL.
2. Publish the outbox event to the queue.
3. Mark it delivered only after broker acknowledgement.

This prevents a successful user response from silently losing its audit and
evaluation evidence.

### 3. Maintain purpose-specific datasets

One dataset should not be reused for every decision.

| Dataset | Purpose |
|---|---|
| Golden | Domain-expert cases with expected evidence and behavior. |
| Regression | Important production failures retained permanently. |
| Production sample | A representative, privacy-reviewed traffic distribution. |
| Adversarial | Injection, exfiltration, ACL bypass, malformed tools and loops. |
| Edge cases | Ambiguity, multilingual input, long sessions and stale documents. |
| Holdout | Restricted cases used only for final promotion decisions. |
| Shadow/canary | Current production traffic evaluated against a candidate. |

Datasets should be versioned like code. A durable case schema includes:

```text
dataset_name and dataset_version
case_id and source
input
identity_and_acl_fixture
expected_documents
required or acceptable answer facts
forbidden claims
expected and forbidden tool calls
expected abstention behavior
allowed trajectory
rubric_version
review_status and reviewer
```

A single ideal answer is often brittle ground truth for enterprise RAG. Required
evidence, required facts, acceptable alternatives, forbidden claims, abstention
behavior and allowed tool trajectories survive model and wording changes more
reliably. Evaluation suites should be task-specific, continuously expanded from
production failures, and calibrated against human review, consistent with
[OpenAI's evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices).

### 4. Support two replay modes

#### Component replay

Reuse the evidence retrieved during the original production run:

```text
stored question + stored context
              ↓
candidate prompt or generation model
              ↓
new answer
```

This isolates generation-model, prompt, context-formatting, citation and output
validation changes. It is fast and reproducible, but cannot reveal retrieval
regressions.

#### End-to-end replay

Run the original input through the complete candidate system:

```text
question
  -> rewrite
  -> retrieval
  -> reranking
  -> context assembly
  -> agent and tools
  -> answer
```

This evaluates changes to chunking, embeddings, indexes, rerankers, agent logic
and tools. The run must identify whether it used an immutable corpus/index
snapshot or intentionally evaluated against today's corpus. Every result must
record the replay mode and data/index policy.

### 5. Score layers independently

A single aggregate RAG score hides the cause and severity of failures.

#### Retrieval

Measure:

- Recall@k, Precision@k, MRR and NDCG.
- Reranker uplift and duplicate-context rate.
- Retrieval latency by stage.
- ACL violations.
- Stale or wrong-version document retrieval.

ACL violations are hard failures and must not be averaged into a quality score.

#### Answer generation

Measure:

- Claim-level groundedness.
- Citation precision and citation recall.
- Completeness and answer relevance.
- Correct abstention.
- Unsupported-claim and contradiction rates.

Groundedness should decompose the answer into atomic claims, map each claim to
evidence, and classify it as `SUPPORTED`, `CONTRADICTED` or
`NOT_IN_EVIDENCE`. This produces better debugging evidence than one holistic
numeric score.

#### Agent trajectory

Evaluate both the final result and the path:

- Correct tool and arguments.
- Required tools called and forbidden tools avoided.
- Authorization respected.
- Unnecessary calls and loops.
- Correct tool-failure handling.
- Side effects executed exactly once.
- Required human approval obtained.
- Final task completed.

A successful answer must not conceal a dangerous trajectory.

#### Safety and security

Keep separate suites for cross-tenant retrieval, retrieved-document prompt
injection, secret extraction, unauthorized tools, DLP failures, role escalation,
malicious tool results, unsafe writes and audit-log omissions. Critical release
thresholds should permit zero failures.

#### Operational quality

Record end-to-end p50/p95/p99 latency, time to first token, retrieval/tool
latency, tokens and cost per successful task, throttling, retries, agent steps,
queue time, cancellations and timeouts.

### 6. Use a scorer hierarchy

Run the cheapest, most deterministic checks first:

1. **Deterministic checks:** schema validity, exact labels, tool arguments,
   citation existence, ACL compliance, latency and cost.
2. **Reference-based checks:** expected evidence/facts and required or forbidden
   actions.
3. **LLM judges:** narrowly scoped groundedness, completeness, relevance and
   pairwise comparisons.
4. **Environment simulators:** verify that the CRM, ticket, order or workflow
   reached the expected state.
5. **Human review:** resolve domain ambiguity, judge disagreement and high-risk
   outputs.

Prefer classification or pairwise judge questions over unrestricted ratings.
Automated judges must be calibrated on human-labelled cases, including
false-positive, false-negative and disagreement analysis.

### 7. Provide three execution paths

#### Pull-request CI

Run a fast, hermetic subset on every relevant change: deterministic checks,
mocked tools, critical ACL/security cases and a small model-based smoke suite.

#### Nightly or pre-release

Run the complete golden, regression and adversarial suites, end-to-end
retrieval, candidate-versus-production comparisons, latency/cost benchmarks,
confidence intervals and human review of disagreements. Kubernetes Jobs can
shard bounded runs and aggregate their results.

#### Continuous production evaluation

Asynchronously sample ordinary traffic and capture all high-signal events:

- A small stratified percentage of successful traffic.
- All negative feedback.
- All policy/safety triggers.
- All failed, timed-out or looping runs.
- Samples stratified by tenant, use case, language and document source.

Evaluation capacity should scale from queue lag. Exhausted judge quotas delay
evaluation rather than user traffic.

### 8. Make release gates explicit

Compare candidate and production configurations on the same versioned cases.
Keep security, quality, latency and cost as separate release decisions.

An illustrative policy is:

```text
Hard gates:
- ACL violations = 0
- Unauthorized tool actions = 0
- Critical injection successes = 0
- Schema compliance = 100%

Quality gates:
- Retrieval recall@10 regression <= 2 percentage points
- Grounded-answer quality is non-inferior
- Task-completion regression <= 1 percentage point

Operational gates:
- p95 latency increase < 10%
- Cost per successful task increase < 5%
- Agent loop rate < 0.5%
```

The actual thresholds must be owned and approved according to product risk.
Every release should retain an immutable report containing candidate and
baseline fingerprints, dataset versions, corpus/index snapshot, grader versions,
per-case results, confidence intervals, failed hard gates and human approvals.

### 9. Map the evaluation plane onto this stack

A practical target architecture is:

```text
FastAPI agent pods
    ↓ OpenTelemetry spans + run events
OTel collector / evaluation queue
    ├── object storage: trace payloads and dataset snapshots
    ├── PostgreSQL: runs, labels, outbox and release decisions
    ├── Opik: trace inspection, experiments and grader results
    └── evaluation-worker Deployment
              ├── deterministic scorers
              ├── retrieval and trajectory scorers
              ├── calibrated LLM judges
              └── human-review queue

Kubernetes evaluation Jobs
    └── component and end-to-end replay

CI/CD
    └── read completed evaluation decision -> promote or block
```

Choose one authoritative experiment/evaluation registry rather than duplicating
identical records across Opik and MLflow. Git should remain authoritative for
dataset definitions, scorer code, gate policy and approved baselines.

The central rule is: **production produces evidence; the evaluation plane
interprets it; deployment gates act on versioned, reproducible results.**

## Status legend

| Status | Meaning |
|---|---|
| Met | The current implementation substantially satisfies the requirement. |
| Partial | A useful part exists, but important production behavior is absent. |
| Scaffold | Supporting code exists but is not connected to the running system. |
| WIP | Implemented locally but not yet committed or hardened. |
| Gap | No current implementation was found. |

## Executive summary

| Requirement | Status | Current implementation | Main gap |
|---|---|---|---|
| Separate evaluation plane | Partial | `eval/harness/` is separate from FastAPI and is run manually. | It invokes application nodes in the same Python process; there are no production run events, evaluation workers or independent storage. |
| Production run instrumentation | Scaffold | FastAPI OpenTelemetry auto-instrumentation, a console exporter and a trace-ID helper exist. | Observability setup has no call site; there is no durable `run_id`, agent-step spans, run-completed event, payload references or redaction pipeline. |
| Evaluation off the request path | Partial | Existing offline evaluation does not delay user requests. | Production does not publish evidence to a queue, and there is no evaluation consumer or transactional outbox. |
| Multiple versioned datasets | Partial | Routing and QA JSONL datasets plus a hashed corpus manifest exist. | No distinct regression, production-sample, adversarial, edge, holdout or shadow/canary datasets. |
| Component and end-to-end replay | Partial | The harness can invoke planner/retriever components and the full LangGraph locally. | It cannot replay captured production runs, reuse original contexts, or select a pinned index snapshot versus today's corpus. |
| Independent layer scoring | Partial | Routing, retrieval and initial answer scorers exist. | Claim-level groundedness, full trajectory, ACL/security and detailed operational scoring are absent. |
| Hierarchy of scorers | Partial | Deterministic, reference-based and optional LLM-judge checks exist. | No simulators, human-review workflow or human calibration of judges; the judge uses broad 1-5 scores. |
| PR, nightly and continuous paths | Gap | Runs are launched manually through the CLI. | No checked-in CI workflow, scheduled/pre-release jobs, Kubernetes evaluation jobs, sharding or continuous production sampling. |
| Explicit release gates | Partial | Baseline comparability, Wilson intervals and invalid-run exit codes exist. | No policy-driven quality/security/latency/cost thresholds, non-inferiority gates, approvals or immutable release report. |
| Evaluation-plane stack | Scaffold | PostgreSQL, OpenTelemetry packages, Kubernetes and an ingestion SQS queue exist elsewhere in the repo. | They are not wired into evaluation; there is no trace payload store, evaluation queue, worker deployment, Opik integration or evaluation registry. |

## Current foundation

The current harness already provides pieces that should become the scoring core
of the future evaluation plane:

- Validated, hashed JSONL loaders in `eval/harness/datasets.py`.
- A seeded, file-hashed corpus manifest in `eval/sets/corpus_manifest.json`.
- Passive planner and Qdrant recorders in `eval/harness/recorders.py`.
- Routing scoring in `eval/harness/routing.py`.
- Retrieval scoring in `eval/harness/retrieval.py`.
- Deterministic answer checks and optional LLM judging in the current working
  tree: `eval/harness/answer.py` and `eval/judges/llm_judge.py`.
- Layer-specific preflight probes and an evaluation manifest.
- Per-case `results.jsonl`, machine-readable `summary.json`, human-readable
  `summary.md`, and tracked routing/retrieval baselines.
- Wilson confidence intervals and guarded baseline comparisons.
- 28 routing cases and 37 QA cases.

The answer layer is marked WIP because its implementation and baseline are
currently uncommitted, and its invalid-run and metric-direction semantics still
need hardening.

## 1. Instrument every production run

**Status: Scaffold.**

### What exists

- `services/api/app/observability.py` can auto-instrument FastAPI.
- `libs/observability/tracing.py` installs an OpenTelemetry tracer provider and
  a console exporter.
- `libs/utils/ids.py` defines `generate_trace_id()`.
- `services/api/app/routes/chat.py` creates a conversation `session_id`, streams
  planner/node status, and persists user and assistant messages to PostgreSQL.
- Prometheus request, latency and token metric objects are declared in
  `libs/observability/metrics.py`.

### Gaps

- `setup_observability()` and the Prometheus helpers have no current call sites.
- The configured trace exporter writes to the console, not an OTLP collector.
- A conversation `session_id` is not a stable identifier for one agent run. A
  distinct `run_id` is missing.
- There are no explicit spans for cache lookup, planner, query rewriting,
  embedding, vector search, graph search, reranking, tool calls, generation,
  persistence or streaming.
- No structured run-completed event captures tenant/use-case identity,
  document/chunk identifiers, ranked scores, tool trajectory, configuration
  versions, token counts, cost and final status.
- Retrieval evidence is recorded only during offline harness execution.
  Production requests do not retain the ranked evidence needed for replay.
- There is no encrypted object-store path for large prompts, contexts and tool
  results, nor reference fields such as `input_ref` or `output_ref`.
- There is no export-time redaction policy, sensitive/raw trace tier, retention
  policy or evaluation-specific access control.
- The system does not record an ACL-filter version, index snapshot, chunking
  version, reranker version, tool-schema version or policy version.

### Target completion criteria

Every cached and non-cached response should emit one durable run record with a
stable `run_id`, trace/span identifiers and enough versioned evidence to replay
or audit the result without scraping application logs.

## 2. Keep evaluation off the request path

**Status: Partial.**

### What exists

- The offline harness is separate and does not run graders inside `/stream`.
- Production uses background tasks for chat-history persistence on cache hits.
- JWT and Pydantic request validation are synchronous request-path controls.

### Gaps

- The API does not publish a run-completed event.
- There is no evaluation topic/queue or worker consumer.
- PostgreSQL contains chat history, but no durable evaluation-run table or
  transactional outbox.
- The repository's SQS queue is for document ingestion, not evaluation.
- Delivery semantics are undefined: best-effort telemetry versus audit-grade
  guaranteed delivery.
- The request path does not yet show the complete required synchronous control
  set: tenant-aware retrieval ACLs, tool authorization and argument validation,
  DLP, output-schema validation, critical safety checks, and citation existence.
- There is no queue-lag, retry, dead-letter or judge-quota behavior for delayed
  evaluation.

### Target completion criteria

The API should perform only safety-critical synchronous checks, then publish a
small run-completed event. Expensive graders should run in independent workers.
Audit-required deployments should use a PostgreSQL transactional outbox.

## 3. Maintain several datasets

**Status: Partial.**

### What exists

- `routing.jsonl` contains routing/tool cases with tags, acceptable alternatives
  and optional calculator expectations.
- `qa.jsonl` contains hand-written, synthetic and golden-origin cases, source or
  fuzzy labels, answerability, reference context and optional ground truth.
- Dataset files are hashed into each run manifest.
- The corpus manifest records filenames and SHA-256 hashes.
- Unanswerable cases and some injection-tagged routing cases exist.

### Gaps

- No independently managed golden, regression, production-sample, adversarial,
  edge-case, holdout or shadow/canary suites.
- Production failures are not promoted into a regression dataset.
- No traffic-distribution snapshots, sampling weights or protected holdout.
- Rows lack tenant/ACL fixtures, required facts, acceptable alternatives,
  forbidden claims, expected/forbidden tool calls, rubric version, source trace,
  review state, reviewer and explicit dataset version.
- Most answer labels use one `ground_truth` string instead of evidence, required
  facts, forbidden claims, abstention requirements and allowed trajectories.
- Critical slices are too small for release gating: source-labelled retrieval
  has seven cases and unanswerable retrieval has ten.

### Target completion criteria

Keep Git as the canonical dataset source. Give every dataset and case explicit
provenance, version and review status, and reserve an independently reviewed
holdout for release promotion.

## 4. Support component and end-to-end replay

**Status: Partial.**

### What exists

- Routing evaluation invokes `planner_node` independently.
- Retrieval invokes `retrieve_node` and records ordered Qdrant results.
- Answer evaluation can invoke the complete `agent_app` in-process.
- Corpus and dataset hashes provide partial offline reproducibility.
- Qdrant point count/vector size and Neo4j node count enter the run manifest.

### Gaps

- Inputs come from static datasets, not stored production evidence.
- Component replay cannot reuse the exact rewritten query, retrieved chunks and
  context assembly from an original production run.
- There is no replay-mode field for stored-context versus end-to-end replay.
- Qdrant point count is not an immutable index snapshot; the same count can hold
  different content.
- The graph index is represented only by node count and is not reproducible.
- Chunking, reranking and retrieval-policy versions are absent from manifests.
- No supported choice exists between a pinned historical index and today's
  corpus.
- No deployed HTTP replay covers cache, JWT, ingress, streaming or deployment
  configuration.

### Target completion criteria

An evaluator should select `component` or `end_to_end` replay for a stored
`run_id`, with the corpus/index policy recorded in an immutable result manifest.

## 5. Score each layer independently

**Status: Partial.**

### Retrieval

Implemented:

- Source Hit@1, Hit@3 and Hit@5.
- Mean reciprocal rank and source recall@5.
- Fuzzy lexical relevance calibrated against dual-labelled cases.
- Answerable/unanswerable top-score AUROC and suggested cutoff.
- Empty-context rate, vector/graph arm counts and p50/p95 latency.

Missing:

- Chunk-level Precision@k, Recall@k and NDCG.
- Reranker uplift; the production retriever does not currently run a reranker.
- Duplicate-context rate.
- ACL violations as hard failures.
- Stale-document and document-version correctness.
- Retrieval p99 and separate embedding/vector/graph/reranking latency.
- Independent graph-arm relevance scoring.

### Answer generation

Implemented or WIP:

- Citation filename existence against retrieved filenames.
- Correct and false refusal checks.
- Calculator expected-number presence.
- Detection of known tool/retrieval error strings in answers.
- Optional 1-5 correctness and faithfulness judges.

Missing:

- Claim extraction and claim-to-evidence mapping.
- Supported, contradicted and unsupported claim classification.
- Citation precision and citation recall.
- Calibrated completeness, relevance and contradiction metrics.
- Output-schema and DLP metrics.
- A numeric/tool-output comparator instead of substring presence.

The citation metric only detects citations to filenames that were not retrieved.
It does not establish that the cited passage supports the associated claim.

### Agent trajectory

Implemented:

- Expected routing action and tool selection.
- Raw planner output versus post-safeguard behavior.
- Per-tool precision/recall, routing stability and calculator execution checks.
- Planner infrastructure failure separated from model failure.

Missing:

- Exact tool-argument scoring and required/forbidden call sequences.
- Tool authorization and user/tenant policy checks.
- Unnecessary-call, repeated-call and loop rates.
- Tool-failure recovery.
- Side-effect exactly-once and environment-state assertions.
- Human-approval behavior.

### Safety and security

Some routing cases carry an `injection` tag, but there is no independent suite
or hard gate for cross-tenant retrieval, retrieved-document injection, secret
extraction, unauthorized tools, DLP failures, role escalation, malicious tool
results, unsafe writes or audit-log omissions.

### Operational quality

The harness records layer p50/p95 latency and some errors. It does not record
end-to-end p99, time to first token, token usage, cost per successful task,
provider throttling, retries, agent steps, queue time, cancellations or timeouts.

## 6. Use a hierarchy of scorers

**Status: Partial.**

### What exists

1. Deterministic checks for dataset validity, routing labels, citation filename
   validity, refusal phrases, calculator output and error leakage.
2. Reference-based retrieval checks against expected sources and passages.
3. Optional local LLM judges for correctness and faithfulness.

### Gaps

- Deterministic scoring does not cover ACLs, schemas, tool arguments, cost or
  operational budgets.
- Labels lack required/forbidden facts and full trajectory contracts.
- The answer judge emits broad 1-5 scores rather than narrow claim-level
  `SUPPORTED`, `CONTRADICTED` or `NOT_IN_EVIDENCE` classifications.
- Judge parsing and failure semantics need further hardening.
- The judge is not calibrated against human labels; there is no false-positive,
  false-negative or inter-rater analysis.
- No environment/task simulators verify external state.
- No human-review queue handles risk, ambiguity or judge disagreement.

## 7. Build three evaluation execution paths

**Status: Gap.**

### Pull-request CI

The repository has unit tests, but no checked-in CI workflow and no small,
hermetic evaluation lane. There are no required evaluation status checks.

### Nightly or pre-release evaluation

Full runs can be launched manually with `python -m eval.harness`, but there is
no scheduler, candidate-versus-production workflow, Kubernetes evaluation Job,
controller, sharding, aggregation or disagreement review.

### Continuous production evaluation

There is no asynchronous sampling of successful traffic, negative feedback,
safety events, failures or loops. There is no stratification by tenant, use
case, language or source, and no scaling from evaluation queue lag.

## 8. Make release gates explicit

**Status: Partial.**

### What exists

- Tracked routing and retrieval baselines.
- Comparability checks on model, embedding model, dataset and corpus hashes.
- Wilson 95% intervals and a `within_noise` comparison label.
- Routing runs become invalid above 20% planner infrastructure failure.
- Exit code 2 for invalid preflight/run or incomparable baseline.
- Manifest, per-case results, aggregate summary and Markdown report artifacts.

### Gaps

- A quality regression outside the confidence interval does not fail the CLI.
- No versioned gate policy defines metric direction, absolute thresholds,
  allowed regressions, minimum sample sizes or confidence-bound rules.
- Security, quality, latency and cost are not separate decision classes.
- No zero-tolerance ACL, authorization, injection or schema gates exist.
- Answer false refusals use the generic `successes/n` shape even though lower is
  better.
- Retrieval and answer runs can contain errors while remaining valid; answer
  currently hardcodes `invalid: false`.
- Manifests lack a full configuration fingerprint, immutable index snapshot,
  grader implementation version, dependency/container digest and approvals.
- Baseline promotion does not require a clean reviewed run, and dirty-tree
  detection ignores untracked files.
- Artifacts are mutable local files rather than retained release evidence.

## 9. Map the target architecture onto this stack

**Status: Scaffold.**

### Existing pieces that can be reused

| Target component | Existing foundation |
|---|---|
| FastAPI agent | `services/api/app/routes/chat.py` and LangGraph. |
| OpenTelemetry | SDK dependencies, FastAPI scaffold and console exporter. |
| PostgreSQL | Chat-history storage and async SQLAlchemy setup. |
| Object storage | S3 in the ingestion architecture. |
| Queue infrastructure | SQS with retry/DLQ for ingestion. |
| Kubernetes | Existing deployment and Ray/KubeRay infrastructure. |
| Offline scorers | `eval/harness/` routing, retrieval and answer modules. |
| Experiment artifacts | Local run directories and tracked baseline JSON. |

### Missing evaluation-plane components

- Production agent-step spans and structured run-completed events.
- A production OTLP collector/export destination.
- An evaluation-specific queue and delivery policy.
- PostgreSQL run/outbox schemas.
- Encrypted trace-payload and dataset-snapshot storage.
- An evaluation-worker deployment that invokes the current scorers.
- Replay orchestration and Kubernetes evaluation Jobs.
- A human-review queue and approval model.
- An authoritative experiment/evaluation registry.
- Opik tracing, experiment publication and grader-result integration.
- CI/CD integration that promotes or blocks a candidate.

The ingestion queue demonstrates a reusable pattern, but evaluation should use
separate schemas, IAM permissions, retention and failure policies.

## Recommended delivery sequence

### Phase 1: make the offline harness gate-ready

1. Commit and harden answer evaluation.
2. Add metric direction and invalid-run semantics.
3. Add a versioned gate policy and regression exit codes.
4. Make the runtime hermetic and capture complete fingerprints.
5. Add a fast PR lane and a full scheduled lane.

### Phase 2: produce reconstructable production evidence

1. Add `run_id` and explicit spans across the graph and dependencies.
2. Define and version the run-completed event.
3. Add redaction, payload storage, access control and retention.
4. Choose best-effort publication or a transactional outbox.

### Phase 3: build replay and evaluation workers

1. Consume run events in a separate worker deployment.
2. Implement stored-context component replay and end-to-end replay.
3. Add immutable corpus/index snapshots or version today's-corpus replay.
4. Publish artifacts and scores to the evaluation registry.

### Phase 4: broaden evidence and risk coverage

1. Expand golden, regression, production, adversarial, edge, holdout and canary
   datasets.
2. Add claim, trajectory, ACL/security and operational scorers.
3. Calibrate LLM judges against human labels.
4. Add environment simulators and human-review workflows.

### Phase 5: continuous evaluation and operations

1. Sample production traffic using explicit risk-based policies.
2. Add dashboards and alerts for quality, security, latency, cost and queue lag.
3. Feed reviewed production failures into the regression suite.
4. Integrate Opik as the trace/experiment inspection layer while keeping Git and
   CI authoritative for datasets, scorer code, baselines and release policy.

## Intended end state

The existing harness should become a library used by several executors rather
than being replaced:

```text
FastAPI agent
    -> OpenTelemetry spans + versioned run-completed event
    -> queue/outbox
    -> evaluation workers
         -> deterministic and reference scorers from eval/harness
         -> calibrated LLM judges
         -> human-review queue
         -> immutable run artifacts and experiment registry
    -> CI/CD release decision
```

Opik is optional for correctness. If adopted, it should receive traces,
experiment metadata and scorer results for investigation and monitoring. Git
should remain authoritative for dataset definitions, scorer implementations,
gate policy and approved baselines.
