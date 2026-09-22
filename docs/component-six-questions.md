# Agent layer: six questions per component

Written 2026-09-21 from a read of `services/api/`, `services/sandbox/`, `pipelines/ingestion/graph/`, `eval/`, git history, `BACKLOG.md` and `.learnings/`.

The six questions, asked of every component:

1. Why does it exist?
2. What is its typed contract?
3. What can fail?
4. What deterministic control contains the failure?
5. How is quality measured?
6. What did I personally implement and observe?

## Reading question 6

Question 6 is answered from repo evidence only: git history, BACKLOG, `.learnings`. It cannot see what was observed at the keyboard, so each entry separates "built" from "observed" and marks gaps as **You fill in**.

- Most of the layer was scaffolded upstream by FareedKhan-dev on 2025-12-27. `supersom` commits start 2026-05-05.
- Commit `3aadbcb` carries a Claude co-author trailer. Commit metadata cannot show what was typed versus generated.
- DEVLOG has no agent-layer entries. Its entries are all EKS and infra, so the planner's design rationale lives only in commit messages.

## Policy layer: absent as a component

Nothing in the repo is named or structured as a policy layer. It is covered in its own section below, because the accurate description is "scattered guardrails, no unified policy layer".

---

## Graph wiring and `AgentState`

Files: `agents/graph.py`, `agents/state.py`

1. **Why:** it makes routing an explicit, inspectable graph edge rather than an LLM deciding inside a loop.
2. **Contract:** `AgentState` is a `TypedDict`. `messages` is append-only via `operator.add`. `action` is one of three strings. `route_after_planner` defaults to `retriever` for anything unrecognised.
3. **Fails:** a typo in `action` still routes, to retrieve. State keys are not validated at runtime, since `TypedDict` is compile-time only. There is no step budget, but the graph is a DAG, so there are no loops.
4. **Control:** the default edge to `retriever`, and the DAG shape, which makes runaway loops impossible.
5. **Quality:** not measured. There are no tests anywhere in the repo.
6. **You:** conditional routing and the `action`/`tool_name`/`tool_input` fields (`0a0c45f`, `c53c002`). Upstream wrote the graph itself.

## Planner

File: `agents/nodes/planner.py`

1. **Why:** it avoids a retrieval round-trip for "hi" and avoids the LLM guessing at arithmetic. It is a cheap intent classifier in front of the expensive path.
2. **Contract:** input is `messages[-1]` only. Output is `{action, tool_name, current_query, tool_input, plan}`. The LLM is asked for JSON with `action/tool_name/reasoning/refined_query`, temperature 0.0, `max_tokens=300`.
3. **Fails:**
   - Malformed or fenced JSON.
   - A wrong action enum value.
   - A hallucinated tool name.
   - A valid tool name paired with the wrong action.
   - The LLM being down. The fallback then reports `action=retrieve`, which hides the outage (`ERR-20260619-001`).
   - Coreference in multi-turn ("who maintains *it*?").
4. **Control:** `_extract_json` takes the last decodable JSON object. Four ordered safeguards coerce into a valid `(action, tool_name)` pair. `except Exception` degrades to `retrieve`. The tool-name check against `TOOL_REGISTRY` is deterministic.
5. **Quality:** measured. `eval/baselines/routing.json` (built 2026-09-22, model `nvidia/nemotron-3-ultra-550b-a55b:free` via OpenRouter, `--repeats 3`): pass rate / action accuracy 95.7% (66/69, 88.0–98.5% CI), tool accuracy 100% (37/37). The deterministic safeguards contribute 0.0 percentage points on this run (the raw, pre-coercion plan already matched); 3 items hit `raw_invalid_json`, all the same reproducible multi-intent query burning its token budget on reasoning before it can close the JSON object, not random noise. Stability across repeats was 64–75% run to run, driven entirely by the hosted model's variable rate-limiting, not the planner logic. `scripts/eval_pipeline.py`'s older 14-query HTTP check still exists as a full-stack smoke test but doesn't check tool success; the harness's `expected_tool_output` does, for calculator.
6. **You:** `3aadbcb`, `6985258` and `b41b6da` are the substance of this node. `b41b6da` says the tool-name safeguards were "observed in practice", and its token-cap rationale (small models pad around the JSON) is an observation. `ERR-20260619-001` is a logged incident. **You fill in:** how often each safeguard fired on `llama3.2:3b`, and the failure rate before and after.

**Verified problem.** The planner's few-shot example, the registry description and the demo plan all use `sqrt(144)` and `15% of 200` as calculator inputs. With simpleeval 0.9.13, `sqrt(144)` raises `FunctionNotDefined` and `15% of 200` raises `SyntaxError`. The planner routes these correctly and then the tool fails. The responder prompt says "Do not say you lack information", so the error string is presented as the answer.

## Retriever

File: `agents/nodes/retriever.py`

1. **Why:** vector search finds similar passages and the graph finds relationships. Running both concurrently costs one round-trip of latency.
2. **Contract:** input is `current_query`. Output is `documents: list[str]`. Vector hits are formatted `"text [Source: filename]"`. Graph hits are `"a REL b"` strings.
3. **Fails:**
   - The embed call or the Qdrant search raises. Neither is caught, so the stream ends in the generic error.
   - The graph arm is caught and returns `[]`, so a failure there is silent. The raw query goes into a Lucene fulltext index, so special characters may cause parse errors (unverified).
   - There is no score threshold, so top-5 comes back however irrelevant.
   - `set()` dedupe discards ordering, which contradicts the comment about prioritising graph results.
   - An empty collection yields an empty context.
4. **Control:** graph-arm isolation via `asyncio.gather` plus try/except. Grounding refusal is enforced only by the responder prompt, which is not deterministic.
5. **Quality:** measured. `eval/baselines/retrieval.json` (built 2026-09-22, 80-file corpus, 12,780 points): source-labelled hand-written queries hit@1/@3/@5 all 71.4% (5/7); `golden.json`'s 20 fuzzy items hit@5 only 30% (this corpus has one broad K8s overview plus 5 narrow files, so many golden questions likely have no real source here — a corpus gap, not necessarily a retrieval defect). Fuzzy-label calibration agreement 93.3% (reliable, ≥85% threshold). Unanswerable-vs-answerable top-1-score separation: AUROC 0.94 (descriptive only, n=10 unanswerable items, below the 20-item trust threshold). One of the two remaining source misses (`h002`) previously passed only because an oversized forced file's sheer chunk volume compensated for weak embedding relevance — after fixing a corpus-composition bug (one file was 40.5% of the collection), it failed honestly instead, exposing that `nomic-embed-text` is used without its required `search_query`/`search_document` prefixes (logged in BACKLOG, unfixed — needs a production change plus a full re-embed).
6. **You:** the `c53c002` fix for fragile metadata access. Upstream wrote the rest. The `ingest_local.py` and batch-ingest work that populates what it retrieves is the larger contribution.

## Tool node and registry

Files: `agents/nodes/tool.py`, `tools/registry.py`

1. **Why:** the registry is one table holding description (for the planner), handler and `cacheable`. Adding a tool is a single entry.
2. **Contract:** `handler(str) -> str`, sync or async. The result becomes `documents=[result]`. The node also appends a `role: user` "Tool Output" message that nothing reads.
3. **Fails:** an unknown tool (already prevented upstream by the planner). A handler raising. There is no timeout or step budget in the node.
4. **Control:** a per-tool convention. Every handler catches its own exceptions and returns an error string. The node has no try/except, so a new tool that forgets would crash the stream.
5. **Quality:** unmeasured. No per-tool success rate is recorded.
6. **You:** `0a0c45f` (registry, sync/async dispatch), and the `cacheable` flag plus `NO_ANSWER_PHRASES` (`372a73a`). **You fill in:** why `web_search` is the only non-cacheable tool.

### calculator

- **Contract:** an expression string of 100 characters or fewer returns `str(result)` or `"Error: ..."`.
- **Fails:** `sqrt` and percent forms (verified above). `10**10**10` is rejected with `NumberTooHigh`, which is correct.
- **Control:** simpleeval AST evaluation with no builtins, plus a length cap. The code comment "prevents ReDoS" is inaccurate, since the cap has nothing to do with regex.
- **You:** upstream.

### web_search

- **Contract:** Tavily, 10s timeout, top 3 results, formatted as text.
- **Fails:** a missing `TAVILY_API_KEY` returns "disabled", which the responder then presents as an answer. User queries go to a third party with no redaction policy.
- **Control:** the 10s timeout and non-cacheable status.
- **You:** upstream.

### graph_search

- **Contract:** the LLM extracts entities as JSON, then a parameterised Cypher fulltext query runs with `LIMIT 10`.
- **Fails:** bad JSON from the small model. `json_mode=True` is passed but `chat_completion` ignores it, so it is a no-op.
- **Control:** no LLM-generated Cypher, which structurally prevents injection. This is the best-designed tool in the layer.
- **You:** upstream.

### sandbox

- **Contract:** POST to `sandbox-service:8080` with `timeout=5`.
- **Fails:**
  - The service is not deployed anywhere, so every call fails.
  - The URL is hardcoded K8s DNS, which violates the env-var-endpoint principle.
  - It is `cacheable: True`, so failures can be cached.
  - `tool_input` is the planner's `refined_query`, so a 3B model rewrites the code before it is executed.
- **Control:** on the runner side, a process kill on timeout and an egress-deny NetworkPolicy. The runner execs with full `__builtins__` and never reads `limits.yaml`.
- **You:** upstream.

### Dead code

`vector_search.py` is not in the registry. The HyDE and query-rewriter enhancers are referenced nowhere. See BACKLOG.

## Responder

File: `agents/nodes/responder.py`

1. **Why:** it is the single synthesis point, with three action-specific prompts so social messages do not go through the "cite sources or refuse" prompt.
2. **Contract:** input is `current_query` and `documents`. It builds a single-message prompt (no system role, no history) at temperature 0.3, and returns one assistant message.
3. **Fails:**
   - The LLM call is uncaught, so an outage becomes "An internal error occurred."
   - The model ignores the citation format or fabricates a source.
   - It uses the planner's refined query rather than the user's text.
   - It answers from general knowledge despite the "only context" instruction.
4. **Control:** only the prompt. Nothing deterministic verifies that a cited `[Source: X]` exists in the retrieved set, or that the answer is grounded. `chat.py` post-filters caching with `NO_ANSWER_PHRASES`, but that is a substring match on the output.
5. **Quality:** unmeasured. `llm_judge.py` and RAGAS exist but are not wired to this pipeline.
6. **You:** `98c402e` and `6d020eb` (action-aware prompts). The missing error handling is a 2026-07-07 backlog finding. **You fill in:** what the actual incident looked like when it was traced.

## Memory (Postgres chat history)

File: `memory/postgres.py`

1. **Why:** it provides an audit trail and the intended multi-turn context.
2. **Contract:** `add_message(session_id, role, content, user_id)` and `get_history(session_id, limit)`, returning the last N in chronological order.
3. **Fails:**
   - Cold-start table race on Aurora (in backlog).
   - Cache-hit writes go through `BackgroundTasks` while the agent path awaits them.
   - `get_history` filters on `session_id` only, so any authenticated user who knows a session ID reads that session.
4. **Control:** `create_all` at startup. Nothing on ownership.
5. **Quality:** N/A.
6. **You:** the `chat_history` bootstrap and stale-image incident (DEVLOG 2026-06-18 "Stale API image caused missing chat_history table").

**Bigger finding.** History is loaded into `state["messages"]`, but the planner reads only the last message and the responder never sees history. Memory is effectively write-only. `memory/models.py` is a dead duplicate schema.

## Semantic cache

Files: `cache/semantic.py`, `routes/chat.py`

1. **Why:** it skips the whole agent path (planner, retrieval, generation) for repeated questions.
2. **Contract:** a cosine similarity of at least 0.95 on the raw user message returns the stored answer. It runs before the planner and before history is loaded.
3. **Fails:**
   - A similar-but-different question returns a wrong hit.
   - It is coreference-blind: "what about its cost?" can hit an entry from another conversation.
   - It is global with no `user_id`, and has no TTL or invalidation on re-ingest.
   - Dimension mismatch on the collection (`EMBED_DIM` 1024 vs 768 local). Qdrant dimensions are immutable.
   - Failed answers get cached (see sandbox above).
   - `ensure_collection()` runs on every lookup.
4. **Control:** the 0.95 threshold, the per-tool `cacheable` flag, and `NO_ANSWER_PHRASES`. Failures fail open to a normal agent run.
5. **Quality:** unmeasured. There is no hit rate or false-hit rate anywhere.
6. **You:** `372a73a` (auto-provisioning, smart caching). The 2026-09-19 investigation showed Redis is not involved at all, despite the docs and the roughly $95/month it costs (unverified pricing). **You fill in:** whether 0.95 was chosen empirically.

## Policy layer (does not exist as a component)

1. **Why it would exist:** to centralise "what may this user do, and what may this agent do".
2. **What actually exists:**
   - JWT: signature and expiry only. `role` and `permissions` are decoded and never enforced.
   - The planner's tool allowlist.
   - The calculator's length cap and AST evaluator.
   - `cacheable`.
   - The sandbox timeout and NetworkPolicy.
   - The prompt-level "refuse if not in context".
3. **One structural property worth naming:** the planner runs before retrieval and sees only the user's message. Retrieved document text can therefore never select a tool, so indirect prompt injection is limited to influencing answer text.
4. **Gaps:**
   - No rate limiting.
   - No output filter.
   - No PII policy on `web_search`.
   - No tool-permission check against JWT claims.
5. **Quality:** unmeasured.
6. **You:** the `NO_ANSWER_PHRASES` and `cacheable` controls.

## Other components

### LLM client

File: `clients/ray_llm.py` (upstream, adapted in `034f72a`)

- Retries on connect and timeout errors only: 3 tries with a 120s timeout, so a hung endpoint can block for about 6 minutes. 5xx responses are not retried.
- `json_mode` is unused.
- It is the only place in the layer with backoff.

### Embed client

File: `clients/ray_embed.py`

- It builds a new `AsyncClient` per call, with no retry and the default 5s timeout.
- A cache miss embeds the same text up to three times (lookup, retrieval, write).

### Chat route

File: `routes/chat.py`

- Its NDJSON event stream (`routing`, `status`, `answer`, `error`) is the only observability surface.
- Any exception collapses to "An internal error occurred."
- `2fc67eb` added the `routing` event.

### Graph extractor

File: `pipelines/ingestion/graph/extractor.py` (upstream)

- It runs at ingestion time and its failure mode is silent. Each failed chunk is caught and yields empty nodes and edges, so graph coverage degrades quietly.
- The `--graph-only` and batch-retry scripts are the containment.

### Eval harness

Files: `eval/` and `scripts/eval_pipeline.py`. **The `eval/` directory is not measuring this system.**

- `scripts/eval_pipeline.py` (2026-05-07) is a separate HTTP black-box script. It runs 14 hand-written queries through `/api/v1/chat/stream` with a minted JWT and purges them from the semantic cache first. It scores routing *action* accuracy and prints latency. It does not check tool choice or tool success (`sqrt(256)` counts as a pass even though the calculator cannot evaluate it), has no retrieval metric, and needs the API running.

- `eval/reports/latest.html` is upstream's placeholder. Its 0.98 faithfulness is not a measurement.
- `ragas/run.py` scores three hard-coded samples and needs OpenAI.
- `llm_judge.py` maps judge errors to score 0.
- The 20-question `golden.json` is never run through `agent_app`.

---

## Summary

The layer's real strength is that safe degradation is in the code, not the prompt. Planner coercion, the parameterised graph tool and the `cacheable` gating are the deterministic controls. The layer has no way to tell whether those controls work. That is question 5 across every component.

New off-task findings from this review are parked in `BACKLOG.md` under "Agent layer — found during 2026-09-21 six-question component review".
