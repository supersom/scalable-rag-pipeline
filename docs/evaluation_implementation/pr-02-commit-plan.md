# PR 2 Reviewable Commit Plan

> **For agentic workers:** Use `superpowers:executing-plans` to carry out this plan inline. This plan was approved and executed; it packages already implemented code.

**Goal:** Split PR 2 into six cohesive commits, each with its implementation and tests, without intentionally red intermediate commits.

**Architecture:** Commit the shared span helper first, followed by model boundaries, retrieval/backend outcomes, graph/tool behavior, complete request orchestration, and documentation. Regroup the currently mixed test files by functionality; preserve test bodies and coverage.

**Tech stack:** Python, FastAPI, LangGraph, OpenTelemetry, HTTPX, Qdrant, Neo4j, pytest, Docker Compose.

**Spec:** [Gap 1 implementation, PR 2](gap-01-production-run-instrumentation.md#pr-2-domain-spans-and-typed-dependency-results).

## Constraints and verification

- Starting commit: `7536cd3` on `dev`.
- Do not amend or rewrite the four PR 1 commits.
- Pair functionality with its tests; no tests-only red commits.
- Do not change production behavior while packaging. Retain string LLM compatibility, best-effort fallbacks, safe attributes, and 5/60-second query/document embedding timeouts.
- Keep collector retention, durable publication, and deployment controls out of scope: they belong to PRs 3–5.
- Leave the unrelated untracked walkthrough, Helm, Kubernetes, and service-convention documents untouched.
- Do not push or deploy to the cloud.

**Review focus:** unsafe model-provided tool names; missing provider usage; exhausted retries; cancellation of parallel retrieval; pooled versus temporary embedding timeouts. Existing regression tests below cover all five.

The current worktree contains *future* commits' code and tests. Passing tests there does not prove an intermediate commit is green. After staging each commit, export the index into a fresh temporary directory and test that snapshot:

```bash
pr2_check_dir=$(mktemp -d /tmp/pr2-commit-check.XXXXXX)
git checkout-index --all --prefix="$pr2_check_dir/"
(cd "$pr2_check_dir" && pytest -q --tb=short)
git diff --cached --check
git diff --cached --stat
```

Expected: all collected tests pass; the existing opt-in live checks remain skipped. Stage only the files listed for the current commit. If a snapshot fails, correct the boundary before committing; do not rely on unstaged future changes.

## 1. Safe domain-span primitives

**Message:** `feat(observability): add safe domain span lifecycle`

Files:

- `libs/observability/tracing.py`
- `tests/observability/conftest.py`
- `tests/observability/test_domain_spans.py` — retain only `test_domain_span_records_safe_failure_and_propagates_original` here.

Includes latency, explicit failure/timeout/cancellation classification, sanitized exception attributes, context cleanup, and shared owned-provider/embedded-Qdrant fixture isolation.

Produces `domain_span()`, `mark_failure()`, and `failure_status()` for later commits. No model, retrieval, tool, or route instrumentation belongs here.

- [x] Regroup the mixed tests as described in commits 2–4; keep their bodies unchanged and their destination files unstaged until their owning commit.
- [x] Stage the three files above.
- [x] Verify the staged snapshot; focused command: `pytest tests/observability/test_domain_spans.py -q`.
- [x] Inspect the staged diff and commit with the message above.

## 2. Model metadata and embedding boundary

**Message:** `feat(models): preserve provider metadata and trace model calls`

Files:

- `services/api/app/clients/ray_llm.py`
- `services/api/app/clients/ray_embed.py`
- `tests/observability/test_model_metadata.py` — create by moving these tests from `test_dependency_metadata.py`:
  - `test_typed_llm_result_preserves_provider_metadata_and_string_wrapper`
  - `test_missing_provider_usage_is_unknown_not_estimated`
  - `test_malformed_provider_response_is_explicit_and_safe`
  - `test_transient_model_retries_are_bounded_and_observable`
  - `test_malformed_embedding_response_fails_safely`
  - `test_query_and_document_timeouts_are_preserved_with_or_without_pool`

Consumes commit 1's helper. Produces `LLMResult`, `chat_completion_result()`, the compatible text wrapper, model/embedding spans, retry counts, and pooled embedding HTTP calls with unchanged per-operation timeouts. Missing usage is not estimated.

- [x] Stage these implementation files and the regrouped model tests.
- [x] Verify the staged snapshot; focused command: `pytest tests/observability/test_model_metadata.py -q`.
- [x] Inspect the staged diff and commit with the message above.

## 3. Ranked retrieval and backend outcomes

**Message:** `feat(retrieval): preserve ranked evidence and backend outcomes`

Files:

- `services/api/app/agents/nodes/retriever.py`
- `services/api/app/clients/qdrant.py`
- `services/api/app/clients/neo4j.py`
- `services/api/app/cache/semantic.py`
- `tests/observability/test_dependency_metadata.py` — retain only:
  - `test_retrieval_preserves_rank_identity_score_and_deterministic_dedup`
  - `test_graph_empty_and_failed_search_have_distinct_outcomes`
- `tests/observability/test_retrieval_lifecycle.py` — move `test_retrieval_failure_cancels_sibling_before_ending_parent_span` from `test_domain_spans.py`.
- `tests/observability/test_cache_tracing.py` — move `test_cache_failure_remains_best_effort_but_distinct_from_miss` from `test_domain_spans.py`.

Consumes commits 1–2. Produces normalized retrieval records, deterministic ordering/deduplication, source ranks and best scores, graph partial-failure status, and sibling cancellation before parent-span completion. Includes Qdrant/Neo4j spans and both Qdrant SDK APIs. The semantic cache uses that same search wrapper and records handled backend failures instead of presenting them as ordinary misses; route-level cache spans arrive in commit 5.

- [x] Stage these files and regrouped tests.
- [x] Verify the staged snapshot; focused command: `pytest tests/observability/test_dependency_metadata.py tests/observability/test_retrieval_lifecycle.py tests/observability/test_cache_tracing.py -q`.
- [x] Inspect the staged diff and commit with the message above.

## 4. Graph-node and tool instrumentation

**Message:** `feat(agent): trace planning generation and tool outcomes`

Files:

- `services/api/app/agents/nodes/planner.py`
- `services/api/app/agents/nodes/responder.py`
- `services/api/app/agents/nodes/tool.py`
- `services/api/app/tools/calculator.py`
- `services/api/app/tools/graph_search.py`
- `services/api/app/tools/sandbox.py`
- `services/api/app/tools/vector_search.py`
- `services/api/app/tools/web_search.py`
- `tests/observability/test_agent_tool_spans.py` — move these tests from `test_domain_spans.py`:
  - `test_real_calculator_failure_is_not_a_successful_tool_span`
  - `test_disabled_web_search_is_explicit_not_success`
  - `test_external_tool_failure_marks_tool_span_without_payloads`
  - `test_graph_tool_extracts_entities_and_runs_parameterized_query`
  - `test_planner_does_not_export_unknown_model_tool_name`

Consumes the typed model API and safe span helper. Includes planner/responder/tool spans, allowlisted tool metadata, typed model calls in nodes, explicit handled tool failures, and the graph tool's JSON prompt-formatting regression fix.

- [x] Stage the node/tool files and regrouped tests.
- [x] Verify the staged snapshot; focused command: `pytest tests/observability/test_agent_tool_spans.py -q`.
- [x] Inspect the staged diff and commit with the message above.

## 5. Request orchestration and complete span-tree acceptance

**Message:** `feat(api): trace request dependencies and verify domain span trees`

Files:

- `services/api/app/routes/chat.py`
- `tests/observability/test_chat_tracing.py`
- `tests/observability/test_domain_integration.py`

Consumes commits 1–4. Adds cache lookup/write and memory load/persist boundaries, including skip reasons. Updates PR 1 assertions to distinguish the root run from its new children. Real-graph acceptance tests cover direct answers, retrieval, cache hits, tools, parentage, provider metadata, and privacy.

- [x] Stage these three files.
- [x] Verify the staged snapshot; focused command: `pytest tests/observability -q`.
- [x] Rebuild the isolated API: `docker compose -p scalable-rag-eval --profile eval-local --env-file .env.example up -d --build --wait api-local`.
- [x] Run `make local-check` and `make local-smoke`.
- [x] Run focused tests against pinned dependencies in a disposable API-image container, mounting the staged snapshot read-only:

  ```bash
  docker run --rm --entrypoint sh \
    -v "$pr2_check_dir:/repo:ro" -w /repo scalable-rag-eval-api-local \
    -c 'pip install -q pytest==8.3.3 && python -m pytest tests/observability -q -p no:cacheprovider --tb=short'
  ```

- [x] Inspect the staged diff and commit with the message above.

## 6. Progress, usage, and commit-plan documentation

**Message:** `docs(evaluation): document domain instrumentation progress`

Files:

- `docs/evaluation_implementation/gap-01-production-run-instrumentation.md`
- `docs/evaluation_implementation/local-environment.md`
- `docs/evaluation_implementation/pr-02-commit-plan.md`

- [x] Update PR 2's status from uncommitted to committed, and record the five implementation commit hashes.
- [x] Update test commands where regrouping changed filenames. Preserve the recorded verification results unless a fresh run changes them.
- [x] Check off the completed steps in this plan.
- [x] Stage only these documentation files, run `git diff --cached --check`, inspect the diff, and commit.

## Final acceptance

- [x] Full suite: `pytest -q --tb=short` — previous result: **229 passed, 2 existing skips**.
- [x] Focused suite: `pytest tests/observability -q` — previous result: **71 passed**, including inside the pinned API image.
- [x] Local checks: **18 passed, 2 existing skips**; local smoke: **4 passed**.
- [x] Confirm all PR 2 changes are accounted for, and only unrelated user files remain uncommitted.
- [x] Report the six hashes and their responsibilities; do not push.

## Execution results

Completed on 2026-09-27. Implementation commits:

| Commit | Responsibility | Independent full-suite snapshot |
|---|---|---|
| `7c4ebbb` | Safe domain-span lifecycle | 199 passed, 2 existing skips |
| `8d0a2f0` | Model metadata and embeddings | 213 passed, 2 existing skips |
| `856b6c7` | Ranked retrieval/backend outcomes | 218 passed, 2 existing skips |
| `033a0b0` | Agent/tool instrumentation | 226 passed, 2 existing skips |
| `0e49865` | Request orchestration/acceptance | 229 passed, 2 existing skips |

The complete staged snapshot also passed 71 focused tests inside the pinned API image. Local checks passed 18 tests with two opt-in skips; the live smoke suite passed four tests. The sixth commit records these results and this completed plan. Nothing was pushed or deployed to the cloud.
