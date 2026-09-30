# PR 3 Reviewable Commit Plan

> **For agentic workers:** Use `superpowers:executing-plans` to carry out this plan inline. This approved plan has been executed. It packaged existing implementation; it did not authorize new functionality, a push, or a deployment. Steps use checkboxes for tracking.

**Goal:** Package the uncommitted PR 3 changes into seven cohesive, independently green commits, each pairing functionality with its owning tests.

**Architecture:** Introduce the evidence contract, collector, and sampling-independent observation bridge together. Add configuration identity and individual evidence producers next; wire the complete request lifecycle only after those producers exist. Finish with progress and usage documentation.

**Tech stack:** Python, Pydantic, FastAPI, LangGraph, OpenTelemetry, HTTPX, pytest, Docker Compose.

**Spec:** [Gap 1 implementation, PR 3](gap-01-production-run-instrumentation.md#pr-3-evidence-contract-and-collector).

## Global constraints

- Starting commit: `8818d20` on `dev`. Do not amend or rewrite the six PR 2 commits.
- Preserve the current implementation's behavior; this is packaging and mechanical test regrouping only.
- Pair implementation and tests in every functionality commit. No intentionally red commits.
- Keep evidence out of `AgentState` and graph checkpoints; preserve string model-client compatibility and existing routing/fallback behavior.
- Records remain metadata-only and in memory. Do not add response payload fields, logging, storage, a publisher, scoring, or cloud controls.
- Leave the unrelated untracked `docs/code-walkthrough.md`, `docs/helm-charts-notes.md`, `docs/kubernetes-notes.md`, and `docs/kubernetes-service-conventions.md` untouched.
- Stage explicit files only; do not use `git add .` or stage the entire evaluation directory before its owning commit.
- Do not push, merge, deploy, or modify dependency pins.

## Review focus

- Sampled-out spans must still retain safe failure and outcome evidence — commit 1, unsampled observation test; commit 5, SDK attribute mirroring test.
- Missing usage must stay unknown, and exhausted retries must preserve the provider failure — commits 1 and 3, existing usage/retry tests.
- A completed retrieval branch must survive sibling failure without leaving work running — commit 4, failed-vector/completed-graph and existing cancellation tests.
- Header/body disconnects must finalize evidence once, and concurrent requests must not share collector context — commit 6, lifecycle tests.
- Fingerprints must track configured build identity without inventing unavailable versions, and records must exclude raw payloads — commits 1–2, fixture/privacy/fingerprint tests; commit 6, real-graph privacy assertions.

These cases already have tests. Preserve their bodies, decorators, and assertions when moving them; no new behavior is needed to execute this plan.

## Staged-snapshot verification

The worktree contains future commits' code and tests, so a green worktree suite does not prove an intermediate commit is green. After staging each commit, export the index and test the exported snapshot:

```bash
pr3_check_dir=$(mktemp -d /tmp/pr3-commit-check.XXXXXX)
git checkout-index --all --prefix="$pr3_check_dir/"
(cd "$pr3_check_dir" && pytest -q --tb=short)
git diff --cached --check
git diff --cached --stat
```

Expected: all collected tests pass, with only the two existing opt-in live checks skipped. The focused commands below must also run from that snapshot, not the original checkout. Do not commit if the snapshot fails; correct the boundary while preserving behavior.

Previously verified complete PR 3 tree: **245 passed, 2 skips**, with **87 observability tests**. These are reference counts, not a substitute for fresh verification. Prior PR 2 baseline: **229 passed, 2 skips**, with **71 observability tests**.

## Test regrouping map

Move the following functions out of `tests/observability/test_evidence_lifecycle.py`. Copy only the imports each destination needs; keep helper imports inside the functions where they already live. Do not introduce cross-test-module imports.

| Existing function | Destination | Owning commit |
|---|---|---|
| `test_unsampled_dependencies_keep_failure_and_outcome_evidence` | `tests/observability/test_evidence_observations.py` | 1 |
| `test_build_revision_is_part_of_configuration_fingerprint` | `tests/observability/test_evidence_configuration.py` | 2 |
| `test_completed_graph_evidence_survives_vector_failure` | `tests/observability/test_retrieval_evidence.py` | 4 |
| `test_sdk_attribute_updates_survive_unsampled_operations` (both parameter cases) | `tests/observability/test_agent_evidence.py` | 5 |

Leave these in `test_evidence_lifecycle.py` for commit 6:

- `test_terminal_evidence_covers_cache_and_dependency_failures` — three parameter cases.
- `test_disconnect_finalizes_evidence_before_loop_shutdown` — two parameter cases.
- `test_concurrent_runs_do_not_leak_collector_context` — one case.

The regrouping must retain **87 focused cases** in the complete tree.

## 1. Evidence contract, collector, and observation bridge

**Message:** `feat(evaluation): add versioned run evidence collector`

Files:

- `services/api/app/evaluation/__init__.py`
- `services/api/app/evaluation/models.py`
- `services/api/app/evaluation/context.py`
- `libs/observability/tracing.py`
- `tests/observability/test_evidence_contract.py`
- `tests/observability/fixtures/run-evidence-v1.json`
- `tests/observability/test_evidence_observations.py` — extracted as mapped above.

**Interfaces:** Produces the schema `1.0` models, `RunEvidenceCollector`, `content_hash(value)`, `current_collector`, `model_purpose`, `bind_collector(collector, purpose=None)`, and `collector_from_config(config)`. The tracing bridge supplies `operation_observer`, `ObservedSpan`, and `set_span_attribute(span, key, value)`.

Keep the collector and tracing bridge together: `bind_collector()` imports `operation_observer`, and its unsampled test requires the metadata mirror. Splitting these would leave a broken binding interface or require temporary scaffolding.

- [x] Mechanically regroup the mixed lifecycle tests using the map; leave later commits' files unstaged.
- [x] Stage only the seven files above; leave `evaluation/configuration.py` unstaged.
- [x] Verify the staged snapshot and run `pytest tests/observability/test_evidence_contract.py tests/observability/test_evidence_observations.py -q` inside it.
- [x] Expect 6 focused cases; full suite reference count: 235 passed, 2 skips.
- [x] Inspect the staged diff and commit with the message above.

## 2. Runtime configuration and build identity

**Message:** `feat(evaluation): fingerprint runtime configuration`

Files:

- `services/api/app/evaluation/configuration.py`
- `services/api/app/config.py`
- `tests/observability/test_evidence_configuration.py` — extracted build-revision test.

**Interfaces:** Consumes `ConfigurationFingerprint` and `content_hash()` from commit 1. Produces `configuration_fingerprint()` and settings `AGENT_BUILD_SHA` and `EVAL_IDENTITY_HASH_KEY`, both optional.

Own the complete four-line settings addition here rather than splitting one hunk across commits. The collector already accepts a key as an argument; these settings prepare the request integration without activating it. Fingerprints retain known model, graph, prompt, registry, and configured build identity; missing versions stay `None`.

- [x] Stage the three files above.
- [x] Verify the staged snapshot and run `pytest tests/observability/test_evidence_configuration.py -q` inside it.
- [x] Expect 1 focused case; full suite reference count: 236 passed, 2 skips.
- [x] Inspect the staged diff and commit with the message above.

## 3. Provider model-call evidence

**Message:** `feat(models): collect model call evidence and retries`

Files:

- `services/api/app/clients/ray_llm.py`
- `tests/observability/test_model_metadata.py`

**Interfaces:** Consumes commit 1's active collector, purpose context, hashing, `ModelCall`, and mirrored span attributes. Records one logical model call across retries, including usage, provider identifiers, latency, and sanitized terminal failures. The existing text wrapper remains unchanged for callers.

Own the existing retry-test amendments here; planner/responder purpose binding belongs to commit 5. Calls without a bound collector continue to work, and calls with no node purpose use the existing `unspecified` default.

- [x] Stage these two files.
- [x] Verify the staged snapshot and run `pytest tests/observability/test_model_metadata.py -q` inside it.
- [x] Expect 14 focused cases; full suite reference count remains 236 passed, 2 skips because these amend existing tests.
- [x] Inspect the staged diff and commit with the message above.

## 4. Ranked retrieval evidence and failed-branch retention

**Message:** `feat(retrieval): retain ranked evidence across branch failures`

Files:

- `services/api/app/agents/nodes/retriever.py`
- `tests/observability/test_retrieval_evidence.py` — extracted completed-graph/failed-vector test.

**Interfaces:** Consumes commit 1's collector binding and `RetrievalEvidence`/`RetrievedEvidence` models. Records ranked identities, scores, hashes, source outcomes, and completed sibling results before prompt flattening. `retrieve_node(state, config=None)` still returns prompt-ready document strings.

- [x] Stage these two files.
- [x] Verify the staged snapshot and run `pytest tests/observability/test_retrieval_evidence.py tests/observability/test_dependency_metadata.py tests/observability/test_retrieval_lifecycle.py -q` inside it.
- [x] Expect 5 focused cases; full suite reference count: 237 passed, 2 skips.
- [x] Inspect the staged diff and commit with the message above.

## 5. Graph-node context and tool trajectory

**Message:** `feat(agent): collect node purposes and tool trajectory`

Files:

- `services/api/app/agents/nodes/planner.py`
- `services/api/app/agents/nodes/responder.py`
- `services/api/app/agents/nodes/tool.py`
- `services/api/app/tools/sandbox.py`
- `services/api/app/tools/web_search.py`
- `tests/observability/test_agent_evidence.py` — extracted two-case unsampled SDK attribute test.

**Interfaces:** Consumes collector context from commit 1 and the model-call boundary from commit 3. Planner/responder/tool nodes accept optional `RunnableConfig`, bind the collector and call purpose, and retain tool hashes, timings, registration, and outcomes. Handled fallback, disabled-tool, and HTTP-status attributes use the metadata mirror.

- [x] Stage the six files above.
- [x] Verify the staged snapshot and run `pytest tests/observability/test_agent_evidence.py tests/observability/test_agent_tool_spans.py -q` inside it.
- [x] Expect 10 focused cases; full suite reference count: 239 passed, 2 skips.
- [x] Inspect the staged diff and commit with the message above.

## 6. Request lifecycle and complete evidence acceptance

**Message:** `feat(api): finalize request evidence and verify real graph runs`

Files:

- `services/api/app/routes/chat.py`
- `tests/observability/test_evidence_lifecycle.py` — only the three retained functions, six parameter cases.
- `tests/observability/test_domain_integration.py`

**Interfaces:** Consumes all earlier evidence producers and `configuration_fingerprint()`. Creates one collector per request, passes it through LangGraph configuration, records output hashes and first-answer timing, and finalizes on success, error, timeout, or disconnect. Exposes a detached terminal record through the server-side `RunStreamingResponse.evidence` property, not the HTTP body.

Keep the real-graph acceptance amendments here: they require the route to create the collector and all earlier producers to populate it. No standalone acceptance commit is needed to leave intermediate tests red or temporarily omit assertions.

- [x] Stage these three files.
- [x] Verify the staged snapshot; run `pytest tests/observability -q --tb=short` inside it.
- [x] Expect 87 focused cases; full suite reference count: 245 passed, 2 skips.
- [x] Rebuild the local API with `docker compose -p scalable-rag-eval --profile eval-local --env-file .env.evidence up -d --build --wait api-local`.
- [x] Run `make local-check` and `make local-smoke`; expect 18 passed/2 opt-in skips and 4 passed respectively.
- [x] Run the focused suite against pinned API dependencies using the staged snapshot:

  ```bash
  docker run --rm --entrypoint sh \
    -v "$pr3_check_dir:/repo:ro" -w /repo scalable-rag-eval-api-local \
    -c 'pip install -q pytest==8.3.3 && python -m pytest tests/observability -q -p no:cacheprovider --tb=short'
  ```

  Expected: 87 passed. The image rebuild does not prove an earlier snapshot green; the read-only snapshot mount supplies the code under test.

- [x] Inspect the staged diff and commit with the message above.

## 7. Progress, usage, and commit-plan documentation

**Message:** `docs(evaluation): document run evidence collection progress`

Files:

- `docs/evaluation_implementation/gap-01-production-run-instrumentation.md`
- `docs/evaluation_implementation/local-environment.md`
- `docs/evaluation_implementation/pr-03-commit-plan.md`

- [x] Update the progress document's file-accountability table to reflect the regrouped test files.
- [x] Change PR 3 status from awaiting commit to committed after commits 1–6 exist; record their actual hashes and fresh verification results in this plan.
- [x] Check off completed steps. Do not claim PR 4/5, durable delivery, full-payload replay, or production guarantees are complete.
- [x] Stage only the three documentation files; inspect links and `git diff --cached --check`.
- [x] Verify the staged snapshot remains green and commit with the message above. Mark the documentation commit by message, not a self-referential hash.
- [x] Confirm `git status --short` contains only the four unrelated untracked documents and the newly requested, uncommitted PR 4 planning draft. Leave the branch and local services in place; do not push.

## Coverage and handoff

Every current PR 3 implementation file and test amendment is assigned exactly once above. The new observation/configuration/retrieval/agent test files are mechanical relocations, not added test cases. Expected complete-suite totals remain unchanged.

This packaging plan changed no production behavior. Test regrouping preserved all seven function bodies and decorators; the complete focused suite remains 87 cases. The next-task planning draft is separate from these approved commits and remains uncommitted for review.

## Completed commits and verification

| Commit | Module | Focused check | Staged full suite |
|---|---|---|---|
| `8feac4f` | Contract, collector, observation bridge | 6 passed | 235 passed, 2 skips |
| `7af5725` | Configuration fingerprint | 1 passed | 236 passed, 2 skips |
| `37a48a0` | Model-call evidence | 14 passed | 236 passed, 2 skips |
| `0a592fb` | Retrieval evidence | 5 passed | 237 passed, 2 skips |
| `5db187a` | Agent/tool evidence | 10 passed | 239 passed, 2 skips |
| `be08317` | Request lifecycle and integration | 87 passed | 245 passed, 2 skips |
| `docs(evaluation): document run evidence collection progress` | Documentation and this plan | Whitespace/link inspection | 245 passed, 2 skips |

Final implementation snapshot: pinned API-container observability tests **87 passed**; `make local-check` **18 passed, 2 opt-in skips**; `make local-smoke` **4 passed**; rebuilt stack **8 healthy services**. Existing deprecation warnings are non-failing. No push or cloud deployment.
