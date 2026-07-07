# Porting the agentic blocks to ~/code/agentic_search

Plan for reusing this repo's agent layer to implement the agentic product search
specified in `~/code/agentic_search/docs/agentic-search-design.md` (referred to below
as "the design"). Reference inventories: this repo's
[`system-overview.md`](system-overview.md) and agentic_search's
`docs/system-overview.md`.

## Fit assessment

The two systems align unusually well. The design specifies **planner → tool-using
retriever → responder**; this repo already runs **planner → {tool | retriever} →
responder** in production-shaped code, with the exact safeguards, registry pattern,
trace streaming, and caching policy the design will need. Three of the design's ten
tools (`calculator`, `web_search`, and the entity-extraction pattern behind
`graph_search`) have working implementations here.

The one honest gap: this repo's agent is **single-hop** (planner picks one action,
one tool call, respond), while the design requires a **bounded multi-step tool loop**
with an evidence list. The loop is new work; everything around the loop ports.

### The design already anticipates the differences

| Design decision (agentic_search) | This repo | Consequence for the port |
|---|---|---|
| Anthropic tool-use API, Haiku/Sonnet | prompt-engineered JSON + regex extraction for a 3B local model | Port the *safeguard philosophy*, drop the JSON-scraping mechanics — Claude's native tool use and forced-tool structured output replace `_extract_json` and most coercion rules |
| No graph database (npz adjacency) | Neo4j + Cypher | Port the two-step *pattern* (extract entities → parameterized lookup), not the client |
| Plan is a rich typed contract (intent, sub_goals, constraints, budget, shortcut) | Plan is `{action, tool_name, refined_query}` | The planner prompt/validation structure ports; the schema grows |
| `shortcut: true` bypasses the agent | `direct_answer` bypasses retrieval | Same idea, same routing seam — direct reuse |
| Orchestrator as a Ray Serve deployment | FastAPI service | Port node functions, not the FastAPI wrapper |

## Architecture recommendation: LangGraph for the outer graph, Anthropic tool loop inside the retriever node

Two options for the orchestrator skeleton:

- **(a) LangGraph** — port this repo's graph structure: nodes for planner /
  retriever-agent / responder / execute-final-queries, conditional edges for the
  shortcut and fallback paths.
- **(b) Plain-Python orchestrator** — planner call → bounded `while` loop over
  Anthropic tool-use turns → responder call, hand-rolled.

**Recommendation: (a), with one hard constraint — the retriever's tool loop lives
*inside* a single node** as a native Anthropic tool-use loop. Individual tool calls
are not graph hops; LangGraph orchestrates only the outer roles and fallbacks.

Reasoning (revised — an earlier draft recommended (b) on a "the control flow is just
one conditional plus one loop" argument, which undersold the design):

1. **The design's control flow is a real state machine.** Beyond the shortcut it
   specifies degrade-to-plain-`/search` on the wall-clock cap
   (`"mode": "degraded_agentic"`), Haiku→Sonnet escalation after a failed retriever
   loop, and a post-responder execution/RRF step. Conditional edges express those
   fallback transitions more legibly than nested try/except-with-flags.
2. **The trace comes free and matches the v2 plan.** `astream` emits an event per
   node; this repo's `chat.py` already converts exactly that into a client-facing
   NDJSON stream (port item #8). The design's v1 "return the whole trace" and v2 "SSE
   streaming" both fall out of one event loop instead of hand-rolled emission.
3. **It's the tested code.** `graph.py`'s routing/state-merge behavior has production
   miles in this repo; porting it wholesale shrinks the new-code surface to the tool
   loop itself. And one developer maintains both repos — one orchestration idiom.

The cost, stated honestly: `langgraph` (plus `langchain-core`) enters agentic_search's
deliberately lean dependency set and the ray-serve image. That's the one condition
under which (b) still wins — if keeping the serve image dependency-minimal outranks
the reuse. It's a library, not a service; no ops surface.

What ports regardless of (a)/(b): the **role separation into modules**
(`planner.py` / `retriever.py` / `responder.py`), the **typed state object**
(`AgentState` → the design's Plan/Evidence/Response contracts), the **registry**, the
**tool dispatch node**, and the **trace event emission**. What stays out regardless:
the JSON-scraping between nodes (see risks) — that was for a 3B model, not a property
of the graph.

---

## Component-by-component port map

Target layout is the design's `src/agent/` package in agentic_search.

| # | Source (this repo) | Target (agentic_search) | Port type | Notes |
|---|---|---|---|---|
| 1 | `app/agents/nodes/planner.py` | `src/agent/planner.py` | adapt | Keep: registry-driven prompt assembly, temperature-0 single call, validation-with-fallback (invalid intent → safe default, unknown tool → drop from budget), planner-as-query-rewriter (`refined_query` → `translated_query`). Replace: JSON scraping with a forced tool call returning the Plan schema. The `direct_answer` route becomes `shortcut: true`. |
| 2 | `app/tools/registry.py` | `src/agent/tools/registry.py` | adapt | The `{description, handler, cacheable}` dict pattern, extended per the design: `timeout_s`, `trust` (internal/external), `allowed_by_default`. Registry stays the single source for both the planner prompt (tool budget) and the Anthropic `tools=[...]` schema list. |
| 3 | `app/tools/calculator.py` | `src/agent/tools/calculator.py` | near drop-in | The design asks for "sandboxed ast-based evaluator (no eval)" — `simpleeval` + length cap is exactly that. Either take the `simpleeval` dep or inline an ast walker; keep the error-as-string contract. |
| 4 | `app/tools/web_search.py` | `src/agent/tools/web_search.py` | adapt | Working Tavily implementation (top-3, snippets-only — matching the design's "no full-page fetch"). The design prefers Brave with Tavily as fallback: put the existing Tavily code behind the provider interface on day one and add Brave when the key exists. Keep `cacheable: false` semantics. |
| 5 | `app/tools/graph_search.py` | `src/agent/tools/graph_search.py` | pattern only | The injection-safety pattern — LLM extracts *entities*, code owns the *query* — transfers verbatim to the product graph: extract product references/relations, look them up in the npz adjacency structure. Drop Neo4j/Cypher entirely per the design. |
| 6 | `app/agents/nodes/tool.py` | `src/agent/executor.py` | adapt | Generic dispatch (sync/async handler support, unknown-tool guard, result-as-evidence). Extend: per-tool timeout, evidence-item construction (`step`, `tool`, `input`, `output_summary`, `trust`), budget decrement. |
| 7 | `app/agents/nodes/responder.py` | `src/agent/responder.py` | adapt | Action-specific prompt structure and "answer only from provided context" discipline port; output grows from plain text to the design's `{final_queries, answer, citations}` contract (again via forced tool call). |
| 8 | `routes/chat.py` event stream | orchestrator trace emission | pattern | The NDJSON `routing`/`status`/`answer` stream is the design's trace, already shaped for a UI. v1 per the design returns the complete trace; keep the event structure so v2 SSE is a transport change, not a redesign. |
| 9 | `NO_ANSWER_PHRASES` + per-tool `cacheable` + failure-suppression in `chat.py` | caching/trace policy | pattern | The design has no cache; this is a cheap, proven cost lever. Mark optional (phase 5): cache `{query → final_queries + answer}` keyed on query embedding — agentic_search already has an encoder and FAISS in-process, so no new infra. |
| 10 | `app/agents/state.py` | `src/agent/contracts.py` | rewrite | TypedDict state → Pydantic models for Plan / EvidenceItem / Response exactly as specified in the design's message contracts. The *idea* (typed state between roles) is the port. |
| 11 | `eval/judges/llm_judge.py` | `eval/agentic.py` judge | adapt | 1–5 grading with reasoning, JSON out — the seed for the design's LLM-judged agentic eval set; add the trace as judge input. |
| 12 | `docs/agentic-demo-plan.md` routing eval | `eval/agentic.py` planner metrics | pattern | Expected-vs-actual routing accuracy transfers directly to planner intent accuracy (the design explicitly reuses the spike's expected/actual framing). |
| 13 | `app/agents/graph.py` + `chat.py` `astream` loop | `src/agent/graph.py` + orchestrator entry | near drop-in | The StateGraph skeleton and conditional-edge routing port almost verbatim; node set becomes planner / retriever_loop / responder / execute_queries, with edges for shortcut, degrade, and escalation. The `astream`-to-event-stream loop is the trace transport. |

**Do not port:** Qdrant/Neo4j/Postgres/Redis clients, JWT auth (agentic_search uses
`X-API-Key`), FastAPI service scaffolding (orchestrator mounts in the existing
Ray Serve app per the design), the semantic-cache Qdrant implementation (re-back it
on FAISS if #9 is adopted), and HyDE/query rewriter (unwired here; the design's
responder owns query rewriting).

---

## Execution plan, aligned to the design's phases

The design's phased plan (0–5) stands; this maps the ports into it.

**Phase 0 — fixes and skeleton** (design's prerequisite list)
- Create `src/agent/` with `contracts.py` (#10), `tools/registry.py` (#2), and empty
  role modules. Port calculator (#3) immediately — zero-dependency, gives the registry
  its first real entry and a unit-test template (this repo's tool tests are also
  portable shape-wise).
- The design's own items (1024-dim fix, projection param, secrets) are unaffected by
  the port.

**Phase 1 — planner + shortcut**
- Port the graph skeleton (#13) with just planner → {shortcut, respond} wired, then #1.
  Validation rules to carry over explicitly: unknown intent → `plain`
  (shortcut), hallucinated tool in `tool_budget.allowed` → removed, empty/failed plan →
  shortcut to `/search` (mirrors this repo's exception→retrieve default). These four
  safeguards are where this repo's production learnings live — don't rediscover them.
- Port the trace event shapes (#8) into the `/agentic_search` response.

**Phase 2 — retriever loop + internal tools**
- New work: the bounded Anthropic tool-use loop (the design's retriever agent). Ports
  feeding it: executor/dispatch (#6), calculator (already in), evidence construction.
- `product_search` tool wraps the existing `/search` via deployment handle — analogous
  to this repo's unregistered `vector_search.py` tool (which is the 10-line template
  for "retrieval as a tool").
- Port responder (#7).

**Phase 3 — external tools**
- Port web_search (#4) behind the provider interface. TMDB/CLIP are new per the design.

**Phase 4 — knowledge tools**
- Apply the graph_search pattern (#5) to the product graph once `build_graph.py`
  exists. `text_to_sql` is new, but note it inherits the same safety philosophy the
  ported tools establish: LLM output is data/parameters, never directly executable
  privilege (parameterized Cypher here ↔ validated read-only SELECT there).

**Phase 5 — eval + hardening**
- Port judge (#11) and the routing-accuracy harness (#12); optionally the semantic
  cache (#9).

### Mechanics and sequencing notes

- Port by **copy-and-adapt, not shared library**: the repos have different LLM
  backends, contracts, and dependency policies; a shared package would couple them for
  ~600 lines of code. Keep a `# ported from scalable-rag-pipeline services/api/app/...`
  provenance line per file.
- Biggest single risk is **over-porting the JSON-wrangling**: `_extract_json`,
  last-object decoding, and the action-coercion ladder exist because of a 3B model.
  With forced tool calls on Claude they're dead weight — port the *fallback behaviors*
  (what to do when validation fails), not the parsing.
- Second risk: this repo's tools return **strings** (fine for one-shot grounding); the
  design's loop needs **structured evidence** so the retriever LLM can reason over
  prior steps. Do the string→EvidenceItem conversion in the executor (#6) from the
  start, not as a retrofit.
- Rough effort: phases 0–2 with the ports in hand are days, not weeks — planner,
  registry, calculator, executor, and responder arrive ~70% written; the genuinely new
  code in that span is the tool loop and the Ray Serve mounting.
