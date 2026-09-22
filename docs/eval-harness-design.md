# Eval harness design

Status: slice 1 implemented; see docs/eval-harness-plan.md.

## Purpose

The existing `eval/` directory does not measure this system: `eval/reports/latest.html` is an upstream placeholder, `eval/ragas/run.py` scores three hard-coded samples, and `golden.json` is never run through the agent. The one real check is `scripts/eval_pipeline.py`, which scores routing *action* only.

This harness measures the agent's own pipeline on the local stack (Ollama embeddings, a hosted or local LLM, Qdrant), reproducibly, so a change to the planner prompt, the model or the retriever can be compared against a baseline.

## Scope

**Slice 1 (this design, to be built first):** planner routing and retrieval, with no LLM judge.

**Later slices (interfaces reserved, not designed in full):**
- **3a, deterministic answer checks:** citation validity, refusal correctness on unanswerable items, expected number present for calculator items, error strings passing as answers.
- **3b, judged answer quality:** faithfulness and correctness against `ground_truth`. The judge model is undecided and `ragas` is not installed.

**Non-goals for slice 1:** answer scoring, graph-arm scoring, a CI gate, HTTP-mode runs, production-code changes.

## Decisions

| Decision | Reason |
|---|---|
| In-process runner, not HTTP | Bypasses the semantic cache and JWT, isolates nodes so a bad score is attributable, and exposes retrieved chunks, which the API stream does not. |
| Retrieval labels are source filenames, plus fuzzy labels from `golden.json` in the same dataset | Filenames give deterministic scoring. `golden.json` has no document IDs, so it needs the fuzzy path. |
| Corpus is `true_data` plus a seeded sample of the noisy PDFs | Cheap to ingest, and retrieval still has to find the right file among many. |
| `scripts/eval_pipeline.py` is absorbed, not deleted | Its cases seed `routing.jsonl`. The script stays as the full-stack HTTP smoke test, since it measures latency through the real API. |
| No production-code changes | Passive recorders wrap the two functions the harness needs to observe. |

## Layout

```
eval/harness/          # code, run as: python -m eval.harness routing|retrieval
eval/sets/             # routing.jsonl, qa.jsonl, corpus_manifest.json   (tracked)
eval/runs/<ts>-<layer>/  # manifest.json, results.jsonl, summary.json, summary.md       (gitignored)
eval/baselines/        # <layer>.json                                    (tracked)
```

## Units

| Unit | Responsibility | Depends on |
|---|---|---|
| Dataset loader | Read and validate the two JSONL files, hash them | none |
| Preflight | Check services and corpus, fail fast with an actionable message | Qdrant, LLM and embed endpoints, Neo4j (probe only) |
| Manifest | Record what produced the run | git, config, preflight |
| Planner recorder | Capture the model's raw plan before coercion | `planner._extract_json` |
| Retrieval recorder | Capture ordered `(filename, page, score)` per search | `qdrant_client.search` |
| Routing runner and scorer | Run `planner_node` (and `tool_node` for calculator), score verdicts | recorders, dataset |
| Retrieval runner and scorer | Run `retrieve_node`, score hits, calibrate the fuzzy threshold | recorders, dataset |
| Report writer | Write `results.jsonl` and `summary.md`, diff against the baseline | manifest, scorers |

## Runner behaviour

- Load `.env`, then start `llm_client` before any node runs.
- Run sequentially by default, because a free-tier hosted model is rate-limited.
- Make one warm-up call before scoring, excluded from timing. Ollama unloads `nomic-embed-text` after about 5 idle minutes, and a cold load took 24 s against a 5 s client timeout.
- `--repeats N` runs each routing item N times, because a hosted model is not reliably deterministic even at temperature 0.
- `retrieve_node` runs **unchanged**. If Neo4j is down, its existing `except` returns `[]`, as in production. A stopped Neo4j fails in about 10 ms (measured), so nothing is stubbed.
- The recorders call the original function and pass the result through untouched. If either wrapped name is renamed, the harness fails loudly at startup.

## Manifest

Recorded with every run: model and endpoint host, embed model and dimension, a hash of the planner system prompt (which embeds the tool descriptions), git SHA and dirty flag, dataset and corpus-manifest hashes, Qdrant point count and vector size, Neo4j node count (or `unavailable`), `k`, the seed, and a `judge` field that is empty in slice 1.

## Preflight

Checks are layer-specific and fail fast:
- **Routing:** the LLM endpoint answers a trivial request. This also warms a hosted model.
- **Retrieval:**
  - The embed endpoint answers within 120 s. This warms Ollama and measures the real dimension, which must equal `EMBED_DIM`.
  - Qdrant is reachable, `rag_collection` exists with a vector size equal to the embed dimension, and it holds points.
  - Every `expected_source` in the dataset appears in the collection's filenames. An item with any missing source is marked `unscorable`, not a miss.
  - Neo4j is probed and its node count recorded. It is not required.

## Datasets

### `routing.jsonl`

`id`, `query`, `tags`, `expected_action` (`direct_answer`, `tool_use` or `retrieve`), `expected_tool` (string or null), and optionally `also_accept` (a list of alternative action/tool pairs for genuinely ambiguous queries) and `expected_tool_output` (substring or regex, checked only for calculator in slice 1).

Tags: greeting, arithmetic, realtime, code, graph, factual, typo, injection, multi-intent. Coreference items carry `known-limitation` and are reported separately from the pass rate, because the planner sees only the latest message.

Seeds: the 14 cases in `scripts/eval_pipeline.py` with tool labels added, and the 6 rows of the table in `docs/agentic-demo-plan.md`.

### `qa.jsonl`

`id`, `query`, `origin` (`hand`, `synthetic` or `golden`), `answerable` (bool), and one of two mutually exclusive label sets:
- `label_type: "source"` with `expected_sources`, a list of filenames matched exactly against the payload `filename`.
- `label_type: "fuzzy"` with `reference_contexts`, free-text passages.

`calibration_sources` (fuzzy items only) is an optional list of filenames that makes an item dual-labelled. `ground_truth` is optional and unused in slice 1. `golden.json` already has it.

Seeds: the 20 golden items as fuzzy items, and hand-written source items starting from the 4 retrieval queries in `scripts/eval_pipeline.py` (Mamba, Windows `KEY_VALUE_BASIC_INFORMATION`, Oracle history tables, mutual information). Their source PDFs must be in the sampled corpus. The fourth of these (Oracle history table storage, originally seeded as `h003`) was dropped: no file in this corpus contains "history table" in the relevant database sense (matches only appear in unrelated CPU branch-prediction papers), so there was no genuine source to label it against. `qa.jsonl`'s hand-written IDs therefore run `h001, h002, h004, h005, h006, h007, h008`.

Payloads are normalised on read: `ingest_local.py` writes nested `metadata.filename`, and the Ray pipeline writes flat `filename`.

**Reporting rule:** metrics are reported per `label_type` and per `origin`, never blended, so LLM-drafted questions (which share vocabulary with their chunk and inflate recall) stay separable from hand-written ones.

## Routing scoring

Verdicts per item: `pass`, `wrong_action`, `wrong_tool` (action right, tool wrong), `planner_failed`, `error`.

`planner_failed` means the planner's LLM call itself failed. The node then silently returns `action=retrieve`, which masks an outage (`ERR-20260619-001`). The harness detects it as the node's fallback `plan` text with **no raw plan recorded**. The same fallback text with a recorded parse error is a model failure (invalid JSON): it is scored as a wrong answer and counted under safeguards. Planner-failed items are excluded from accuracy and reported separately. If more than 20% of items hit it, the run is marked **invalid** (exit code 2), because it measured an outage rather than routing.

Metrics:
- Action accuracy, overall and per tag.
- Tool accuracy given `tool_use`.
- A 3x3 confusion matrix and per-tool precision and recall.
- **Safeguard value:** accuracy on the raw model plan versus accuracy after coercion, plus a count of each safeguard firing (invalid JSON, invalid action, tool promoted to `tool_use`, hallucinated tool to `retrieve`, `direct_answer` with a bad tool to `retrieve`).
- With `--repeats N`: a per-item majority verdict, a stability fraction, and a list of items whose verdicts disagreed.
- **Tool execution check** for items with `expected_tool_output`. In slice 1 only calculator is hermetic. Other tools are recorded as `skipped: <reason>`, never dropped silently.

## Retrieval scoring

Ranking comes from the retrieval recorder, not from `retrieve_node`'s output. The node returns `list(set(...))`, so its documents are unordered and carry no scores. Because the node hard-codes `limit=5`, k is capped at 5.

**Source items:**
- Hit@k for k in 1, 3 and 5: any of the top k chunks comes from an expected source.
- MRR: 1/rank of the first such chunk.
- Source recall@5: the fraction of the item's expected sources found in the top 5.
- Arm contribution: vector hits end in `[Source: filename]`, graph hits are bare `a REL b` strings, so the two are counted by that suffix.
- Empty-context rate.

**Fuzzy items** (weakest evidence, reported separately):
- Relevance is lexical overlap between a retrieved chunk and a reference context. Embedding similarity is a secondary check only, because retrieval ranks by the same `nomic` embeddings and would flatter itself.
- The overlap threshold is calibrated on a **dual-labelled subset**: golden items mapped by hand to their `true_data` file, so they carry both label types. The harness chooses the threshold that best agrees with the source label, chunk by chunk over each item's top 5 (a chunk is source-relevant if it comes from a `calibration_sources` file), and reports the agreement rate. A right file also contains chunks unrelated to the question, so chunk-level agreement understates true agreement and the bar is strict. Below about 85%, fuzzy results are marked unreliable.
- The 20-item mapping is drafted by a script from the `true_data` files and reviewed by the user.

**Unanswerable items:** the retriever has no score threshold, so "correct retrieval" is undefined. Slice 1 reports how well top-1 score separates answerable from unanswerable queries (AUROC, counts, suggested cutoff). That is evidence for adding a threshold later. Below about 20 unanswerable items the number is descriptive only.

## Report

- `results.jsonl`: one line per item and repeat, with verdict, expected and actual, raw plan, recorded retrieval list, latency and any error. `summary.json` and `manifest.json` sit beside it, and baselines are built from them.
- `summary.md`: manifest digest, headline table, per-tag breakdown, failure listing, and a warnings block (`unscorable` count, `planner_failed` rate, graph state).
- Latency: p50 and p95 for the planner call and for retrieval, as diagnostics.
- Baseline: `--set-baseline` writes `eval/baselines/<layer>.json`. `--compare` prints deltas.
- Noise: with 20 to 60 items a one- or two-item swing is noise. Accuracies carry Wilson 95% intervals, and deltas whose intervals overlap are labelled "within noise".
- Comparability: a compare refuses, or warns loudly, if the model, dataset hash, corpus hash or embed model differ. Git SHA and prompt hash may differ, since that is what is being tested.
- Exit codes: 0 for a valid run, 2 for an invalid one. `--fail-under` thresholds are deferred to a later pytest wrapper.

## Corpus

- **Answer sources:** `eval/datasets/true_data/`, 6 files (`architecture.pptx`, `cronjobs.docx`, `monitor_job.docx`, `job_management.html`, `pods_autoscale.html`, `parallel_work_queue.txt`), tracked in git.
- **Distractors:** a seeded sample from the noisy pool. Since `scripts/revert_datasets.py --apply` was run, `ingested/` and `digested/` are empty and the pool is now the 934 files in `noisy_data/`. `failed/` (20) is excluded.
- **Sample size:** about 60 random PDFs plus about 20 hard negatives chosen by keyword (scheduling, distributed, container, autoscaling). The numbers are defaults.
- **Reproducibility:** the noisy directories are gitignored, so the sample is recorded in `eval/sets/corpus_manifest.json` as filenames, SHA-256 hashes and the seed. Only the manifest is tracked. Note that the `random` group is no longer reproducible from the seed alone: after the initial seeded draw, several oversized files (see the composition-fix note above) were manually swapped for smaller ones, without recording which ones or why. The manifest's filenames and hashes, not the seed, are now the authoritative record of the corpus — a fresh `corpus.py build --seed 42` will NOT reproduce this exact set.

## Environment prerequisites

| Step | State on 2026-09-21 |
|---|---|
| Qdrant running | Done (native Docker engine, port 6333, no collections) |
| Ollama embeddings working | Done (768 dimensions) |
| `qdrant-client` matches the API pin (1.7.3, the same version as the compose server image) | Done, in an overlay venv `.venv-eval` (the conda environment has 1.18.0, which has no `AsyncQdrantClient.search`, and cannot be downgraded without breaking `langchain-qdrant` and, via protobuf, `vllm`). Run the harness with `.venv-eval/bin/python`. |
| Create `rag_collection`, 768 dimensions, cosine | Done. Created manually via `curl -X PUT .../collections/rag_collection` (nothing local creates it automatically; only the EKS bootstrap does, at 1024). |
| Set `EMBED_DIM=768` in `.env` | Done (`.env` line 65). |
| Ingest `true_data` and the sample through the Ray pipeline with `--no-graph` | Done. Two production bugs found and fixed along the way (`langchain.text_splitter` moved packages; `read_binary_files` was hardcoded to PDF-only, silently dropping docx/html/pptx/txt). True_data smoke ingest: ~70s. Full 80-file corpus: 57m34s, 36,868 chunks. A duplication bug (staging copies `true_data` into `eval_corpus`, so ingesting both separately double-counted 6 files) was found and fixed with a targeted delete, not a re-ingest. A corpus-composition issue (one 4983-page forced file was 40.5% of the collection, swamping unrelated queries by chunk volume) was found and fixed by truncating it to a 30-page excerpt and swapping 5 other oversized files for smaller ones. The excerpt (`Windows Kernel - MSDN Export - Registry Key Structures Excerpt.pdf`) was derived from the original `Windows Kernel - MSDN Export (7th Feb, 2019).pdf`, pages 250–280 (0-indexed), via `pypdf.PdfWriter`. Final collection: 12,780 points, no file over 7.3%. |
| `scripts/revert_datasets.py --apply` | Applied. `eval/datasets/ingested/` and `digested/` are now empty; their files were moved back into `noisy_data/`. |

The Ray pipeline is chosen because `ingest_local.py` supports only PDF, TXT and MD, and 5 of the 6 `true_data` files are pptx, docx or html. It also gives one flat payload shape.

## Slice 3 interface commitments

1. The runner can stream the full `agent_app` to capture the final answer and retrieved documents, in addition to calling nodes one by one.
2. `qa.jsonl` already carries `ground_truth` and `answerable`.
3. The manifest reserves a `judge` field (model, endpoint, prompt hash).

## Known limits

- Fuzzy retrieval labels are the weakest evidence in the harness, and the calibration subset is small.
- With 20 to 60 items per layer, differences under a few items are noise.
- A hosted free-tier model can change silently or be rate-limited. Results are only comparable within one recorded model.
- The recorders depend on the private name `_extract_json` and on `qdrant_client.search`. A rename breaks the harness loudly, by design.
- k is capped at 5 by the retriever's hard-coded limit.
- The noisy sample is mostly topically far from Kubernetes, which is why hard negatives are included.
- Graph retrieval is not scored in slice 1.
- The harness must run with the API's pinned `qdrant-client` (1.7.3), via `.venv-eval/bin/python`. Newer clients removed `search`, which the retriever calls.
