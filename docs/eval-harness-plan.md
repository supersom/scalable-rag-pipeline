# Eval Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, in-process eval harness that scores the agent's planner routing and retrieval on the local stack, with baselines and comparison.

**Architecture:** A `eval/harness/` package imports `planner_node`, `tool_node` and `retrieve_node` directly (no API, cache or JWT). Two passive recorders wrap `planner._extract_json` and `qdrant_client.search` to observe raw plans and ranked hits without changing production code. Pure scorer functions take recorded data; a CLI wires services, runs a layer, and writes `manifest.json`, `results.jsonl`, `summary.json` and `summary.md`.

**Tech Stack:** Python 3.12, pytest 9 (no pytest-asyncio; tests use `asyncio.run`), httpx, qdrant-client, python-dotenv, pypdf, python-docx, python-pptx, beautifulsoup4 (all already installed).

**Spec:** `docs/eval-harness-design.md`

## Global Constraints

- Work on branch `dev`. Never push.
- Run every command from the repo root `/home/som/code/scalable-rag-pipeline` (`Settings` reads `.env` relative to the working directory). Use `.venv-eval/bin/python -m pytest`, not bare `pytest`.
- No production-code changes: nothing under `services/` or `pipelines/` is modified.
- No new dependencies. Tests never need a running service.
- Commits: the user's standing rule is no commits until asked. At the start of execution, ask once whether to commit after each task. Each task ends with a commit step; run it only if authorised. Commit messages explain why, and end with `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>`.
- Code style: no comments unless the reason is non-obvious; no abstraction beyond what a task needs.
- k is capped at 5 because `retrieve_node` hard-codes `limit=5`. Hit@k is reported for k in 1, 3 and 5.
- Accuracies carry Wilson 95% intervals (z = 1.96).
- A routing run with more than 20% `planner_failed` results is **invalid** (exit code 2). Valid runs exit 0.
- Fuzzy retrieval results are marked unreliable when calibration agreement is below 85%.
- Unanswerable-item separation (AUROC) is descriptive only below 20 unanswerable items.
- Every `python` command in this plan runs with `.venv-eval/bin/python` (an overlay venv pinning `qdrant-client==1.7.3`; see Task 0). With a shell that does not persist state, call `.venv-eval/bin/python` explicitly, for example `.venv-eval/bin/python -m pytest`.

## File Structure

```
conftest.py                              # empty; puts the repo root on sys.path for pytest
eval/harness/__init__.py                 # empty
eval/harness/datasets.py                 # JSONL loaders + validation + file hashing
eval/harness/stats.py                    # Wilson, AUROC, percentile, lexical containment
eval/harness/recorders.py                # PlannerRecorder, RetrievalRecorder, normalize_payload
eval/harness/routing.py                  # routing runner + scorer + summary
eval/harness/retrieval.py                # retrieval runner + scorer + calibration + summary
eval/harness/manifest.py                 # run manifest, prompt hash, git info, comparability
eval/harness/preflight.py                # layer-specific preflight with injected probes
eval/harness/probes.py                   # real probes (LLM, embed, Qdrant, Neo4j); live-verified only
eval/harness/report.py                   # results/summary writers, baselines, comparison
eval/harness/cli.py                      # argument parsing, orchestration, exit codes
eval/harness/__main__.py                 # python -m eval.harness entry
eval/harness/corpus.py                   # corpus sampling, manifest, staging, text extraction
eval/sets/routing.jsonl                  # routing dataset
eval/sets/qa.jsonl                       # retrieval dataset
eval/sets/corpus_manifest.json           # sampled corpus (filenames + sha256 + seed)
scripts/seed_qa.py                       # golden.json -> qa.jsonl with suggested calibration sources
scripts/add_source_item.py               # locate a source file by phrase and append a source item
tests/eval_harness/test_*.py             # one test file per harness module
```

---

### Task 0: Pin `qdrant-client` 1.7.3 in an overlay venv (done 2026-09-21)

**Files:** none tracked. Creates the gitignored `.venv-eval/` and adds `.venv-eval/` to `.gitignore`.

**Why:** `services/api/requirements.txt:25` pins `qdrant-client==1.7.3`, and the compose server is `qdrant/qdrant:v1.7.3`. The conda environment has 1.18.0, which has no `AsyncQdrantClient.search`, so `retrieve_node` raises `AttributeError` there.

**Decision:** installing the pin into the conda environment was rejected, because it is a shared environment:
- A normal install would downgrade `protobuf` 6.33.6 to 4.25.9 (the installed `grpcio-tools 1.62.3` requires `protobuf<5`), breaking `grpcio-status`, `vllm 0.20.1` and `opentelemetry-proto`.
- A `--no-deps` install would leave `langchain-qdrant 1.1.0` (needs `qdrant-client>=1.15.1`) broken.
- `pip check` crashes in the conda environment on a malformed wheel tag in `nvidia_nvcomp_cu12`, so it cannot be used for verification.

Instead the harness, the corpus tooling and ingestion run in an overlay venv that inherits the conda packages and shadows only `qdrant-client`.

**Interfaces:**
- Produces: `.venv-eval/bin/python` with `qdrant-client 1.7.3`. **Every `python` command in the rest of this plan means `.venv-eval/bin/python`.**

- [x] **Step 1: Confirm the problem.** `python -c "from qdrant_client import AsyncQdrantClient as A; ..."` printed `1.18.0 False`.
- [x] **Step 2: Preview the install.** `pip install --dry-run qdrant-client==1.7.3` reported `Would install protobuf-4.25.9 qdrant-client-1.7.3`. Stopped and asked the user.
- [x] **Step 3: User chose the overlay venv.**
- [x] **Step 4: Create it.**

```bash
python -m venv --system-site-packages .venv-eval
.venv-eval/bin/python -m pip install --no-deps qdrant-client==1.7.3
```

- [x] **Step 5: Verify the pin and isolation.** In the venv: `qdrant-client 1.7.3`, `search` present, `protobuf 6.33.6` unchanged, every requirement of `qdrant-client` 1.7.3 satisfied. In the conda environment: still `qdrant-client 1.18.0`, `protobuf 6.33.6`, `langchain-qdrant 1.1.0`.
- [x] **Step 6: Verify the app's client and dependencies in the venv.** `qdrant_client.search(...)` against the empty server raised `UnexpectedResponse: 404 Not Found` (the collection does not exist yet), not `AttributeError`. `import ray, pypdf, docx, pptx, bs4, pytest, dotenv, httpx` and `import qdrant_client.grpc` (1.7.3's generated code under protobuf 6) both succeed. A Ray worker reports `sys.executable` inside `.venv-eval` and `qdrant-client 1.7.3`.

To recreate the venv on another machine, rerun the two commands in Step 4.

---

### Task 1: Package skeleton and dataset loaders (done 2026-09-21)

**Files:**
- Create: `conftest.py`, `eval/harness/__init__.py`, `eval/harness/datasets.py`, `tests/eval_harness/test_datasets.py`
- Modify: `.gitignore` (append two lines)

**Interfaces:**
- Produces (used by Tasks 4, 5, 8, 11):
  - Constants `ACTIONS`, `TOOLS`, `ORIGINS`, `LABEL_TYPES` (tuples of str).
  - `class DatasetError(ValueError)`.
  - `RoutingItem(id, query, expected_action, expected_tool, tags, also_accept, expected_tool_output)`: frozen dataclass; `tags` is a tuple of str; `also_accept` is a tuple of `(action, tool)` tuples.
  - `QAItem(id, query, origin, answerable, label_type, expected_sources, reference_contexts, calibration_sources, ground_truth, tags)`: frozen dataclass; the three `*_sources`/`reference_contexts` fields are tuples of str; `label_type` is `None` for unanswerable items.
  - `load_routing(path) -> list[RoutingItem]`, `load_qa(path) -> list[QAItem]`, `file_sha256(path) -> str`.

- [x] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_datasets.py`:

```python
import json

import pytest

from eval.harness.datasets import DatasetError, file_sha256, load_qa, load_routing


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


def routing_row(**over):
    row = {"id": "r1", "query": "hi", "expected_action": "direct_answer", "expected_tool": None, "tags": ["greeting"]}
    row.update(over)
    return row


def qa_row(**over):
    row = {"id": "q1", "query": "what is x", "origin": "hand", "answerable": True,
           "label_type": "source", "expected_sources": ["a.pdf"]}
    row.update(over)
    return row


def test_load_routing_valid(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [
        routing_row(),
        routing_row(id="r2", query="3*7", expected_action="tool_use", expected_tool="calculator",
                    tags=["arithmetic"], expected_tool_output="21",
                    also_accept=[["tool_use", "sandbox"]]),
    ])
    items = load_routing(path)
    assert [i.id for i in items] == ["r1", "r2"]
    assert items[0].expected_tool is None and items[0].also_accept == ()
    assert items[1].also_accept == (("tool_use", "sandbox"),)
    assert items[1].expected_tool_output == "21"
    assert items[1].tags == ("arithmetic",)


def test_routing_duplicate_id_reports_line(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(), routing_row()])
    with pytest.raises(DatasetError, match=r"routing.jsonl:2: duplicate id 'r1'"):
        load_routing(path)


def test_routing_bad_action(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(expected_action="answer")])
    with pytest.raises(DatasetError, match="expected action must be one of"):
        load_routing(path)


def test_routing_tool_use_requires_known_tool(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(expected_action="tool_use", expected_tool="nope")])
    with pytest.raises(DatasetError, match="expected tool must be one of"):
        load_routing(path)


def test_routing_non_tool_action_forbids_tool(tmp_path):
    path = tmp_path / "routing.jsonl"
    write_jsonl(path, [routing_row(expected_tool="calculator")])
    with pytest.raises(DatasetError, match="must be null unless action is tool_use"):
        load_routing(path)


def test_routing_invalid_json_reports_line(tmp_path):
    path = tmp_path / "routing.jsonl"
    path.write_text(json.dumps(routing_row()) + "\n{not json}\n")
    with pytest.raises(DatasetError, match=r"routing.jsonl:2: invalid JSON"):
        load_routing(path)


def test_load_qa_source_fuzzy_and_unanswerable(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [
        qa_row(),
        qa_row(id="q2", origin="golden", label_type="fuzzy", expected_sources=None,
               reference_contexts=["passage"], calibration_sources=["hpa.html"], ground_truth="gt"),
        qa_row(id="q3", answerable=False, label_type=None, expected_sources=None),
    ])
    items = load_qa(path)
    assert items[0].label_type == "source" and items[0].expected_sources == ("a.pdf",)
    assert items[1].reference_contexts == ("passage",) and items[1].calibration_sources == ("hpa.html",)
    assert items[1].ground_truth == "gt"
    assert items[2].answerable is False and items[2].label_type is None


def test_qa_source_requires_sources(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(expected_sources=[])])
    with pytest.raises(DatasetError, match="source items need expected_sources"):
        load_qa(path)


def test_qa_source_rejects_reference_contexts(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(reference_contexts=["x"])])
    with pytest.raises(DatasetError, match="source items must not have reference_contexts"):
        load_qa(path)


def test_qa_calibration_sources_only_for_fuzzy(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(calibration_sources=["a.pdf"])])
    with pytest.raises(DatasetError, match="calibration_sources is only allowed on fuzzy items"):
        load_qa(path)


def test_qa_unanswerable_forbids_labels(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(answerable=False, label_type=None)])
    with pytest.raises(DatasetError, match="unanswerable items must have no labels"):
        load_qa(path)


def test_qa_bad_origin_and_non_bool_answerable(tmp_path):
    path = tmp_path / "qa.jsonl"
    write_jsonl(path, [qa_row(origin="llm")])
    with pytest.raises(DatasetError, match="origin must be one of"):
        load_qa(path)
    write_jsonl(path, [qa_row(answerable="yes")])
    with pytest.raises(DatasetError, match="answerable must be true or false"):
        load_qa(path)


def test_file_sha256_changes_with_content(tmp_path):
    path = tmp_path / "f.txt"
    path.write_text("a")
    first = file_sha256(path)
    path.write_text("b")
    assert first != file_sha256(path) and len(first) == 64
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/eval_harness/test_datasets.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness'`

- [x] **Step 3: Create the package files**

Create an empty `conftest.py` at the repo root and an empty `eval/harness/__init__.py`. Append to `.gitignore`:

```
eval/runs/
eval/datasets/eval_corpus/
```

Create `eval/harness/datasets.py`:

```python
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ACTIONS = ("direct_answer", "tool_use", "retrieve")
TOOLS = ("calculator", "web_search", "sandbox", "graph_search")
ORIGINS = ("hand", "synthetic", "golden")
LABEL_TYPES = ("source", "fuzzy")


class DatasetError(ValueError):
    pass


@dataclass(frozen=True)
class RoutingItem:
    id: str
    query: str
    expected_action: str
    expected_tool: Optional[str]
    tags: tuple
    also_accept: tuple
    expected_tool_output: Optional[str]


@dataclass(frozen=True)
class QAItem:
    id: str
    query: str
    origin: str
    answerable: bool
    label_type: Optional[str]
    expected_sources: tuple
    reference_contexts: tuple
    calibration_sources: tuple
    ground_truth: Optional[str]
    tags: tuple


def file_sha256(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _fail(path, lineno, message):
    raise DatasetError(f"{path}:{lineno}: {message}")


def _read_jsonl(path):
    rows = []
    for lineno, line in enumerate(Path(path).read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            rows.append((lineno, json.loads(line)))
        except json.JSONDecodeError as e:
            raise DatasetError(f"{path}:{lineno}: invalid JSON ({e.msg})") from e
    return rows


def _text(row, key, path, lineno):
    value = row.get(key)
    if not isinstance(value, str) or not value.strip():
        _fail(path, lineno, f"'{key}' is required and must be a non-empty string")
    return value


def _string_list(row, key, path, lineno):
    value = row.get(key) or []
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        _fail(path, lineno, f"'{key}' must be a list of non-empty strings")
    return tuple(value)


def _check_action_tool(action, tool, path, lineno, where):
    if action not in ACTIONS:
        _fail(path, lineno, f"{where} action must be one of {ACTIONS}, got {action!r}")
    if action == "tool_use" and tool not in TOOLS:
        _fail(path, lineno, f"{where} tool must be one of {TOOLS} when action is tool_use, got {tool!r}")
    if action != "tool_use" and tool is not None:
        _fail(path, lineno, f"{where} tool must be null unless action is tool_use, got {tool!r}")


def _unique_id(row, seen, path, lineno):
    item_id = _text(row, "id", path, lineno)
    if item_id in seen:
        _fail(path, lineno, f"duplicate id {item_id!r}")
    seen.add(item_id)
    return item_id


def load_routing(path):
    items, seen = [], set()
    for lineno, row in _read_jsonl(path):
        item_id = _unique_id(row, seen, path, lineno)
        action, tool = row.get("expected_action"), row.get("expected_tool")
        _check_action_tool(action, tool, path, lineno, "expected")
        also = []
        for pair in row.get("also_accept") or []:
            if not isinstance(pair, list) or len(pair) != 2:
                _fail(path, lineno, "'also_accept' entries must be [action, tool] pairs")
            _check_action_tool(pair[0], pair[1], path, lineno, "also_accept")
            also.append((pair[0], pair[1]))
        output = row.get("expected_tool_output")
        if output is not None and not isinstance(output, str):
            _fail(path, lineno, "'expected_tool_output' must be a string")
        items.append(RoutingItem(
            id=item_id,
            query=_text(row, "query", path, lineno),
            expected_action=action,
            expected_tool=tool,
            tags=_string_list(row, "tags", path, lineno),
            also_accept=tuple(also),
            expected_tool_output=output,
        ))
    return items


def load_qa(path):
    items, seen = [], set()
    for lineno, row in _read_jsonl(path):
        item_id = _unique_id(row, seen, path, lineno)
        origin = row.get("origin")
        if origin not in ORIGINS:
            _fail(path, lineno, f"origin must be one of {ORIGINS}, got {origin!r}")
        answerable = row.get("answerable")
        if not isinstance(answerable, bool):
            _fail(path, lineno, "answerable must be true or false")
        label_type = row.get("label_type")
        sources = _string_list(row, "expected_sources", path, lineno)
        contexts = _string_list(row, "reference_contexts", path, lineno)
        calibration = _string_list(row, "calibration_sources", path, lineno)
        if answerable:
            if label_type not in LABEL_TYPES:
                _fail(path, lineno, f"label_type must be one of {LABEL_TYPES} for answerable items")
            if label_type == "source":
                if not sources:
                    _fail(path, lineno, "source items need expected_sources")
                if contexts:
                    _fail(path, lineno, "source items must not have reference_contexts")
                if calibration:
                    _fail(path, lineno, "calibration_sources is only allowed on fuzzy items")
            else:
                if not contexts:
                    _fail(path, lineno, "fuzzy items need reference_contexts")
                if sources:
                    _fail(path, lineno, "fuzzy items must not have expected_sources")
        elif label_type is not None or sources or contexts or calibration:
            _fail(path, lineno, "unanswerable items must have no labels")
        ground_truth = row.get("ground_truth")
        items.append(QAItem(
            id=item_id,
            query=_text(row, "query", path, lineno),
            origin=origin,
            answerable=answerable,
            label_type=label_type if answerable else None,
            expected_sources=sources,
            reference_contexts=contexts,
            calibration_sources=calibration,
            ground_truth=ground_truth if isinstance(ground_truth, str) else None,
            tags=_string_list(row, "tags", path, lineno),
        ))
    return items
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/eval_harness/test_datasets.py -q`
Expected: `13 passed`

- [x] **Step 5: Commit (if authorised)**

```bash
git add conftest.py eval/harness/__init__.py eval/harness/datasets.py tests/eval_harness/test_datasets.py .gitignore
git commit -m "feat(eval): add dataset loaders with validation

Validated JSONL loaders give every later harness unit typed items and
fail with file:line messages instead of surfacing bad labels as bogus
scores mid-run."
```

---

### Task 2: Statistics helpers (done 2026-09-21)

**Files:**
- Create: `eval/harness/stats.py`, `tests/eval_harness/test_stats.py`

**Interfaces:**
- Produces (used by Tasks 4, 5, 7):
  - `wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]`; `(0.0, 1.0)` when `n == 0`.
  - `intervals_overlap(a: tuple, b: tuple) -> bool`.
  - `auroc(positive: list[float], negative: list[float]) -> float | None`; ties count 0.5; `None` if either list is empty.
  - `percentile(values: list[float], q: float) -> float | None`; linear interpolation, `q` in 0..100.
  - `content_tokens(text: str) -> set[str]`; lowercase alphanumerics of length 3 or more, minus stopwords.
  - `containment(reference: str, chunk: str) -> float`; the share of the reference's content tokens found in the chunk, `0.0` for an empty reference.

- [x] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_stats.py`:

```python
import pytest

from eval.harness.stats import auroc, containment, content_tokens, intervals_overlap, percentile, wilson_interval


def test_wilson_known_values():
    lo, hi = wilson_interval(8, 10)
    assert lo == pytest.approx(0.490, abs=0.002) and hi == pytest.approx(0.943, abs=0.002)
    assert wilson_interval(0, 10) == (0.0, pytest.approx(0.2775, abs=0.002))
    assert wilson_interval(10, 10) == (pytest.approx(0.7225, abs=0.002), 1.0)


def test_wilson_empty_sample_is_uninformative():
    assert wilson_interval(0, 0) == (0.0, 1.0)


def test_intervals_overlap():
    assert intervals_overlap((0.1, 0.5), (0.4, 0.9))
    assert intervals_overlap((0.1, 0.5), (0.5, 0.9))
    assert not intervals_overlap((0.1, 0.4), (0.5, 0.9))


def test_auroc():
    assert auroc([0.9, 0.8], [0.5, 0.4]) == 1.0
    assert auroc([0.5], [0.5]) == 0.5
    assert auroc([0.4], [0.9]) == 0.0
    assert auroc([], [0.1]) is None and auroc([0.1], []) is None


def test_percentile():
    assert percentile([1, 2, 3, 4, 5], 50) == 3.0
    assert percentile([1, 2, 3, 4, 5], 95) == pytest.approx(4.8)
    assert percentile([7], 95) == 7.0
    assert percentile([], 50) is None


def test_content_tokens_drops_stopwords_and_short_words():
    assert content_tokens("The Pod is an HPA target, and it has 3 replicas") == {"pod", "hpa", "target", "replicas"}


def test_containment():
    reference = "Horizontal Pod Autoscaler adjusts replicas"
    assert containment(reference, "The horizontal pod autoscaler adjusts the number of replicas") == 1.0
    assert containment(reference, "The horizontal pod autoscaler adjusts the number") == pytest.approx(0.8)
    assert containment(reference, "assembly language registers") == 0.0
    assert containment("", "anything") == 0.0
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/eval_harness/test_stats.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.stats'`

- [x] **Step 3: Write the implementation**

Create `eval/harness/stats.py`:

```python
import math
import re

_STOPWORDS = frozenset(
    "the and for are was with that this from into its has have can not but you all any one how what when "
    "where which who will each also than then them they their there these those been being such".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")


def wilson_interval(successes, n, z=1.96):
    if n == 0:
        return (0.0, 1.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def intervals_overlap(a, b):
    return a[0] <= b[1] and b[0] <= a[1]


def auroc(positive, negative):
    if not positive or not negative:
        return None
    wins = 0.0
    for p in positive:
        for n in negative:
            wins += 1.0 if p > n else 0.5 if p == n else 0.0
    return wins / (len(positive) * len(negative))


def percentile(values, q):
    if not values:
        return None
    ordered = sorted(values)
    idx = (len(ordered) - 1) * q / 100
    lo, hi = math.floor(idx), math.ceil(idx)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)


def content_tokens(text):
    return {t for t in _TOKEN.findall(text.lower()) if len(t) >= 3 and t not in _STOPWORDS}


def containment(reference, chunk):
    ref = content_tokens(reference)
    if not ref:
        return 0.0
    return len(ref & content_tokens(chunk)) / len(ref)
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/eval_harness/test_stats.py -q`
Expected: `7 passed`

- [x] **Step 5: Commit (if authorised)**

```bash
git add eval/harness/stats.py tests/eval_harness/test_stats.py
git commit -m "feat(eval): add Wilson, AUROC, percentile and lexical containment helpers

Small evals (20-60 items) need confidence intervals to separate signal
from noise, and lexical containment gives fuzzy relevance that does not
share the retriever's embedding space."
```

---

### Task 3: Passive recorders (done 2026-09-21)

**Files:**
- Create: `eval/harness/recorders.py`, `tests/eval_harness/test_recorders.py`

**Interfaces:**
- Produces (used by Tasks 4, 5, 8):
  - `class RecorderError(RuntimeError)`.
  - `normalize_payload(payload: dict | None) -> tuple[str, int | None, str]` returning `(filename, page, text)`. It handles the flat payload (`filename`, `page`) and the nested one (`metadata.filename`, `metadata.page_number`); the filename is `"unknown"` if absent.
  - `PlannerRecorder(module)`: wraps `module._extract_json`. Methods `reset()`, `last() -> dict | None`, `install()`, `uninstall()`, and context-manager use. `last()` is the model's raw plan dict, or `{"_error": "<message>"}` if parsing raised (the exception is re-raised unchanged), or `None` if `_extract_json` was never called since `reset()`.
  - `RetrievalRecorder(client)`: wraps the async `client.search`. Same method set. `last()` is a list of `{"filename", "page", "score", "text"}` in rank order, or `None` if `search` was not called since `reset()`. The original result is returned untouched.
  - Both constructors raise `RecorderError` if the wrapped name is missing (and for `RetrievalRecorder`, if `search` is not an async method).

- [x] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_recorders.py`:

```python
import asyncio
import types

import pytest

from eval.harness.recorders import PlannerRecorder, RecorderError, RetrievalRecorder, normalize_payload


def test_normalize_payload_flat_nested_and_missing():
    assert normalize_payload({"filename": "a.pdf", "page": 3, "text": "t"}) == ("a.pdf", 3, "t")
    assert normalize_payload({"text": "t", "metadata": {"filename": "b.pdf", "page_number": 7}}) == ("b.pdf", 7, "t")
    assert normalize_payload(None) == ("unknown", None, "")


def make_module(fn):
    return types.SimpleNamespace(_extract_json=fn)


def test_planner_recorder_captures_raw_plan_and_passes_through():
    module = make_module(lambda text: {"action": "retrieve", "tool_name": None})
    recorder = PlannerRecorder(module)
    with recorder:
        assert module._extract_json("x") == {"action": "retrieve", "tool_name": None}
    assert recorder.last() == {"action": "retrieve", "tool_name": None}


def test_planner_recorder_records_parse_error_and_reraises():
    def boom(text):
        raise ValueError("no json")

    module = make_module(boom)
    recorder = PlannerRecorder(module)
    with recorder:
        with pytest.raises(ValueError):
            module._extract_json("x")
    assert recorder.last() == {"_error": "no json"}


def test_planner_recorder_restores_original_and_resets():
    def original(text):
        return {}

    module = make_module(original)
    recorder = PlannerRecorder(module)
    with recorder:
        assert module._extract_json is not original
        module._extract_json("x")
        recorder.reset()
        assert recorder.last() is None
    assert module._extract_json is original


def test_planner_recorder_fails_loudly_when_function_is_missing():
    with pytest.raises(RecorderError, match="_extract_json"):
        PlannerRecorder(types.SimpleNamespace())


class FakeClient:
    def __init__(self, results):
        self.results = results

    async def search(self, vector, limit=5):
        return self.results


def point(payload, score):
    return types.SimpleNamespace(payload=payload, score=score)


def test_retrieval_recorder_records_ranked_hits_and_passes_result_through():
    results = [
        point({"filename": "a.pdf", "page": 1, "text": "alpha"}, 0.9),
        point({"text": "beta", "metadata": {"filename": "b.pdf", "page_number": 2}}, 0.5),
    ]
    client = FakeClient(results)
    recorder = RetrievalRecorder(client)
    with recorder:
        returned = asyncio.run(client.search([0.1], limit=5))
    assert returned is results
    assert recorder.last() == [
        {"filename": "a.pdf", "page": 1, "score": 0.9, "text": "alpha"},
        {"filename": "b.pdf", "page": 2, "score": 0.5, "text": "beta"},
    ]


def test_retrieval_recorder_stops_recording_after_exit():
    client = FakeClient([point({"filename": "a.pdf", "text": "t"}, 0.1)])
    recorder = RetrievalRecorder(client)
    with recorder:
        pass
    recorder.reset()
    asyncio.run(client.search([0.1]))
    assert recorder.last() is None


def test_retrieval_recorder_rejects_sync_search():
    class SyncClient:
        def search(self, vector, limit=5):
            return []

    with pytest.raises(RecorderError, match="async"):
        RetrievalRecorder(SyncClient())
```

- [x] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/eval_harness/test_recorders.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.recorders'`

- [x] **Step 3: Write the implementation**

Create `eval/harness/recorders.py`:

```python
import inspect


class RecorderError(RuntimeError):
    pass


def normalize_payload(payload):
    payload = payload or {}
    meta = payload.get("metadata") or {}
    filename = payload.get("filename") or meta.get("filename") or "unknown"
    page = payload.get("page")
    if page is None:
        page = meta.get("page_number")
    return filename, page, payload.get("text", "")


class _Recorder:
    _last = None

    def reset(self):
        self._last = None

    def last(self):
        return self._last

    def __enter__(self):
        self.install()
        return self

    def __exit__(self, *exc):
        self.uninstall()
        return False


class PlannerRecorder(_Recorder):
    def __init__(self, module):
        if not callable(getattr(module, "_extract_json", None)):
            raise RecorderError("planner module has no callable _extract_json; the harness needs it to capture raw plans")
        self._module = module
        self._original = None

    def install(self):
        original = self._module._extract_json
        recorder = self

        def wrapped(text):
            try:
                plan = original(text)
            except Exception as e:
                recorder._last = {"_error": str(e)}
                raise
            recorder._last = dict(plan)
            return plan

        self._original = original
        self._module._extract_json = wrapped

    def uninstall(self):
        if self._original is not None:
            self._module._extract_json = self._original
            self._original = None


class RetrievalRecorder(_Recorder):
    def __init__(self, client):
        if not inspect.iscoroutinefunction(getattr(client, "search", None)):
            raise RecorderError("client.search must be an async method; the harness needs it to record ranked hits")
        self._client = client
        self._original = None

    def install(self):
        original = self._client.search
        recorder = self

        async def wrapped(*args, **kwargs):
            results = await original(*args, **kwargs)
            hits = []
            for r in results:
                filename, page, text = normalize_payload(r.payload)
                hits.append({"filename": filename, "page": page, "score": r.score, "text": text})
            recorder._last = hits
            return results

        self._original = original
        self._client.search = wrapped

    def uninstall(self):
        if self._original is not None:
            self._client.search = self._original
            self._original = None
```

- [x] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/eval_harness/test_recorders.py -q`
Expected: `8 passed`

- [x] **Step 5: Commit (if authorised)**

```bash
git add eval/harness/recorders.py tests/eval_harness/test_recorders.py
git commit -m "feat(eval): add passive recorders for raw planner plans and ranked hits

retrieve_node returns an unordered, score-less list, so ranking and the
model's pre-coercion plan can only be observed by wrapping two functions.
The wrappers pass results through untouched so production code is unchanged."
```

---

### Task 4: Routing runner and scorer

**Files:**
- Create: `eval/harness/routing.py`, `tests/eval_harness/test_routing.py`

**Interfaces:**
- Consumes: `RoutingItem`, `ACTIONS`, `TOOLS` (Task 1); `percentile` (Task 2). A recorder object needs only `reset()` and `last()`, so tests use a fake.
- Produces (used by Tasks 7, 8):
  - Constants `FALLBACK_PLAN_PREFIX = "Error in planning"`, `INVALID_RUN_PLANNER_FAILED_RATE = 0.20`, `KNOWN_LIMITATION_TAG = "known-limitation"`, `SCORED = ("pass", "wrong_action", "wrong_tool")`.
  - `RoutingResult` dataclass with fields `item_id, repeat, verdict, actual_action, actual_tool, raw_plan, raw_verdict, safeguards (list), safeguard_mismatch (bool), tool_check, tool_output, latency_ms, error`. Verdict is one of `pass`, `wrong_action`, `wrong_tool`, `planner_failed`, `error`.
  - `judge(item, action, tool) -> str` returning `pass`, `wrong_tool` or `wrong_action`.
  - `raw_verdict(item, raw_plan) -> str` returning `invalid` if the plan is `None` or has `_error`, else `wrong_action` for an invalid action, else `judge(...)`.
  - `simulate_planner(raw_plan, known_tools) -> tuple[list[str], str, str | None]`, returning `(safeguards_fired, final_action, final_tool)`.
  - `async run_item(item, repeat, planner_fn, tool_fn, recorder, known_tools) -> RoutingResult`.
  - `async run_routing(items, repeats, planner_fn, tool_fn, recorder, known_tools) -> list[RoutingResult]`.
  - `summarize_routing(items, results) -> dict` with keys `headline` (`{name: {"successes", "n"}}` for `pass_rate`, `action_accuracy` and, when defined, `tool_accuracy`), `per_tag`, `confusion`, `per_tool`, `safeguards` (`fired`, `value`, `mismatches`), `stability` (`value`, `unstable`), `tool_execution` (`pass`, `fail`, `skipped`), `planner_failed` (`count`, `rate`), `errors`, `invalid`, `latency_ms` (`p50`, `p95`), `n_items`, `n_results`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_routing.py`:

```python
import asyncio

from eval.harness.datasets import RoutingItem
from eval.harness.routing import (
    RoutingResult, judge, raw_verdict, run_item, run_routing, simulate_planner, summarize_routing,
)

TOOLS = {"calculator", "web_search", "sandbox", "graph_search"}


def item(id="a", action="direct_answer", tool=None, tags=(), also=(), output=None, query="q"):
    return RoutingItem(id=id, query=query, expected_action=action, expected_tool=tool, tags=tuple(tags),
                       also_accept=tuple(also), expected_tool_output=output)


class FakeRecorder:
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def last(self):
        return self.value


def planner(recorder, raw, out):
    async def fn(state):
        recorder.value = raw
        return out
    return fn


def tool(text):
    async def fn(state):
        return {"documents": [text]}
    return fn


def out(action, tool_name="", tool_input="", plan=None):
    return {"action": action, "tool_name": tool_name, "current_query": "q", "tool_input": tool_input,
            "plan": plan or [f"action={action}"]}


CALC = item("c", "tool_use", "calculator", output="21")
FALLBACK = "Error in planning, defaulting to retrieval."


def test_judge():
    assert judge(CALC, "tool_use", "calculator") == "pass"
    assert judge(CALC, "tool_use", "web_search") == "wrong_tool"
    assert judge(CALC, "retrieve", None) == "wrong_action"
    ambiguous = item(action="tool_use", tool="web_search", also=[("tool_use", "calculator")])
    assert judge(ambiguous, "tool_use", "calculator") == "pass"
    assert judge(item(), "direct_answer", None) == "pass"


def test_raw_verdict():
    assert raw_verdict(CALC, None) == "invalid"
    assert raw_verdict(CALC, {"_error": "x"}) == "invalid"
    assert raw_verdict(CALC, {"action": "answer"}) == "wrong_action"
    assert raw_verdict(CALC, {"action": "tool_use", "tool_name": "calculator"}) == "pass"
    assert raw_verdict(CALC, {"action": "retrieve", "tool_name": "calculator"}) == "wrong_action"
    assert raw_verdict(item(action="retrieve"), {"action": "retrieve", "tool_name": "calculator"}) == "wrong_tool"


def test_simulate_planner_covers_each_safeguard():
    assert simulate_planner({"_error": "x"}, TOOLS) == (["raw_invalid_json"], "retrieve", None)
    assert simulate_planner({"action": "tool_use", "tool_name": "nope"}, TOOLS) == (["hallucinated_tool"], "retrieve", None)
    assert simulate_planner({"action": "retrieve", "tool_name": "calculator"}, TOOLS) == (["tool_promoted"], "tool_use", "calculator")
    assert simulate_planner({"action": "bogus", "tool_name": "calculator"}, TOOLS) == (["invalid_action"], "retrieve", None)
    assert simulate_planner({"action": "direct_answer", "tool_name": "nope"}, TOOLS) == (["direct_answer_bad_tool"], "retrieve", None)
    assert simulate_planner({"action": "tool_use", "tool_name": ["calculator"]}, TOOLS) == (["tool_name_list_unwrapped"], "tool_use", "calculator")
    assert simulate_planner({"action": "retrieve", "tool_name": None}, TOOLS) == ([], "retrieve", None)


def run(it, planner_fn, tool_fn=None, recorder=None):
    recorder = recorder or FakeRecorder()
    return asyncio.run(run_item(it, 0, planner_fn, tool_fn or tool(""), recorder, TOOLS))


def test_run_item_pass_with_tool_check():
    rec = FakeRecorder()
    raw = {"action": "tool_use", "tool_name": "calculator"}
    result = run(CALC, planner(rec, raw, out("tool_use", "calculator", "3*7")), tool("21"), rec)
    assert result.verdict == "pass" and result.raw_verdict == "pass"
    assert result.tool_check == "pass" and result.tool_output == "21"
    assert result.safeguards == [] and result.safeguard_mismatch is False
    assert result.latency_ms is not None


def test_run_item_tool_check_fails_when_calculator_errors():
    rec = FakeRecorder()
    raw = {"action": "tool_use", "tool_name": "calculator"}
    result = run(item("s", "tool_use", "calculator", output="16"),
                 planner(rec, raw, out("tool_use", "calculator", "sqrt(256)")),
                 tool("Error: Function 'sqrt' not defined"), rec)
    assert result.verdict == "pass" and result.tool_check == "fail"


def test_run_item_tool_check_skipped_when_not_routed_to_calculator():
    rec = FakeRecorder()
    raw = {"action": "tool_use", "tool_name": "web_search"}
    result = run(CALC, planner(rec, raw, out("tool_use", "web_search", "q")), tool("x"), rec)
    assert result.verdict == "wrong_tool"
    assert result.tool_check.startswith("skipped")


def test_run_item_planner_failure_is_not_scored():
    rec = FakeRecorder()
    result = run(item(action="retrieve"), planner(rec, None, out("retrieve", plan=[FALLBACK])), recorder=rec)
    assert result.verdict == "planner_failed"


def test_run_item_invalid_json_is_a_model_failure():
    rec = FakeRecorder()
    result = run(item(action="direct_answer"),
                 planner(rec, {"_error": "No JSON object found"}, out("retrieve", plan=[FALLBACK])), recorder=rec)
    assert result.verdict == "wrong_action" and result.raw_verdict == "invalid"
    assert result.safeguards == ["raw_invalid_json"] and result.safeguard_mismatch is False


def test_run_item_flags_safeguard_simulation_drift():
    rec = FakeRecorder()
    result = run(CALC, planner(rec, {"action": "retrieve", "tool_name": None}, out("tool_use", "calculator")), recorder=rec)
    assert result.safeguard_mismatch is True


def test_run_item_records_exceptions():
    async def boom(state):
        raise RuntimeError("down")

    result = run(item(), boom)
    assert result.verdict == "error" and "RuntimeError: down" in result.error


def test_run_routing_repeats_each_item():
    rec = FakeRecorder()
    items = [item("a"), item("b")]
    results = asyncio.run(run_routing(items, 2, planner(rec, {"action": "direct_answer", "tool_name": None},
                                                       out("direct_answer")), tool(""), rec, TOOLS))
    assert [(r.item_id, r.repeat) for r in results] == [("a", 0), ("a", 1), ("b", 0), ("b", 1)]


def result(item_id, verdict, action, tool_name=None, raw=None, fired=(), check=None, repeat=0, latency=10.0):
    return RoutingResult(item_id=item_id, repeat=repeat, verdict=verdict, actual_action=action, actual_tool=tool_name,
                         raw_verdict=raw, safeguards=list(fired), tool_check=check, latency_ms=latency)


def test_summarize_routing_headline_tags_confusion_and_safeguards():
    items = [
        item("a", tags=["greeting"]),
        item("b", "tool_use", "calculator", tags=["arithmetic"], output="1"),
        item("c", "retrieve", tags=["factual"]),
        item("e", "retrieve", tags=["typo"]),
        item("d", "retrieve", tags=["coreference", "known-limitation"]),
    ]
    results = [
        result("a", "pass", "direct_answer", raw="pass"),
        result("b", "wrong_tool", "tool_use", "web_search", raw="wrong_tool", check="skipped: routed elsewhere"),
        result("c", "wrong_action", "direct_answer", raw="wrong_action"),
        result("e", "pass", "retrieve", raw="wrong_action", fired=["invalid_action"]),
        result("d", "wrong_action", "direct_answer", raw="wrong_action"),
        result("f", "planner_failed", "retrieve"),
    ]
    s = summarize_routing(items, results)
    assert s["headline"]["pass_rate"] == {"successes": 2, "n": 4}
    assert s["headline"]["action_accuracy"] == {"successes": 3, "n": 4}
    assert s["headline"]["tool_accuracy"] == {"successes": 0, "n": 1}
    assert s["per_tag"]["greeting"] == {"successes": 1, "n": 1}
    assert s["per_tag"]["known-limitation"] == {"successes": 0, "n": 1}
    assert s["confusion"]["retrieve"]["direct_answer"] == 2 and s["confusion"]["retrieve"]["retrieve"] == 1
    assert s["confusion"]["direct_answer"]["direct_answer"] == 1 and s["confusion"]["tool_use"]["tool_use"] == 1
    assert s["per_tool"]["calculator"] == {"precision": None, "recall": 0.0}
    assert s["per_tool"]["web_search"] == {"precision": 0.0, "recall": None}
    assert s["safeguards"] == {"fired": {"invalid_action": 1}, "value": 0.25, "mismatches": 0}
    assert s["tool_execution"] == {"pass": 0, "fail": 0, "skipped": 1}
    assert s["planner_failed"]["count"] == 1 and s["invalid"] is False
    assert s["stability"] == {"value": None, "unstable": []}
    assert s["latency_ms"] == {"p50": 10.0, "p95": 10.0}


def test_summarize_routing_marks_run_invalid_when_planner_mostly_failed():
    items = [item("a"), item("b")]
    results = [result("a", "planner_failed", "retrieve"), result("b", "pass", "direct_answer", raw="pass")]
    s = summarize_routing(items, results)
    assert s["planner_failed"]["rate"] == 0.5 and s["invalid"] is True


def test_summarize_routing_reports_unstable_items():
    items = [item("a"), item("b")]
    results = [
        result("a", "pass", "direct_answer", raw="pass", repeat=0),
        result("a", "wrong_action", "retrieve", raw="wrong_action", repeat=1),
        result("b", "pass", "direct_answer", raw="pass", repeat=0),
        result("b", "pass", "direct_answer", raw="pass", repeat=1),
    ]
    s = summarize_routing(items, results)
    assert s["stability"] == {"value": 0.5, "unstable": ["a"]}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/eval_harness/test_routing.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.routing'`

- [ ] **Step 3: Write the implementation**

Create `eval/harness/routing.py`:

```python
import re
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

from eval.harness.datasets import ACTIONS, TOOLS
from eval.harness.stats import percentile

# Prefix of the plan text planner_node emits from its blanket except; the CLI verifies it still exists in the source.
FALLBACK_PLAN_PREFIX = "Error in planning"
INVALID_RUN_PLANNER_FAILED_RATE = 0.20
KNOWN_LIMITATION_TAG = "known-limitation"
SCORED = ("pass", "wrong_action", "wrong_tool")


@dataclass
class RoutingResult:
    item_id: str
    repeat: int
    verdict: str
    actual_action: Optional[str] = None
    actual_tool: Optional[str] = None
    raw_plan: Optional[dict] = None
    raw_verdict: Optional[str] = None
    safeguards: list = field(default_factory=list)
    safeguard_mismatch: bool = False
    tool_check: Optional[str] = None
    tool_output: Optional[str] = None
    latency_ms: Optional[float] = None
    error: Optional[str] = None


def _tool_of(value):
    if isinstance(value, list):
        value = value[0] if value else ""
    return value or None


def judge(item, action, tool):
    acceptable = {(item.expected_action, item.expected_tool), *item.also_accept}
    if (action, tool) in acceptable:
        return "pass"
    if action in {a for a, _ in acceptable}:
        return "wrong_tool"
    return "wrong_action"


def raw_verdict(item, raw_plan):
    if raw_plan is None or "_error" in raw_plan:
        return "invalid"
    action = raw_plan.get("action", "retrieve")
    if action not in ACTIONS:
        return "wrong_action"
    return judge(item, action, _tool_of(raw_plan.get("tool_name")))


def simulate_planner(raw_plan, known_tools):
    if "_error" in raw_plan:
        return ["raw_invalid_json"], "retrieve", None
    fired = []
    action = raw_plan.get("action", "retrieve")
    tool = raw_plan.get("tool_name") or ""
    if isinstance(tool, list):
        fired.append("tool_name_list_unwrapped")
        tool = tool[0] if tool else ""
    if action not in ACTIONS:
        fired.append("invalid_action")
        action, tool = "retrieve", ""
    if tool and tool in known_tools and action != "tool_use":
        fired.append("tool_promoted")
        action = "tool_use"
    if action == "tool_use" and tool not in known_tools:
        fired.append("hallucinated_tool")
        action, tool = "retrieve", ""
    if action == "direct_answer" and tool and tool not in known_tools:
        fired.append("direct_answer_bad_tool")
        action, tool = "retrieve", ""
    return fired, action, tool or None


async def _check_tool(item, action, tool, planned, tool_fn):
    if action != "tool_use" or tool != "calculator":
        return f"skipped: routed to {action}/{tool}, only calculator is executed", None
    try:
        executed = await tool_fn({"tool_name": tool, "tool_input": planned.get("tool_input", "")})
        output = executed["documents"][0]
        return ("pass" if re.search(item.expected_tool_output, output) else "fail"), output
    except Exception as e:
        return "fail", f"{type(e).__name__}: {e}"


async def run_item(item, repeat, planner_fn, tool_fn, recorder, known_tools):
    recorder.reset()
    state = {"messages": [{"role": "user", "content": item.query}], "current_query": item.query,
             "action": "retrieve", "tool_name": "", "tool_input": "", "documents": [], "plan": []}
    started = time.perf_counter()
    try:
        planned = await planner_fn(state)
    except Exception as e:
        return RoutingResult(item.id, repeat, "error", error=f"{type(e).__name__}: {e}",
                             latency_ms=(time.perf_counter() - started) * 1000)
    latency_ms = (time.perf_counter() - started) * 1000
    raw = recorder.last()
    action = planned.get("action")
    tool = planned.get("tool_name") or None
    if " ".join(planned.get("plan") or []).startswith(FALLBACK_PLAN_PREFIX) and raw is None:
        return RoutingResult(item.id, repeat, "planner_failed", action, tool, latency_ms=latency_ms)
    fired, sim_action, sim_tool = simulate_planner(raw, known_tools) if raw is not None else ([], action, tool)
    tool_check = tool_output = None
    if item.expected_tool_output is not None:
        tool_check, tool_output = await _check_tool(item, action, tool, planned, tool_fn)
    return RoutingResult(
        item_id=item.id, repeat=repeat, verdict=judge(item, action, tool), actual_action=action, actual_tool=tool,
        raw_plan=raw, raw_verdict=raw_verdict(item, raw) if raw is not None else None, safeguards=fired,
        safeguard_mismatch=(sim_action, sim_tool) != (action, tool), tool_check=tool_check,
        tool_output=tool_output, latency_ms=latency_ms,
    )


async def run_routing(items, repeats, planner_fn, tool_fn, recorder, known_tools):
    results = []
    for item in items:
        for repeat in range(repeats):
            results.append(await run_item(item, repeat, planner_fn, tool_fn, recorder, known_tools))
    return results


def _rate(successes, n):
    return {"successes": successes, "n": n}


def summarize_routing(items, results):
    by_id = {i.id: i for i in items}
    scored = [r for r in results if r.verdict in SCORED]
    rows = [r for r in scored if KNOWN_LIMITATION_TAG not in by_id[r.item_id].tags]

    def action_ok(r):
        return r.verdict in ("pass", "wrong_tool")

    headline = {
        "pass_rate": _rate(sum(r.verdict == "pass" for r in rows), len(rows)),
        "action_accuracy": _rate(sum(action_ok(r) for r in rows), len(rows)),
    }
    tool_rows = [r for r in rows if by_id[r.item_id].expected_action == "tool_use" and action_ok(r)]
    if tool_rows:
        headline["tool_accuracy"] = _rate(sum(r.verdict == "pass" for r in tool_rows), len(tool_rows))

    per_tag = defaultdict(lambda: {"successes": 0, "n": 0})
    for r in scored:
        for tag in by_id[r.item_id].tags:
            per_tag[tag]["n"] += 1
            per_tag[tag]["successes"] += r.verdict == "pass"

    confusion = {a: {b: 0 for b in ACTIONS} for a in ACTIONS}
    for r in scored:
        if r.actual_action in ACTIONS:
            confusion[by_id[r.item_id].expected_action][r.actual_action] += 1

    per_tool = {}
    for name in TOOLS:
        hits = sum(1 for r in scored if by_id[r.item_id].expected_tool == name and r.actual_tool == name)
        predicted = sum(1 for r in scored if r.actual_tool == name)
        expected = sum(1 for r in scored if by_id[r.item_id].expected_tool == name)
        per_tool[name] = {"precision": hits / predicted if predicted else None,
                          "recall": hits / expected if expected else None}

    with_raw = [r for r in rows if r.raw_verdict is not None]
    value = None
    if with_raw:
        value = (sum(r.verdict == "pass" for r in with_raw) - sum(r.raw_verdict == "pass" for r in with_raw)) / len(with_raw)

    verdicts = defaultdict(list)
    for r in results:
        verdicts[r.item_id].append(r.verdict)
    repeated = {k: v for k, v in verdicts.items() if len(v) > 1}
    unstable = sorted(k for k, v in repeated.items() if len(set(v)) > 1)
    stability = 1 - len(unstable) / len(repeated) if repeated else None

    tool_execution = {"pass": 0, "fail": 0, "skipped": 0}
    for r in results:
        if r.tool_check is not None:
            tool_execution["skipped" if r.tool_check.startswith("skipped") else r.tool_check] += 1

    failed = sum(r.verdict == "planner_failed" for r in results)
    failed_rate = failed / len(results) if results else 0.0
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return {
        "headline": headline,
        "per_tag": dict(per_tag),
        "confusion": confusion,
        "per_tool": per_tool,
        "safeguards": {"fired": dict(Counter(n for r in results for n in r.safeguards)), "value": value,
                       "mismatches": sum(r.safeguard_mismatch for r in results)},
        "stability": {"value": stability, "unstable": unstable},
        "tool_execution": tool_execution,
        "planner_failed": {"count": failed, "rate": failed_rate},
        "errors": sum(r.verdict == "error" for r in results),
        "invalid": failed_rate > INVALID_RUN_PLANNER_FAILED_RATE,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "n_items": len(items),
        "n_results": len(results),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/eval_harness/test_routing.py -q`
Expected: `14 passed`

If `test_summarize_routing_headline_tags_confusion_and_safeguards` fails on `per_tool`, re-check the hand-computed expectations: item `b` expects calculator but was routed to `web_search`, so calculator has recall 0.0 and no predictions (precision `None`), and web_search has precision 0.0 and no expected items (recall `None`).

- [ ] **Step 5: Commit (if authorised)**

```bash
git add eval/harness/routing.py tests/eval_harness/test_routing.py
git commit -m "feat(eval): add routing runner and scorer

Scores planner routing per verdict, separates infrastructure failures
(planner_failed) from model failures, and reports raw-vs-coerced accuracy so
the value of the deterministic safeguards is a number rather than a claim."
```

---

### Task 5: Retrieval runner and scorer

**Files:**
- Create: `eval/harness/retrieval.py`, `tests/eval_harness/test_retrieval.py`

**Interfaces:**
- Consumes: `QAItem` (Task 1); `auroc`, `containment`, `percentile` (Task 2). Recorded chunks are dicts `{"filename", "page", "score", "text"}` in rank order (Task 3). A recorder needs only `reset()` and `last()`.
- Produces (used by Tasks 7, 8):
  - Constants `K_VALUES = (1, 3, 5)`, `DEFAULT_FUZZY_THRESHOLD = 0.5`, `FUZZY_RELIABLE_AGREEMENT = 0.85`, `UNANSWERABLE_MIN_N = 20`.
  - `RetrievalResult` dataclass: `item_id, verdict ("scored" | "unscorable" | "error"), ranked (list of chunk dicts), documents_n, vector_docs, graph_docs, top1_score, latency_ms, reason`.
  - `arm_counts(documents) -> (vector, graph)`, `hit_at_k(filenames, expected, k) -> bool`, `reciprocal_rank(filenames, expected) -> float`, `source_recall(filenames, expected, k=5) -> float`, `chunk_relevant(chunk, contexts, threshold) -> bool`, `fuzzy_hit_at_k(chunks, contexts, threshold, k) -> bool`.
  - `async run_retrieval(items, retrieve_fn, recorder, present_sources) -> list[RetrievalResult]`. A source item with any expected source missing from `present_sources` is `unscorable` and the retriever is not called.
  - `calibrate_threshold(items, results, candidates=CALIBRATION_CANDIDATES) -> (threshold, agreement | None, n_dual_items)`. Agreement is measured chunk by chunk over each dual-labelled item's top 5; ties go to the higher threshold; with no dual items it returns `(DEFAULT_FUZZY_THRESHOLD, None, 0)`.
  - `summarize_retrieval(items, results, threshold, agreement, n_calibration) -> dict` with keys `headline` (`source_hit@1/3/5` as `{"successes", "n"}`, only if there are scored source items), `groups` (keyed `"<label_type or unanswerable>/<origin>"`), `fuzzy`, `unanswerable`, `empty_context_rate`, `arms`, `unscorable` (item ids), `errors`, `invalid` (always `False`), `latency_ms`, `n_items`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_retrieval.py`:

```python
import asyncio

import pytest

from eval.harness.datasets import QAItem
from eval.harness.retrieval import (
    RetrievalResult, arm_counts, calibrate_threshold, hit_at_k, reciprocal_rank, run_retrieval,
    source_recall, summarize_retrieval,
)


def qa(id, origin="hand", label="source", sources=("a.pdf",), contexts=(), calib=(), answerable=True):
    return QAItem(id=id, query="q", origin=origin, answerable=answerable,
                  label_type=label if answerable else None,
                  expected_sources=tuple(sources) if label == "source" and answerable else (),
                  reference_contexts=tuple(contexts), calibration_sources=tuple(calib),
                  ground_truth=None, tags=())


class FakeRecorder:
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def last(self):
        return self.value


def chunks(rows):
    return [{"filename": f, "page": 1, "score": s, "text": t} for f, s, t in rows]


def retriever(recorder, rows, documents):
    async def fn(state):
        recorder.value = chunks(rows)
        return {"documents": documents}
    return fn


def scored(item_id, rows):
    ranked = chunks(rows)
    return RetrievalResult(item_id=item_id, verdict="scored", ranked=ranked, documents_n=len(ranked),
                           vector_docs=len(ranked), top1_score=ranked[0]["score"] if ranked else None, latency_ms=5.0)


def test_arm_counts_separates_vector_and_graph_documents():
    docs = ["text [Source: a.pdf]", "Kubernetes RELATES_TO Pods", "multi\nline [Source: b.pdf]"]
    assert arm_counts(docs) == (2, 1)
    assert arm_counts([]) == (0, 0)


def test_rank_metrics():
    names = ["x.pdf", "a.pdf", "y.pdf", "b.pdf"]
    expected = {"a.pdf", "b.pdf"}
    assert [hit_at_k(names, expected, k) for k in (1, 3, 5)] == [False, True, True]
    assert reciprocal_rank(names, expected) == 0.5
    assert reciprocal_rank(["x.pdf"], expected) == 0.0
    assert source_recall(names, expected, k=3) == 0.5
    assert source_recall(names, expected, k=5) == 1.0


def test_run_retrieval_scores_item_and_counts_arms():
    rec = FakeRecorder()
    fn = retriever(rec, [("a.pdf", 0.9, "t"), ("x.pdf", 0.4, "t")], ["t [Source: a.pdf]", "A REL B"])
    [result] = asyncio.run(run_retrieval([qa("s1")], fn, rec, {"a.pdf"}))
    assert result.verdict == "scored" and result.top1_score == 0.9
    assert (result.documents_n, result.vector_docs, result.graph_docs) == (2, 1, 1)
    assert [c["filename"] for c in result.ranked] == ["a.pdf", "x.pdf"]


def test_run_retrieval_marks_missing_sources_unscorable_without_searching():
    async def must_not_run(state):
        raise AssertionError("retriever should not be called")

    [result] = asyncio.run(run_retrieval([qa("s1", sources=("a.pdf", "gone.pdf"))], must_not_run, FakeRecorder(), {"a.pdf"}))
    assert result.verdict == "unscorable" and "gone.pdf" in result.reason


def test_run_retrieval_records_errors():
    async def boom(state):
        raise RuntimeError("qdrant down")

    [result] = asyncio.run(run_retrieval([qa("s1")], boom, FakeRecorder(), {"a.pdf"}))
    assert result.verdict == "error" and "RuntimeError: qdrant down" in result.reason


REFERENCE = "Horizontal Pod Autoscaler adjusts replicas"


def dual_item():
    return qa("f1", "golden", "fuzzy", contexts=[REFERENCE], calib=["hpa.html"])


def test_calibrate_threshold_prefers_highest_threshold_on_ties():
    result = scored("f1", [("hpa.html", 0.9, "The horizontal pod autoscaler adjusts the number of replicas"),
                           ("x.pdf", 0.5, "assembly language registers")])
    assert calibrate_threshold([dual_item()], [result]) == (0.9, 1.0, 1)


def test_calibrate_threshold_finds_boundary():
    result = scored("f1", [("hpa.html", 0.9, "horizontal pod autoscaler"), ("x.pdf", 0.5, "assembly language registers")])
    threshold, agreement, n = calibrate_threshold([dual_item()], [result])
    assert threshold == 0.6 and agreement == 1.0 and n == 1


def test_calibrate_threshold_without_dual_items():
    plain = qa("f2", "golden", "fuzzy", contexts=[REFERENCE])
    assert calibrate_threshold([plain], [scored("f2", [("a.pdf", 0.9, "t")])]) == (0.5, None, 0)


def summary_inputs():
    items = [
        qa("s1"), qa("s2", sources=("b.pdf",)), qa("s3", "synthetic", sources=("c.pdf",)),
        qa("f1", "golden", "fuzzy", contexts=[REFERENCE]), qa("u1", answerable=False, label=None),
    ]
    results = [
        scored("s1", [("a.pdf", 0.9, "t"), ("x.pdf", 0.5, "t")]),
        scored("s2", [("x.pdf", 0.8, "t"), ("y.pdf", 0.7, "t"), ("b.pdf", 0.6, "t")]),
        RetrievalResult(item_id="s3", verdict="unscorable", reason="sources not ingested: ['c.pdf']"),
        scored("f1", [("k8s.html", 0.7, "horizontal pod autoscaler adjusts replicas")]),
        scored("u1", [("x.pdf", 0.4, "t")]),
    ]
    return items, results


def test_summarize_retrieval():
    items, results = summary_inputs()
    s = summarize_retrieval(items, results, 0.5, 0.9, 3)
    assert s["headline"] == {"source_hit@1": {"successes": 1, "n": 2}, "source_hit@3": {"successes": 2, "n": 2},
                             "source_hit@5": {"successes": 2, "n": 2}}
    hand = s["groups"]["source/hand"]
    assert hand["n"] == 2 and hand["hit@1"] == 0.5 and hand["hit@3"] == 1.0
    assert hand["mrr"] == pytest.approx((1 + 1 / 3) / 2) and hand["source_recall@5"] == 1.0
    assert "source/synthetic" not in s["groups"] and s["unscorable"] == ["s3"]
    assert s["groups"]["fuzzy/golden"]["hit@1"] == 1.0
    assert s["groups"]["unanswerable/hand"]["mean_top1_score"] == 0.4
    assert s["fuzzy"] == {"n": 1, "threshold": 0.5, "agreement": 0.9, "n_calibration": 3, "reliable": True}
    assert s["unanswerable"] == {"n_answerable": 3, "n_unanswerable": 1, "auroc": 1.0, "cutoff": 0.7,
                                 "descriptive_only": True}
    assert s["empty_context_rate"] == 0.0 and s["arms"] == {"vector_docs": 7, "graph_docs": 0}
    assert s["latency_ms"] == {"p50": 5.0, "p95": 5.0} and s["errors"] == 0 and s["invalid"] is False


def test_summarize_retrieval_flags_low_agreement_as_unreliable():
    items, results = summary_inputs()
    assert summarize_retrieval(items, results, 0.5, 0.5, 3)["fuzzy"]["reliable"] is False
    assert summarize_retrieval(items, results, 0.5, None, 0)["fuzzy"]["reliable"] is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/eval_harness/test_retrieval.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.retrieval'`

- [ ] **Step 3: Write the implementation**

Create `eval/harness/retrieval.py`:

```python
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from eval.harness.stats import auroc, containment, percentile

K_VALUES = (1, 3, 5)
DEFAULT_FUZZY_THRESHOLD = 0.5
FUZZY_RELIABLE_AGREEMENT = 0.85
UNANSWERABLE_MIN_N = 20
CALIBRATION_CANDIDATES = tuple(round(0.1 + 0.05 * i, 2) for i in range(17))
_SOURCE_SUFFIX = re.compile(r"\[Source: .*\]\s*$")


@dataclass
class RetrievalResult:
    item_id: str
    verdict: str
    ranked: list = field(default_factory=list)
    documents_n: int = 0
    vector_docs: int = 0
    graph_docs: int = 0
    top1_score: Optional[float] = None
    latency_ms: Optional[float] = None
    reason: Optional[str] = None


def arm_counts(documents):
    vector = sum(1 for d in documents if _SOURCE_SUFFIX.search(d))
    return vector, len(documents) - vector


def hit_at_k(filenames, expected, k):
    return any(f in expected for f in filenames[:k])


def reciprocal_rank(filenames, expected):
    for rank, name in enumerate(filenames, start=1):
        if name in expected:
            return 1.0 / rank
    return 0.0


def source_recall(filenames, expected, k=5):
    return len({f for f in filenames[:k] if f in expected}) / len(set(expected))


def chunk_relevant(chunk, contexts, threshold):
    return max((containment(ref, chunk["text"]) for ref in contexts), default=0.0) >= threshold


def fuzzy_hit_at_k(chunks, contexts, threshold, k):
    return any(chunk_relevant(c, contexts, threshold) for c in chunks[:k])


async def run_retrieval(items, retrieve_fn, recorder, present_sources):
    results = []
    for item in items:
        if item.label_type == "source":
            missing = [s for s in item.expected_sources if s not in present_sources]
            if missing:
                results.append(RetrievalResult(item_id=item.id, verdict="unscorable",
                                               reason=f"sources not ingested: {missing}"))
                continue
        recorder.reset()
        started = time.perf_counter()
        try:
            out = await retrieve_fn({"current_query": item.query})
        except Exception as e:
            results.append(RetrievalResult(item_id=item.id, verdict="error", reason=f"{type(e).__name__}: {e}",
                                           latency_ms=(time.perf_counter() - started) * 1000))
            continue
        latency_ms = (time.perf_counter() - started) * 1000
        ranked = recorder.last() or []
        documents = out.get("documents", [])
        vector, graph = arm_counts(documents)
        results.append(RetrievalResult(
            item_id=item.id, verdict="scored", ranked=ranked, documents_n=len(documents), vector_docs=vector,
            graph_docs=graph, top1_score=ranked[0]["score"] if ranked else None, latency_ms=latency_ms))
    return results


def calibrate_threshold(items, results, candidates=CALIBRATION_CANDIDATES):
    scored = {r.item_id: r for r in results if r.verdict == "scored"}
    dual = [(i, scored[i.id]) for i in items if i.label_type == "fuzzy" and i.calibration_sources and i.id in scored]
    pairs = [(item, chunk) for item, res in dual for chunk in res.ranked[:5]]
    if not pairs:
        return DEFAULT_FUZZY_THRESHOLD, None, len(dual)
    best_threshold, best_rate = candidates[0], -1.0
    for threshold in candidates:
        agree = sum(chunk_relevant(c, item.reference_contexts, threshold) == (c["filename"] in item.calibration_sources)
                    for item, c in pairs)
        rate = agree / len(pairs)
        if rate >= best_rate:
            best_threshold, best_rate = threshold, rate
    return best_threshold, best_rate, len(dual)


def _mean(values):
    return sum(values) / len(values) if values else None


def _youden_cutoff(positive, negative):
    best, best_j = None, -1.0
    for t in sorted(set(positive + negative)):
        j = sum(p >= t for p in positive) / len(positive) - sum(n >= t for n in negative) / len(negative)
        if j > best_j:
            best, best_j = t, j
    return best


def summarize_retrieval(items, results, threshold, agreement, n_calibration):
    by_id = {i.id: i for i in items}
    scored = [(by_id[r.item_id], r) for r in results if r.verdict == "scored"]

    def names(r):
        return [c["filename"] for c in r.ranked]

    source = [(i, r) for i, r in scored if i.label_type == "source"]
    headline = {}
    if source:
        for k in K_VALUES:
            headline[f"source_hit@{k}"] = {
                "successes": sum(hit_at_k(names(r), set(i.expected_sources), k) for i, r in source), "n": len(source)}

    grouped = defaultdict(list)
    for i, r in scored:
        grouped[f"{i.label_type or 'unanswerable'}/{i.origin}"].append((i, r))
    groups = {}
    for key, rows in sorted(grouped.items()):
        entry = {"n": len(rows)}
        label = key.split("/")[0]
        if label == "source":
            for k in K_VALUES:
                entry[f"hit@{k}"] = sum(hit_at_k(names(r), set(i.expected_sources), k) for i, r in rows) / len(rows)
            entry["mrr"] = _mean([reciprocal_rank(names(r), set(i.expected_sources)) for i, r in rows])
            entry["source_recall@5"] = _mean([source_recall(names(r), set(i.expected_sources)) for i, r in rows])
        elif label == "fuzzy":
            for k in K_VALUES:
                entry[f"hit@{k}"] = sum(fuzzy_hit_at_k(r.ranked, i.reference_contexts, threshold, k)
                                        for i, r in rows) / len(rows)
        else:
            entry["mean_top1_score"] = _mean([r.top1_score for _, r in rows if r.top1_score is not None])
        groups[key] = entry

    positives = [r.top1_score for i, r in scored if i.answerable and r.top1_score is not None]
    negatives = [r.top1_score for i, r in scored if not i.answerable and r.top1_score is not None]
    latencies = [r.latency_ms for r in results if r.latency_ms is not None]
    return {
        "headline": headline,
        "groups": groups,
        "fuzzy": {"n": sum(1 for i, _ in scored if i.label_type == "fuzzy"), "threshold": threshold,
                  "agreement": agreement, "n_calibration": n_calibration,
                  "reliable": agreement is not None and agreement >= FUZZY_RELIABLE_AGREEMENT},
        "unanswerable": {"n_answerable": len(positives), "n_unanswerable": len(negatives),
                         "auroc": auroc(positives, negatives),
                         "cutoff": _youden_cutoff(positives, negatives) if positives and negatives else None,
                         "descriptive_only": len(negatives) < UNANSWERABLE_MIN_N},
        "empty_context_rate": sum(r.documents_n == 0 for _, r in scored) / len(scored) if scored else None,
        "arms": {"vector_docs": sum(r.vector_docs for _, r in scored), "graph_docs": sum(r.graph_docs for _, r in scored)},
        "unscorable": [r.item_id for r in results if r.verdict == "unscorable"],
        "errors": sum(r.verdict == "error" for r in results),
        "invalid": False,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "n_items": len(items),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/eval_harness/test_retrieval.py -q`
Expected: `10 passed`

- [ ] **Step 5: Commit (if authorised)**

```bash
git add eval/harness/retrieval.py tests/eval_harness/test_retrieval.py
git commit -m "feat(eval): add retrieval runner, scorers and fuzzy calibration

Ranking comes from the recorder because retrieve_node's output is an
unordered set. Source labels score deterministically; fuzzy labels use
lexical overlap calibrated against dual-labelled items so they never
borrow the retriever's own embedding space to grade itself."
```

---

### Task 6: Manifest, preflight and probes

**Files:**
- Create: `eval/harness/manifest.py`, `eval/harness/preflight.py`, `eval/harness/probes.py`, `tests/eval_harness/test_manifest.py`, `tests/eval_harness/test_preflight.py`

**Interfaces:**
- Produces (used by Tasks 7, 8):
  - `manifest.py`: `COMPARABLE_KEYS = ("model", "embed_model", "dataset_hashes", "corpus_hash")`; `endpoint_host(url) -> str`; `planner_prompt_hash(system_prompt) -> str`; `git_info(run=subprocess.run) -> (sha: str, dirty: bool)` (dirty ignores untracked files); `build_manifest(*, layer, model, endpoint, embed_model, embed_dim, prompt_hash, git_sha, git_dirty, dataset_hashes, corpus_hash, corpus_seed, qdrant_points, qdrant_vector_size, neo4j_nodes, k=5, judge=None, now=None) -> dict`; `comparability(a, b) -> list[str]` (the `COMPARABLE_KEYS` on which two manifests differ).
  - `preflight.py`: `PreflightResult(errors, warnings, facts)` with property `ok`; `async run_preflight(*, layer, embed_dim, expected_sources, probes, collection="rag_collection") -> PreflightResult`. `probes` is a dict of async callables: routing needs `llm`; retrieval needs `embed` (returns the measured dimension), `qdrant` (returns `{"exists", "points", "vector_size", "filenames"}`) and `neo4j` (returns a node count or raises). Facts set for retrieval: `embed_dim_actual`, `qdrant_points`, `qdrant_vector_size`, `filenames`, `missing_sources`, `neo4j_nodes`.
  - `probes.py`: `async llm_probe()`, `async embed_probe() -> int`, `async qdrant_probe() -> dict`, `async neo4j_probe() -> int`. These call live services and are verified in Task 12, not by unit tests.

- [ ] **Step 1: Write the failing manifest tests**

Create `tests/eval_harness/test_manifest.py`:

```python
import types

from eval.harness.manifest import (
    COMPARABLE_KEYS, build_manifest, comparability, endpoint_host, git_info, planner_prompt_hash,
)


def test_endpoint_host_drops_path_and_credentials():
    assert endpoint_host("https://user:pw@openrouter.ai/api/v1/chat/completions") == "openrouter.ai"
    assert endpoint_host("http://localhost:11434/api/embeddings") == "localhost"


def test_prompt_hash_changes_with_prompt():
    assert planner_prompt_hash("a") == planner_prompt_hash("a")
    assert planner_prompt_hash("a") != planner_prompt_hash("b") and len(planner_prompt_hash("a")) == 64


def fake_run(outputs):
    calls = []

    def run(cmd, **kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(stdout=outputs[cmd[1]])

    run.calls = calls
    return run


def test_git_info_reports_sha_and_dirty_flag_ignoring_untracked():
    dirty = fake_run({"rev-parse": "abc123\n", "status": " M file.py\n"})
    assert git_info(dirty) == ("abc123", True)
    assert "--untracked-files=no" in dirty.calls[1]
    assert git_info(fake_run({"rev-parse": "abc123\n", "status": ""})) == ("abc123", False)


def manifest(**over):
    fields = dict(layer="routing", model="m", endpoint="https://openrouter.ai/api/v1", embed_model="nomic",
                  embed_dim=768, prompt_hash="p", git_sha="abc", git_dirty=False, dataset_hashes={"routing.jsonl": "h"},
                  corpus_hash=None, corpus_seed=None, qdrant_points=None, qdrant_vector_size=None, neo4j_nodes=None,
                  now="2026-09-21T10:00:00+00:00")
    fields.update(over)
    return build_manifest(**fields)


def test_build_manifest_records_fields_and_defaults():
    m = manifest()
    assert m["endpoint_host"] == "openrouter.ai" and "endpoint" not in m
    assert m["k"] == 5 and m["judge"] is None and m["started_at"] == "2026-09-21T10:00:00+00:00"
    assert m["planner_prompt_hash"] == "p" and m["dataset_hashes"] == {"routing.jsonl": "h"}


def test_comparability_flags_only_comparable_keys():
    base = manifest()
    assert comparability(base, manifest(git_sha="def", prompt_hash="q")) == []
    assert comparability(base, manifest(model="other", corpus_hash="c")) == ["model", "corpus_hash"]
    assert set(COMPARABLE_KEYS) == {"model", "embed_model", "dataset_hashes", "corpus_hash"}
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/eval_harness/test_manifest.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.manifest'`

- [ ] **Step 3: Write `eval/harness/manifest.py`**

```python
import hashlib
import subprocess
from datetime import datetime, timezone
from urllib.parse import urlparse

COMPARABLE_KEYS = ("model", "embed_model", "dataset_hashes", "corpus_hash")


def endpoint_host(url):
    return urlparse(url).hostname or ""


def planner_prompt_hash(system_prompt):
    return hashlib.sha256(system_prompt.encode()).hexdigest()


def git_info(run=subprocess.run):
    sha = run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    status = run(["git", "status", "--porcelain", "--untracked-files=no"],
                 capture_output=True, text=True, check=True).stdout.strip()
    return sha, bool(status)


def build_manifest(*, layer, model, endpoint, embed_model, embed_dim, prompt_hash, git_sha, git_dirty,
                   dataset_hashes, corpus_hash, corpus_seed, qdrant_points, qdrant_vector_size, neo4j_nodes,
                   k=5, judge=None, now=None):
    return {
        "layer": layer,
        "started_at": now or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "model": model,
        "endpoint_host": endpoint_host(endpoint),
        "embed_model": embed_model,
        "embed_dim": embed_dim,
        "planner_prompt_hash": prompt_hash,
        "git_sha": git_sha,
        "git_dirty": git_dirty,
        "dataset_hashes": dataset_hashes,
        "corpus_hash": corpus_hash,
        "corpus_seed": corpus_seed,
        "qdrant_points": qdrant_points,
        "qdrant_vector_size": qdrant_vector_size,
        "neo4j_nodes": neo4j_nodes,
        "k": k,
        "judge": judge,
    }


def comparability(a, b):
    return [key for key in COMPARABLE_KEYS if a.get(key) != b.get(key)]
```

- [ ] **Step 4: Run to verify the manifest tests pass**

Run: `python -m pytest tests/eval_harness/test_manifest.py -q`
Expected: `5 passed`

- [ ] **Step 5: Write the failing preflight tests**

Create `tests/eval_harness/test_preflight.py`:

```python
import asyncio

from eval.harness.preflight import run_preflight


def probes(**over):
    async def llm():
        return None

    async def embed():
        return 768

    async def qdrant():
        return {"exists": True, "points": 100, "vector_size": 768, "filenames": {"a.pdf", "b.pdf"}}

    async def neo4j():
        return 5

    base = {"llm": llm, "embed": embed, "qdrant": qdrant, "neo4j": neo4j}
    base.update(over)
    return base


def failing(exc):
    async def fn():
        raise exc
    return fn


def returning(value):
    async def fn():
        return value
    return fn


def preflight(layer="retrieval", sources=("a.pdf",), **over):
    return asyncio.run(run_preflight(layer=layer, embed_dim=768, expected_sources=set(sources), probes=probes(**over)))


def test_routing_needs_only_the_llm():
    result = preflight("routing", qdrant=failing(RuntimeError("must not be probed")), embed=failing(RuntimeError("x")))
    assert result.ok and result.errors == []


def test_routing_reports_llm_failure_with_fix():
    result = preflight("routing", llm=failing(RuntimeError("401")))
    assert not result.ok and "RAY_LLM_ENDPOINT" in result.errors[0] and "401" in result.errors[0]


def test_retrieval_ok_records_facts():
    result = preflight()
    assert result.ok and result.warnings == []
    assert result.facts["qdrant_points"] == 100 and result.facts["neo4j_nodes"] == 5
    assert result.facts["embed_dim_actual"] == 768 and result.facts["missing_sources"] == []


def test_embed_dimension_must_match_setting():
    result = preflight(embed=returning(1024))
    assert any("EMBED_DIM=768" in e and "1024" in e for e in result.errors)


def test_embed_endpoint_failure():
    result = preflight(embed=failing(TimeoutError("slow")))
    assert any("Embed endpoint failed" in e for e in result.errors)


def test_missing_collection_gives_create_command():
    result = preflight(qdrant=returning({"exists": False, "points": 0, "vector_size": None, "filenames": set()}))
    assert any("PUT" in e and "rag_collection" in e for e in result.errors)


def test_vector_size_mismatch():
    result = preflight(qdrant=returning({"exists": True, "points": 5, "vector_size": 1024, "filenames": {"a.pdf"}}))
    assert any("vector size 1024" in e for e in result.errors)


def test_empty_collection():
    result = preflight(qdrant=returning({"exists": True, "points": 0, "vector_size": 768, "filenames": set()}))
    assert any("empty" in e for e in result.errors)


def test_qdrant_unreachable():
    result = preflight(qdrant=failing(ConnectionError("refused")))
    assert any("Qdrant unreachable" in e and "docker compose up -d qdrant" in e for e in result.errors)


def test_missing_sources_are_warnings_not_errors():
    result = preflight(sources=("a.pdf", "gone.pdf"))
    assert result.ok and result.facts["missing_sources"] == ["gone.pdf"]
    assert any("gone.pdf" in w for w in result.warnings)


def test_neo4j_unavailable_and_empty_are_warnings():
    down = preflight(neo4j=failing(ConnectionError("refused")))
    assert down.ok and down.facts["neo4j_nodes"] is None and any("Neo4j unavailable" in w for w in down.warnings)
    empty = preflight(neo4j=returning(0))
    assert empty.ok and any("empty" in w for w in empty.warnings)
```

- [ ] **Step 6: Run to verify failure**

Run: `python -m pytest tests/eval_harness/test_preflight.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.preflight'`

- [ ] **Step 7: Write `eval/harness/preflight.py`**

```python
from dataclasses import dataclass, field


@dataclass
class PreflightResult:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    facts: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.errors


async def run_preflight(*, layer, embed_dim, expected_sources, probes, collection="rag_collection"):
    result = PreflightResult()
    if layer == "routing":
        try:
            await probes["llm"]()
        except Exception as e:
            result.errors.append(f"LLM endpoint failed ({type(e).__name__}: {e}). Check RAY_LLM_ENDPOINT and LLM_API_KEY in .env.")
        return result

    actual_dim = None
    try:
        actual_dim = await probes["embed"]()
    except Exception as e:
        result.errors.append(f"Embed endpoint failed ({type(e).__name__}: {e}). Check RAY_EMBED_ENDPOINT and that Ollama is running.")
    else:
        result.facts["embed_dim_actual"] = actual_dim
        if actual_dim != embed_dim:
            result.errors.append(f"Embed endpoint returns {actual_dim}-dim vectors but EMBED_DIM={embed_dim}. Fix EMBED_DIM in .env.")

    info = None
    try:
        info = await probes["qdrant"]()
    except Exception as e:
        result.errors.append(f"Qdrant unreachable ({type(e).__name__}: {e}). Run: docker compose up -d qdrant")
    if info is not None:
        result.facts.update(qdrant_points=info["points"], qdrant_vector_size=info["vector_size"],
                            filenames=info["filenames"])
        if not info["exists"]:
            result.errors.append(
                f"Collection '{collection}' does not exist. Create it: curl -X PUT localhost:6333/collections/{collection} "
                f"-H 'Content-Type: application/json' -d '{{\"vectors\":{{\"size\":{embed_dim},\"distance\":\"Cosine\"}}}}'")
        elif actual_dim is not None and info["vector_size"] != actual_dim:
            result.errors.append(
                f"Collection '{collection}' has vector size {info['vector_size']} but embeddings are {actual_dim}-dim. "
                "Delete and recreate the collection, then re-ingest.")
        elif info["points"] == 0:
            result.errors.append(f"Collection '{collection}' is empty. Ingest the corpus first.")
        else:
            missing = sorted(set(expected_sources) - info["filenames"])
            result.facts["missing_sources"] = missing
            if missing:
                result.warnings.append(f"{len(missing)} expected source(s) not ingested; their items will be unscorable: {missing}")

    try:
        nodes = await probes["neo4j"]()
    except Exception:
        nodes = None
    result.facts["neo4j_nodes"] = nodes
    if nodes is None:
        result.warnings.append("Neo4j unavailable: the graph arm returns no documents.")
    elif nodes == 0:
        result.warnings.append("Neo4j is up but empty: the graph arm returns no documents.")
    return result
```

- [ ] **Step 8: Run to verify the preflight tests pass**

Run: `python -m pytest tests/eval_harness/test_preflight.py -q`
Expected: `11 passed`

- [ ] **Step 9: Write `eval/harness/probes.py` (live services; not unit-tested)**

```python
import httpx

from eval.harness.recorders import normalize_payload


async def llm_probe():
    from services.api.app.clients.ray_llm import llm_client

    await llm_client.chat_completion(
        messages=[{"role": "user", "content": "Reply with the word ok."}], temperature=0.0, max_tokens=8)


async def embed_probe():
    from services.api.app.config import settings

    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(settings.RAY_EMBED_ENDPOINT,
                                     json={"model": settings.EMBED_MODEL_NAME, "prompt": "warm-up"})
        response.raise_for_status()
        return len(response.json()["embedding"])


async def qdrant_probe():
    from services.api.app.clients.qdrant import qdrant_client
    from services.api.app.config import settings

    client, name = qdrant_client.client, settings.QDRANT_COLLECTION
    existing = {c.name for c in (await client.get_collections()).collections}
    if name not in existing:
        return {"exists": False, "points": 0, "vector_size": None, "filenames": set()}
    info = await client.get_collection(name)
    filenames, offset = set(), None
    while True:
        points, offset = await client.scroll(
            collection_name=name, limit=256, offset=offset, with_payload=["filename", "metadata"], with_vectors=False)
        filenames.update(normalize_payload(p.payload)[0] for p in points)
        if offset is None:
            break
    return {"exists": True, "points": info.points_count,
            "vector_size": getattr(info.config.params.vectors, "size", None), "filenames": filenames}


async def neo4j_probe():
    from services.api.app.clients.neo4j import neo4j_client

    rows = await neo4j_client.query("MATCH (n) RETURN count(n) AS c")
    return rows[0]["c"]
```

- [ ] **Step 10: Check the probes module imports cleanly**

Run: `python -c "import eval.harness.probes as p; print(sorted(n for n in dir(p) if n.endswith('_probe')))"`
Expected: `['embed_probe', 'llm_probe', 'neo4j_probe', 'qdrant_probe']`

- [ ] **Step 11: Commit (if authorised)**

```bash
git add eval/harness/manifest.py eval/harness/preflight.py eval/harness/probes.py tests/eval_harness/test_manifest.py tests/eval_harness/test_preflight.py
git commit -m "feat(eval): add run manifest, layer-specific preflight and live probes

A run is only comparable to a baseline if the model, datasets and corpus
match, so the manifest records them. Preflight fails fast with the fix
command, because scoring an empty or wrong-dimension collection produces
plausible-looking numbers that mean nothing."
```

---

### Task 7: Report writer, baselines and comparison

**Files:**
- Create: `eval/harness/report.py`, `tests/eval_harness/test_report.py`

**Interfaces:**
- Consumes: `comparability` (Task 6); `wilson_interval`, `intervals_overlap` (Task 2); `ACTIONS` (Task 1); the summary dicts from `summarize_routing` and `summarize_retrieval` (Tasks 4, 5); result rows are `dataclasses.asdict(result)` dicts.
- Produces (used by Task 8):
  - `class ComparabilityError(RuntimeError)`.
  - `save_baseline(path, manifest, summary)` writes `{"manifest", "headline"}`; `load_baseline(path) -> dict`.
  - `check_comparable(manifest, baseline_manifest, force=False) -> list[str]` returns the differing comparable keys, or raises `ComparabilityError` if any differ and `force` is false.
  - `compare_headline(current: dict, baseline: dict) -> list[dict]`; each row has `metric, baseline, current, delta, within_noise` (Wilson intervals overlap). Metrics missing from the baseline, or with `n == 0`, are skipped.
  - `failure_rows(layer, items, rows, limit=25) -> list[dict]` with keys `id, query, expected, actual, reason`.
  - `render_summary_md(layer, manifest, summary, warnings, failures, comparison=None) -> str`.
  - `write_run(out_dir, manifest, rows, summary, markdown)` writes `manifest.json`, `summary.json`, `results.jsonl`, `summary.md`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_report.py`:

```python
import json

import pytest

from eval.harness.datasets import QAItem, RoutingItem
from eval.harness.report import (
    ComparabilityError, check_comparable, compare_headline, failure_rows, load_baseline, render_summary_md,
    save_baseline, write_run,
)
from eval.harness.retrieval import RetrievalResult, summarize_retrieval
from eval.harness.routing import RoutingResult, summarize_routing

MANIFEST = {"layer": "routing", "started_at": "2026-09-21T10:00:00+00:00", "model": "m", "endpoint_host": "h",
            "embed_model": "nomic", "embed_dim": 768, "git_sha": "abcdef1234567890", "git_dirty": False,
            "dataset_hashes": {"routing.jsonl": "0123456789abcdef"}, "corpus_hash": None, "qdrant_points": None,
            "neo4j_nodes": None, "planner_prompt_hash": "p"}


def test_compare_headline_flags_noise_and_skips_missing_metrics():
    current = {"pass_rate": {"successes": 9, "n": 10}, "new_metric": {"successes": 1, "n": 1}, "empty": {"successes": 0, "n": 0}}
    baseline = {"pass_rate": {"successes": 8, "n": 10}, "empty": {"successes": 0, "n": 0}}
    [row] = compare_headline(current, baseline)
    assert row["metric"] == "pass_rate" and row["delta"] == pytest.approx(0.1) and row["within_noise"] is True
    [big] = compare_headline({"pass_rate": {"successes": 95, "n": 100}}, {"pass_rate": {"successes": 60, "n": 100}})
    assert big["within_noise"] is False


def test_check_comparable():
    assert check_comparable(MANIFEST, dict(MANIFEST, git_sha="other")) == []
    other = dict(MANIFEST, model="different")
    with pytest.raises(ComparabilityError, match="model"):
        check_comparable(MANIFEST, other)
    assert check_comparable(MANIFEST, other, force=True) == ["model"]


def test_baseline_round_trip(tmp_path):
    path = tmp_path / "baselines" / "routing.json"
    save_baseline(path, MANIFEST, {"headline": {"pass_rate": {"successes": 1, "n": 2}}, "other": 1})
    assert load_baseline(path) == {"manifest": MANIFEST, "headline": {"pass_rate": {"successes": 1, "n": 2}}}


def routing_data():
    items = [RoutingItem("a", "hi", "direct_answer", None, ("greeting",), (), None),
             RoutingItem("b", "3*7", "tool_use", "calculator", ("arithmetic",), (), "21"),
             RoutingItem("c", "sqrt(4)", "tool_use", "calculator", ("arithmetic",), (), "2")]
    results = [RoutingResult("a", 0, "pass", "direct_answer", raw_verdict="pass", latency_ms=5.0),
               RoutingResult("b", 0, "wrong_tool", "tool_use", "web_search", raw_verdict="wrong_tool",
                             tool_check="skipped: routed elsewhere", latency_ms=7.0),
               RoutingResult("c", 0, "pass", "tool_use", "calculator", raw_verdict="pass", tool_check="fail", latency_ms=9.0)]
    return items, results


def test_failure_rows_for_routing():
    items, results = routing_data()
    rows = [vars(r) for r in results]
    failures = failure_rows("routing", items, rows)
    assert [f["id"] for f in failures] == ["b", "c"]
    assert failures[0]["expected"] == "tool_use/calculator" and failures[0]["actual"] == "tool_use/web_search"
    assert failures[0]["reason"] == "wrong_tool" and failures[1]["reason"] == "tool_check_failed"
    assert len(failure_rows("routing", items, rows, limit=1)) == 1


def qa_item(id, sources=("a.pdf",), label="source"):
    return QAItem(id=id, query="q", origin="hand", answerable=True, label_type=label,
                  expected_sources=tuple(sources) if label == "source" else (),
                  reference_contexts=("ctx",) if label == "fuzzy" else (), calibration_sources=(),
                  ground_truth=None, tags=())


def retrieval_data():
    items = [qa_item("s1"), qa_item("s2", ("b.pdf",)), qa_item("s3", ("c.pdf",)), qa_item("f1", label="fuzzy")]

    def scored(item_id, names):
        ranked = [{"filename": n, "page": 1, "score": 0.9 - i / 10, "text": "t"} for i, n in enumerate(names)]
        return RetrievalResult(item_id=item_id, verdict="scored", ranked=ranked, documents_n=len(ranked),
                               vector_docs=len(ranked), top1_score=ranked[0]["score"], latency_ms=5.0)

    results = [scored("s1", ["x.pdf", "y.pdf"]), scored("s2", ["b.pdf"]),
               RetrievalResult(item_id="s3", verdict="unscorable", reason="sources not ingested: ['c.pdf']"),
               scored("f1", ["k.html"])]
    return items, results


def test_failure_rows_for_retrieval():
    items, results = retrieval_data()
    failures = failure_rows("retrieval", items, [vars(r) for r in results])
    assert [(f["id"], f["reason"].split(":")[0]) for f in failures] == [("s1", "miss@5"), ("s3", "unscorable")]
    assert failures[0]["actual"] == "x.pdf, y.pdf"


def test_render_routing_summary():
    items, results = routing_data()
    summary = summarize_routing(items, results)
    failures = failure_rows("routing", items, [vars(r) for r in results])
    md = render_summary_md("routing", MANIFEST, summary, ["a warning"], failures)
    for expected in ["# Eval run: routing", "| pass_rate | 66.7% |", "## By tag", "## Safeguards", "## Confusion matrix",
                     "## Tool execution", "## Failures", "a warning", "abcdef1234"]:
        assert expected in md
    assert "INVALID RUN" not in md and "## Comparison with baseline" not in md


def test_render_marks_invalid_runs_and_includes_comparison():
    items, results = routing_data()
    summary = dict(summarize_routing(items, results), invalid=True)
    comparison = [{"metric": "pass_rate", "baseline": 0.5, "current": 0.75, "delta": 0.25, "within_noise": True}]
    md = render_summary_md("routing", MANIFEST, summary, [], [], comparison)
    assert "INVALID RUN" in md and "## Comparison with baseline" in md and "+25.0 pts" in md and "within noise" in md


def test_render_retrieval_summary():
    items, results = retrieval_data()
    summary = summarize_retrieval(items, results, 0.5, None, 0)
    md = render_summary_md("retrieval", dict(MANIFEST, layer="retrieval"), summary, [], [])
    for expected in ["# Eval run: retrieval", "| source_hit@5 |", "## Groups", "source/hand", "## Fuzzy",
                     "uncalibrated", "## Unanswerable separation", "## Arms"]:
        assert expected in md


def test_write_run_creates_all_files(tmp_path):
    out = tmp_path / "run"
    write_run(out, MANIFEST, [{"item_id": "a", "verdict": "pass"}, {"item_id": "b", "verdict": "wrong_tool"}],
              {"headline": {}}, "# md")
    assert sorted(p.name for p in out.iterdir()) == ["manifest.json", "results.jsonl", "summary.json", "summary.md"]
    lines = (out / "results.jsonl").read_text().splitlines()
    assert [json.loads(line)["item_id"] for line in lines] == ["a", "b"]
    assert json.loads((out / "manifest.json").read_text())["model"] == "m"
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/eval_harness/test_report.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.report'`

- [ ] **Step 3: Write `eval/harness/report.py`**

```python
import json
from pathlib import Path

from eval.harness.datasets import ACTIONS
from eval.harness.manifest import comparability
from eval.harness.stats import intervals_overlap, wilson_interval


class ComparabilityError(RuntimeError):
    pass


def save_baseline(path, manifest, summary):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"manifest": manifest, "headline": summary["headline"]}, indent=2, sort_keys=True))


def load_baseline(path):
    return json.loads(Path(path).read_text())


def check_comparable(manifest, baseline_manifest, force=False):
    differing = comparability(manifest, baseline_manifest)
    if differing and not force:
        raise ComparabilityError(
            f"baseline is not comparable; these differ: {', '.join(differing)} (--force-compare overrides)")
    return differing


def compare_headline(current, baseline):
    rows = []
    for name, cur in current.items():
        base = baseline.get(name)
        if base is None or not cur["n"] or not base["n"]:
            continue
        cur_rate, base_rate = cur["successes"] / cur["n"], base["successes"] / base["n"]
        within_noise = intervals_overlap(wilson_interval(cur["successes"], cur["n"]),
                                         wilson_interval(base["successes"], base["n"]))
        rows.append({"metric": name, "baseline": base_rate, "current": cur_rate,
                     "delta": cur_rate - base_rate, "within_noise": within_noise})
    return rows


def failure_rows(layer, items, rows, limit=25):
    by_id = {i.id: i for i in items}
    failures = []
    for r in rows:
        item = by_id[r["item_id"]]
        if layer == "routing":
            reasons = [] if r["verdict"] == "pass" else [r["verdict"]]
            if r.get("tool_check") == "fail":
                reasons.append("tool_check_failed")
            if reasons:
                failures.append({"id": item.id, "query": item.query,
                                 "expected": f"{item.expected_action}/{item.expected_tool}",
                                 "actual": f"{r['actual_action']}/{r['actual_tool']}", "reason": ", ".join(reasons)})
        elif r["verdict"] in ("unscorable", "error"):
            failures.append({"id": item.id, "query": item.query, "expected": ", ".join(item.expected_sources),
                             "actual": "", "reason": f"{r['verdict']}: {r['reason']}"})
        elif item.label_type == "source":
            names = [c["filename"] for c in r["ranked"]]
            if not any(n in item.expected_sources for n in names[:5]):
                failures.append({"id": item.id, "query": item.query, "expected": ", ".join(item.expected_sources),
                                 "actual": ", ".join(names[:3]), "reason": "miss@5"})
    return failures[:limit]


def _pct(value):
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _fmt(value):
    return "-" if value is None else f"{value:.3f}"


def _table(headers, rows):
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    return lines + ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]


def _headline_lines(headline):
    rows = []
    for name, h in headline.items():
        rate = h["successes"] / h["n"] if h["n"] else None
        lo, hi = wilson_interval(h["successes"], h["n"])
        rows.append((name, _pct(rate), f"{_pct(lo)} to {_pct(hi)}", h["n"]))
    return _table(["metric", "value", "95% CI", "n"], rows)


def _routing_sections(s):
    tag_rows = [(t, _pct(v["successes"] / v["n"] if v["n"] else None), v["n"]) for t, v in sorted(s["per_tag"].items())]
    guards = s["safeguards"]
    lines = ["", "## By tag", ""] + _table(["tag", "pass", "n"], tag_rows)
    lines += ["", "## Safeguards", "",
              f"- Accuracy gained by the safeguards (coerced minus raw): {_pct(guards['value'])}",
              f"- Fired: {guards['fired'] or 'none'}",
              f"- Simulation mismatches: {guards['mismatches']}"]
    lines += ["", "## Confusion matrix (rows expected, columns actual)", ""]
    lines += _table(["expected", *ACTIONS], [[a, *[s["confusion"][a][b] for b in ACTIONS]] for a in ACTIONS])
    te = s["tool_execution"]
    lines += ["", "## Tool execution", "", f"- pass {te['pass']}, fail {te['fail']}, skipped {te['skipped']}"]
    if s["stability"]["value"] is not None:
        unstable = s["stability"]["unstable"]
        lines += ["", "## Stability", "",
                  f"- {_pct(s['stability']['value'])} of repeated items gave the same verdict every time"
                  + (f"; unstable: {', '.join(unstable)}" if unstable else "")]
    return lines


def _retrieval_sections(s):
    metrics = ("hit@1", "hit@3", "hit@5", "mrr", "source_recall@5", "mean_top1_score")
    group_rows = [[key, g["n"], *[_fmt(g.get(m)) for m in metrics]] for key, g in s["groups"].items()]
    lines = ["", "## Groups", ""] + _table(["group", "n", *metrics], group_rows)
    fz = s["fuzzy"]
    if fz["agreement"] is None:
        status = "uncalibrated (no dual-labelled items)"
    else:
        status = f"agreement {_pct(fz['agreement'])} over {fz['n_calibration']} items, " + \
                 ("reliable" if fz["reliable"] else "UNRELIABLE (below 85%)")
    lines += ["", "## Fuzzy", "", f"- {fz['n']} fuzzy items, lexical threshold {fz['threshold']}: {status}"]
    un = s["unanswerable"]
    lines += ["", "## Unanswerable separation", "",
              f"- {un['n_answerable']} answerable vs {un['n_unanswerable']} unanswerable; AUROC {_fmt(un['auroc'])}; "
              f"suggested cutoff {_fmt(un['cutoff'])}"
              + ("; descriptive only (fewer than 20 unanswerable items)" if un["descriptive_only"] else "")]
    lines += ["", "## Arms", "",
              f"- vector documents {s['arms']['vector_docs']}, graph documents {s['arms']['graph_docs']}",
              f"- empty-context rate {_pct(s['empty_context_rate'])}"]
    return lines


def render_summary_md(layer, manifest, summary, warnings, failures, comparison=None):
    lines = [f"# Eval run: {layer}", ""]
    if summary.get("invalid"):
        lines += ["**INVALID RUN**: the planner's LLM call failed too often, so this measured an outage, not routing.", ""]
    lines += ["## Manifest", "",
              f"- started {manifest['started_at']}; model {manifest['model']} at {manifest['endpoint_host']}",
              f"- embeddings {manifest['embed_model']} ({manifest['embed_dim']}-dim)",
              f"- git {manifest['git_sha'][:10]}{' (dirty)' if manifest['git_dirty'] else ''}",
              f"- datasets {', '.join(f'{k}@{v[:8]}' for k, v in manifest['dataset_hashes'].items())}"
              + (f"; corpus {manifest['corpus_hash'][:8]}" if manifest.get("corpus_hash") else ""),
              f"- Qdrant points {manifest.get('qdrant_points')}; Neo4j nodes {manifest.get('neo4j_nodes')}"]
    if warnings:
        lines += ["", "## Warnings", ""] + [f"- {w}" for w in warnings]
    lines += ["", "## Headline", ""] + _headline_lines(summary["headline"])
    lines += _routing_sections(summary) if layer == "routing" else _retrieval_sections(summary)
    lat = summary["latency_ms"]
    lines += ["", "## Latency", "", f"- p50 {_fmt(lat['p50'])} ms, p95 {_fmt(lat['p95'])} ms"]
    if comparison:
        lines += ["", "## Comparison with baseline", ""]
        lines += _table(["metric", "baseline", "current", "delta", "verdict"],
                        [(c["metric"], _pct(c["baseline"]), _pct(c["current"]), f"{100 * c['delta']:+.1f} pts",
                          "within noise" if c["within_noise"] else "outside noise") for c in comparison])
    if failures:
        lines += ["", "## Failures", ""]
        lines += _table(["id", "query", "expected", "actual", "reason"],
                        [(f["id"], f["query"][:60], f["expected"], f["actual"], f["reason"]) for f in failures])
    return "\n".join(lines) + "\n"


def write_run(out_dir, manifest, rows, summary, markdown):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    (out / "summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True))
    (out / "results.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    (out / "summary.md").write_text(markdown)
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `python -m pytest tests/eval_harness/test_report.py -q`
Expected: `9 passed`

Note on `test_render_routing_summary`: the `"| pass_rate | 66.7% |"` assertion relies on 2 of 3 items passing (`a` and `c` have verdict `pass`; `b` is `wrong_tool`).

- [ ] **Step 5: Commit (if authorised)**

```bash
git add eval/harness/report.py tests/eval_harness/test_report.py
git commit -m "feat(eval): add report writer, baselines and noise-aware comparison

With 20-60 items a one-item swing is noise, so comparisons carry Wilson
intervals and refuse to diff runs whose model, datasets or corpus differ."
```

---

### Task 8: CLI and entry point

**Files:**
- Create: `eval/harness/cli.py`, `eval/harness/__main__.py`, `tests/eval_harness/test_cli.py`

**Interfaces:**
- Consumes: everything from Tasks 1 to 7.
- Produces: the command `python -m eval.harness {routing|retrieval} [options]`.
  - Options common to both: `--dataset PATH`, `--out DIR`, `--set-baseline`, `--compare`, `--force-compare`.
  - `routing`: `--repeats N` (default 1), `--dataset` defaults to `eval/sets/routing.jsonl`.
  - `retrieval`: `--corpus-manifest PATH` (default `eval/sets/corpus_manifest.json`), `--dataset` defaults to `eval/sets/qa.jsonl`.
  - `build_parser() -> argparse.ArgumentParser`, `exit_code(invalid, preflight_ok, comparison_error) -> int` (2 if any is bad, else 0), `verify_planner_contract(module)` (raises `RecorderError` if the planner source no longer contains `FALLBACK_PLAN_PREFIX`), `main(argv=None) -> int`.
  - Runs are written to `eval/runs/<YYYYmmdd-HHMMSS>-<layer>/` unless `--out` is given. `--set-baseline` writes `eval/baselines/<layer>.json` and is ignored for invalid runs.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_cli.py`:

```python
import types

import pytest

from eval.harness.cli import build_parser, exit_code, main, verify_planner_contract
from eval.harness.recorders import RecorderError


def test_parser_defaults():
    routing = build_parser().parse_args(["routing"])
    assert routing.layer == "routing" and routing.repeats == 1 and routing.dataset == "eval/sets/routing.jsonl"
    assert routing.out is None and not routing.set_baseline and not routing.compare and not routing.force_compare
    retrieval = build_parser().parse_args(["retrieval"])
    assert retrieval.dataset == "eval/sets/qa.jsonl" and retrieval.corpus_manifest == "eval/sets/corpus_manifest.json"


def test_parser_flags():
    args = build_parser().parse_args(["routing", "--repeats", "3", "--set-baseline", "--compare", "--out", "x"])
    assert args.repeats == 3 and args.set_baseline and args.compare and args.out == "x"
    assert build_parser().parse_args(["retrieval", "--force-compare"]).force_compare


def test_parser_requires_a_layer():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_exit_code():
    assert exit_code(False, True, None) == 0
    assert exit_code(True, True, None) == 2
    assert exit_code(False, False, None) == 2
    assert exit_code(False, True, "not comparable") == 2


def test_verify_planner_contract(tmp_path):
    good = tmp_path / "planner_ok.py"
    good.write_text('plan = ["Error in planning, defaulting to retrieval."]\n')
    verify_planner_contract(types.SimpleNamespace(__file__=str(good)))
    bad = tmp_path / "planner_changed.py"
    bad.write_text('plan = ["Planner failed"]\n')
    with pytest.raises(RecorderError, match="FALLBACK_PLAN_PREFIX"):
        verify_planner_contract(types.SimpleNamespace(__file__=str(bad)))


def test_main_refuses_to_run_outside_the_repo_root(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["routing"]) == 2
    assert "repo root" in capsys.readouterr().err
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/eval_harness/test_cli.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.cli'`

- [ ] **Step 3: Write `eval/harness/cli.py`**

```python
import argparse
import asyncio
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from eval.harness import probes
from eval.harness.datasets import DatasetError, file_sha256, load_qa, load_routing
from eval.harness.manifest import build_manifest, git_info, planner_prompt_hash
from eval.harness.preflight import run_preflight
from eval.harness.recorders import PlannerRecorder, RecorderError, RetrievalRecorder
from eval.harness.report import (
    ComparabilityError, check_comparable, compare_headline, failure_rows, load_baseline, render_summary_md,
    save_baseline, write_run,
)
from eval.harness.retrieval import calibrate_threshold, run_retrieval, summarize_retrieval
from eval.harness.routing import FALLBACK_PLAN_PREFIX, run_routing, summarize_routing

EXIT_OK = 0
EXIT_INVALID = 2


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m eval.harness")
    layers = parser.add_subparsers(dest="layer", required=True)
    routing = layers.add_parser("routing", help="score planner routing")
    routing.add_argument("--dataset", default="eval/sets/routing.jsonl")
    routing.add_argument("--repeats", type=int, default=1)
    retrieval = layers.add_parser("retrieval", help="score retrieval")
    retrieval.add_argument("--dataset", default="eval/sets/qa.jsonl")
    retrieval.add_argument("--corpus-manifest", default="eval/sets/corpus_manifest.json")
    for sub in (routing, retrieval):
        sub.add_argument("--out", default=None)
        sub.add_argument("--set-baseline", action="store_true")
        sub.add_argument("--compare", action="store_true")
        sub.add_argument("--force-compare", action="store_true")
    return parser


def exit_code(invalid, preflight_ok, comparison_error):
    return EXIT_INVALID if invalid or not preflight_ok or comparison_error else EXIT_OK


def verify_planner_contract(module):
    if FALLBACK_PLAN_PREFIX not in Path(module.__file__).read_text():
        raise RecorderError(
            f"planner module no longer contains {FALLBACK_PLAN_PREFIX!r}; update FALLBACK_PLAN_PREFIX in eval/harness/routing.py")


async def _run(args):
    from dotenv import load_dotenv

    load_dotenv()

    from services.api.app.agents.nodes import planner as planner_module
    from services.api.app.agents.nodes.retriever import retrieve_node
    from services.api.app.agents.nodes.tool import tool_node
    from services.api.app.clients.qdrant import qdrant_client
    from services.api.app.clients.ray_llm import llm_client
    from services.api.app.config import settings
    from services.api.app.tools.registry import TOOL_REGISTRY

    layer = args.layer
    out_dir = Path(args.out) if args.out else Path("eval/runs") / f"{datetime.now():%Y%m%d-%H%M%S}-{layer}"
    dataset_path = Path(args.dataset)
    corpus_path = None
    if layer == "routing":
        items, expected_sources = load_routing(dataset_path), set()
    else:
        items = load_qa(dataset_path)
        expected_sources = {s for i in items if i.label_type == "source" for s in i.expected_sources}
        corpus_path = Path(args.corpus_manifest)

    await llm_client.start()
    try:
        pre = await run_preflight(
            layer=layer, embed_dim=settings.EMBED_DIM, expected_sources=expected_sources,
            probes={"llm": probes.llm_probe, "embed": probes.embed_probe,
                    "qdrant": probes.qdrant_probe, "neo4j": probes.neo4j_probe})
        for message in pre.errors:
            print(f"ERROR: {message}", file=sys.stderr)
        for message in pre.warnings:
            print(f"warning: {message}", file=sys.stderr)
        if not pre.ok:
            return EXIT_INVALID
        if layer == "routing":
            verify_planner_contract(planner_module)
            recorder = PlannerRecorder(planner_module)
            with recorder:
                results = await run_routing(items, args.repeats, planner_module.planner_node, tool_node,
                                            recorder, set(TOOL_REGISTRY))
            summary = summarize_routing(items, results)
        else:
            if pre.facts.get("neo4j_nodes") is None:
                logging.getLogger("services.api.app.agents.nodes.retriever").setLevel(logging.CRITICAL)
            recorder = RetrievalRecorder(qdrant_client)
            with recorder:
                results = await run_retrieval(items, retrieve_node, recorder, pre.facts["filenames"])
            threshold, agreement, n_dual = calibrate_threshold(items, results)
            summary = summarize_retrieval(items, results, threshold, agreement, n_dual)
    finally:
        await llm_client.close()

    sha, dirty = git_info()
    corpus_hash = corpus_seed = None
    if corpus_path is not None and corpus_path.exists():
        corpus_hash = file_sha256(corpus_path)
        corpus_seed = json.loads(corpus_path.read_text()).get("seed")
    manifest = build_manifest(
        layer=layer, model=settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME, endpoint=settings.RAY_LLM_ENDPOINT,
        embed_model=settings.EMBED_MODEL_NAME, embed_dim=settings.EMBED_DIM,
        prompt_hash=planner_prompt_hash(planner_module.SYSTEM_PROMPT), git_sha=sha, git_dirty=dirty,
        dataset_hashes={dataset_path.name: file_sha256(dataset_path)}, corpus_hash=corpus_hash,
        corpus_seed=corpus_seed, qdrant_points=pre.facts.get("qdrant_points"),
        qdrant_vector_size=pre.facts.get("qdrant_vector_size"), neo4j_nodes=pre.facts.get("neo4j_nodes"))

    baseline_path = Path("eval/baselines") / f"{layer}.json"
    comparison, comparison_error = None, None
    if args.compare or args.force_compare:
        if not baseline_path.exists():
            pre.warnings.append(f"No baseline at {baseline_path}; run once with --set-baseline first.")
        else:
            baseline = load_baseline(baseline_path)
            try:
                differing = check_comparable(manifest, baseline["manifest"], force=args.force_compare)
                if differing:
                    pre.warnings.append(f"Compared across differing {', '.join(differing)} (forced).")
                comparison = compare_headline(summary["headline"], baseline["headline"])
            except ComparabilityError as e:
                comparison_error = str(e)
                pre.warnings.append(comparison_error)

    rows = [asdict(r) for r in results]
    markdown = render_summary_md(layer, manifest, summary, pre.warnings, failure_rows(layer, items, rows), comparison)
    write_run(out_dir, manifest, rows, summary, markdown)
    if args.set_baseline and not summary["invalid"]:
        save_baseline(baseline_path, manifest, summary)
        print(f"baseline saved: {baseline_path}")
    print(f"run written to {out_dir}")
    for name, h in summary["headline"].items():
        print(f"  {name}: {h['successes']}/{h['n']}")
    return exit_code(summary["invalid"], True, comparison_error)


def main(argv=None):
    args = build_parser().parse_args(argv)
    if not Path("services/api/app").is_dir():
        print("error: run from the repo root, e.g. python -m eval.harness routing", file=sys.stderr)
        return EXIT_INVALID
    try:
        return asyncio.run(_run(args))
    except (DatasetError, RecorderError) as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_INVALID
```

Create `eval/harness/__main__.py`:

```python
import sys

from eval.harness.cli import main

sys.exit(main())
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `python -m pytest tests/eval_harness/test_cli.py -q`
Expected: `6 passed`

- [ ] **Step 5: Smoke-check the entry point without services**

Run: `python -m eval.harness --help | head -3`
Expected: usage text beginning `usage: python -m eval.harness [-h] {routing,retrieval} ...`

Run from a different directory: `cd /tmp && python -m eval.harness routing; echo "exit=$?"; cd -`
Expected: `No module named eval`. The package is only importable from the repo root, so this confirms the harness must be launched there; the root check inside `main` covers the case where the package is importable but the working directory is wrong.

- [ ] **Step 6: Run the whole harness test suite**

Run: `python -m pytest tests/eval_harness -q`
Expected: `all tests pass` (13 + 7 + 8 + 14 + 10 + 5 + 11 + 9 + 6 = 83 passed)

- [ ] **Step 7: Commit (if authorised)**

```bash
git add eval/harness/cli.py eval/harness/__main__.py tests/eval_harness/test_cli.py
git commit -m "feat(eval): add the harness CLI

Wires preflight, recorders, scorers, manifest and report behind one
command. Invalid runs and failed preflight exit 2 so an outage can never
be mistaken for a regression."
```

---

### Task 9: Corpus tooling and the source-locating script

**Files:**
- Create: `eval/harness/corpus.py`, `tests/eval_harness/test_corpus.py`, `scripts/add_source_item.py`

**Interfaces:**
- Consumes: `load_qa` (Task 1).
- Produces (used by Tasks 10, 11):
  - `corpus.py` constants `DEFAULT_HARD_KEYWORDS`, `POOL_DIRS = ("eval/datasets/noisy_data", "eval/datasets/ingested", "eval/datasets/digested")`, `TRUE_DIR = "eval/datasets/true_data"`, `STAGE_DIR = "eval/datasets/eval_corpus"`, `MANIFEST_PATH = "eval/sets/corpus_manifest.json"`.
  - `list_pool(dirs, suffixes=(".pdf",)) -> list[Path]` sorted by filename; raises `ValueError` on duplicate filenames.
  - `select_sample(pool, *, seed, n_random, n_hard, keywords=DEFAULT_HARD_KEYWORDS, include=()) -> {"forced", "hard_negatives", "random"}` (lists of filenames). Hard negatives are pool files whose lowercase name contains a keyword; all groups are disjoint; the result is deterministic for a seed.
  - `build_corpus_manifest(true_dir, pool, selection, seed) -> dict` with `seed`, `true_data`, `forced`, `hard_negatives`, `random`; each entry is `{"filename", "sha256"}`.
  - `stage_corpus(manifest, true_dir, pool, dest) -> int` copies every listed file into `dest`, raising `ValueError` if a source file's hash no longer matches the manifest; returns the file count in `dest`.
  - `extract_text(path, max_pages=3) -> str` for pdf, docx, pptx, html and plain text.
  - CLI: `python -m eval.harness.corpus build [--seed 42] [--n-random 60] [--n-hard 20] [--include NAME ...] [--out PATH]` and `python -m eval.harness.corpus stage [--manifest PATH] [--dest DIR]`.
  - `scripts/add_source_item.py --id ID --query Q --phrase P [--tag T ...] [--pages N] [--pick N] [--dry-run] [--out PATH]` finds corpus files whose first `N` pages (default 3) contain every word of the phrase, and appends a `source` item to `qa.jsonl`.

- [ ] **Step 1: Write the failing tests**

Create `tests/eval_harness/test_corpus.py`:

```python
import pytest
from docx import Document

from eval.harness.corpus import (
    build_corpus_manifest, extract_text, list_pool, select_sample, sha256_file, stage_corpus,
)


def make_files(directory, names):
    directory.mkdir(parents=True, exist_ok=True)
    for name in names:
        (directory / name).write_text(name)


def make_pool(tmp_path):
    names = [f"paper_{i:02d}.pdf" for i in range(30)] + [f"distributed_{i}.pdf" for i in range(5)]
    make_files(tmp_path / "pool", names)
    return list_pool([tmp_path / "pool"])


def test_list_pool_filters_suffix_sorts_and_rejects_duplicates(tmp_path):
    make_files(tmp_path / "a", ["b.pdf", "a.pdf", "notes.txt"])
    assert [p.name for p in list_pool([tmp_path / "a", tmp_path / "missing"])] == ["a.pdf", "b.pdf"]
    make_files(tmp_path / "b", ["a.pdf"])
    with pytest.raises(ValueError, match="duplicate"):
        list_pool([tmp_path / "a", tmp_path / "b"])


def test_select_sample_is_deterministic_disjoint_and_keyword_driven(tmp_path):
    pool = make_pool(tmp_path)
    first = select_sample(pool, seed=1, n_random=10, n_hard=3)
    assert first == select_sample(pool, seed=1, n_random=10, n_hard=3)
    assert len(first["hard_negatives"]) == 3 and all("distributed" in n for n in first["hard_negatives"])
    assert len(first["random"]) == 10
    assert not set(first["hard_negatives"]) & set(first["random"])
    assert first["random"] != select_sample(pool, seed=2, n_random=10, n_hard=3)["random"]


def test_select_sample_caps_hard_negatives_and_honours_include(tmp_path):
    pool = make_pool(tmp_path)
    capped = select_sample(pool, seed=1, n_random=5, n_hard=50)
    assert len(capped["hard_negatives"]) == 5
    forced = select_sample(pool, seed=1, n_random=29, n_hard=0, include=["paper_00.pdf"])
    assert forced["forced"] == ["paper_00.pdf"] and "paper_00.pdf" not in forced["random"]
    with pytest.raises(ValueError, match="not in the pool"):
        select_sample(pool, seed=1, n_random=1, n_hard=0, include=["nope.pdf"])


def test_manifest_and_staging_verify_hashes(tmp_path):
    pool = make_pool(tmp_path)
    make_files(tmp_path / "true", ["k8s.txt"])
    selection = select_sample(pool, seed=1, n_random=2, n_hard=1)
    manifest = build_corpus_manifest(tmp_path / "true", pool, selection, seed=1)
    assert manifest["seed"] == 1 and manifest["true_data"] == [{"filename": "k8s.txt", "sha256": sha256_file(tmp_path / "true" / "k8s.txt")}]
    assert stage_corpus(manifest, tmp_path / "true", pool, tmp_path / "stage") == 4
    assert (tmp_path / "stage" / "k8s.txt").exists()
    (tmp_path / "pool" / selection["random"][0]).write_text("changed")
    with pytest.raises(ValueError, match="changed since"):
        stage_corpus(manifest, tmp_path / "true", list_pool([tmp_path / "pool"]), tmp_path / "stage2")


def test_extract_text_for_text_html_and_docx(tmp_path):
    (tmp_path / "a.txt").write_text("plain words")
    (tmp_path / "b.html").write_text("<html><body><h1>Title</h1><p>Body text</p></body></html>")
    doc = Document()
    doc.add_paragraph("Horizontal pod autoscaler")
    doc.save(str(tmp_path / "c.docx"))
    assert extract_text(tmp_path / "a.txt") == "plain words"
    assert "Body text" in extract_text(tmp_path / "b.html")
    assert "Horizontal pod autoscaler" in extract_text(tmp_path / "c.docx")
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/eval_harness/test_corpus.py -q`
Expected: collection error `ModuleNotFoundError: No module named 'eval.harness.corpus'`

- [ ] **Step 3: Write `eval/harness/corpus.py`**

```python
import argparse
import hashlib
import json
import random
import shutil
import sys
from pathlib import Path

DEFAULT_HARD_KEYWORDS = ("scheduling", "distributed", "container", "autoscal", "cluster", "kubernetes",
                         "orchestrat", "docker", "cloud", "load balanc")
POOL_DIRS = ("eval/datasets/noisy_data", "eval/datasets/ingested", "eval/datasets/digested")
TRUE_DIR = "eval/datasets/true_data"
STAGE_DIR = "eval/datasets/eval_corpus"
MANIFEST_PATH = "eval/sets/corpus_manifest.json"
_GROUPS = ("forced", "hard_negatives", "random")


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def list_pool(dirs, suffixes=(".pdf",)):
    files = [p for d in dirs if Path(d).is_dir() for p in Path(d).iterdir()
             if p.is_file() and p.suffix.lower() in suffixes]
    files.sort(key=lambda p: p.name)
    names = [p.name for p in files]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate filenames in the pool: {duplicates}")
    return files


def select_sample(pool, *, seed, n_random, n_hard, keywords=DEFAULT_HARD_KEYWORDS, include=()):
    names = [p.name for p in pool]
    unknown = [n for n in include if n not in names]
    if unknown:
        raise ValueError(f"--include names not in the pool: {unknown}")
    rng = random.Random(seed)
    forced = sorted(include)
    remaining = [n for n in names if n not in forced]
    candidates = [n for n in remaining if any(k in n.lower() for k in keywords)]
    hard = sorted(rng.sample(candidates, min(n_hard, len(candidates))))
    rest = [n for n in remaining if n not in hard]
    return {"forced": forced, "hard_negatives": hard, "random": sorted(rng.sample(rest, min(n_random, len(rest))))}


def build_corpus_manifest(true_dir, pool, selection, seed):
    by_name = {p.name: p for p in pool}
    manifest = {"seed": seed, "true_data": [{"filename": p.name, "sha256": sha256_file(p)}
                                            for p in sorted(Path(true_dir).iterdir()) if p.is_file()]}
    for group in _GROUPS:
        manifest[group] = [{"filename": n, "sha256": sha256_file(by_name[n])} for n in selection[group]]
    return manifest


def stage_corpus(manifest, true_dir, pool, dest):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    by_name = {p.name: p for p in pool}
    for entry in manifest["true_data"]:
        _copy_checked(Path(true_dir) / entry["filename"], entry, dest)
    for group in _GROUPS:
        for entry in manifest[group]:
            _copy_checked(by_name[entry["filename"]], entry, dest)
    return sum(1 for p in dest.iterdir() if p.is_file())


def _copy_checked(src, entry, dest):
    if sha256_file(src) != entry["sha256"]:
        raise ValueError(f"{entry['filename']} changed since the corpus manifest was built; rebuild the manifest")
    shutil.copy2(src, dest / entry["filename"])


def extract_text(path, max_pages=3):
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        return " ".join((page.extract_text() or "") for page in PdfReader(str(path)).pages[:max_pages])
    if suffix == ".docx":
        from docx import Document

        return "\n".join(p.text for p in Document(str(path)).paragraphs)
    if suffix == ".pptx":
        from pptx import Presentation

        return "\n".join(shape.text_frame.text for slide in Presentation(str(path)).slides
                         for shape in slide.shapes if shape.has_text_frame)
    if suffix in (".html", ".htm"):
        from bs4 import BeautifulSoup

        return BeautifulSoup(path.read_text(errors="ignore"), "html.parser").get_text(" ")
    return path.read_text(errors="ignore")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m eval.harness.corpus")
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build")
    build.add_argument("--seed", type=int, default=42)
    build.add_argument("--n-random", type=int, default=60)
    build.add_argument("--n-hard", type=int, default=20)
    build.add_argument("--include", nargs="*", default=[])
    build.add_argument("--out", default=MANIFEST_PATH)
    stage = commands.add_parser("stage")
    stage.add_argument("--manifest", default=MANIFEST_PATH)
    stage.add_argument("--dest", default=STAGE_DIR)
    args = parser.parse_args(argv)

    pool = list_pool(POOL_DIRS)
    if args.command == "build":
        selection = select_sample(pool, seed=args.seed, n_random=args.n_random, n_hard=args.n_hard,
                                  include=args.include)
        manifest = build_corpus_manifest(TRUE_DIR, pool, selection, args.seed)
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        print(f"pool {len(pool)} pdfs; manifest {out}: " + ", ".join(f"{g} {len(manifest[g])}" for g in ("true_data", *_GROUPS)))
    else:
        manifest = json.loads(Path(args.manifest).read_text())
        print(f"staged {stage_corpus(manifest, TRUE_DIR, pool, args.dest)} files in {args.dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run to verify the tests pass**

Run: `python -m pytest tests/eval_harness/test_corpus.py -q`
Expected: `5 passed`

- [ ] **Step 5: Write `scripts/add_source_item.py`**

```python
#!/usr/bin/env python3
"""
Find corpus files containing a phrase and append a source-labelled item to eval/sets/qa.jsonl.

Searches the noisy pool and true_data. A file matches when every word of --phrase appears in its
first --pages pages (PDF) or anywhere in it (other formats). Use --dry-run to list matches first.

Usage:
    python scripts/add_source_item.py --id h001 --query "How does selective state space modeling work?" \
        --phrase "selective state space" --tag mamba --dry-run
    python scripts/add_source_item.py --id h001 --query "..." --phrase "..." --pick 1
"""
import argparse
import json
import sys
from pathlib import Path

# Scripts run by path start with scripts/ on sys.path, not the repo root, so the eval package would not import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.harness.corpus import POOL_DIRS, TRUE_DIR, extract_text, list_pool  # noqa: E402


def find_matches(phrase, pages):
    words = phrase.lower().split()
    files = list_pool([*POOL_DIRS, TRUE_DIR], suffixes=(".pdf", ".docx", ".pptx", ".html", ".htm", ".txt", ".md"))
    matches = []
    for path in files:
        try:
            text = extract_text(path, max_pages=pages).lower()
        except Exception as e:
            print(f"skipped {path.name}: {type(e).__name__}", file=sys.stderr)
            continue
        if all(w in text for w in words):
            matches.append(path.name)
    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--id", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--phrase", required=True)
    parser.add_argument("--tag", action="append", default=[])
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--pick", type=int, help="1-based index into the match list when several files match")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", default="eval/sets/qa.jsonl")
    args = parser.parse_args()

    matches = find_matches(args.phrase, args.pages)
    for n, name in enumerate(matches, start=1):
        print(f"{n}. {name}")
    if args.dry_run:
        return 0
    if not matches or (len(matches) > 1 and args.pick is None):
        print(f"{len(matches)} matches; refine --phrase, raise --pages, or choose one with --pick N", file=sys.stderr)
        return 1
    chosen = matches[(args.pick or 1) - 1]

    out = Path(args.out)
    existing = {json.loads(line)["id"] for line in out.read_text().splitlines() if line.strip()} if out.exists() else set()
    if args.id in existing:
        print(f"id {args.id!r} already exists in {out}", file=sys.stderr)
        return 1
    item = {"id": args.id, "query": args.query, "origin": "hand", "answerable": True, "label_type": "source",
            "expected_sources": [chosen], "tags": args.tag}
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as f:
        f.write(json.dumps(item) + "\n")
    print(f"added {args.id} -> {chosen}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Smoke-check the script's help and a dry run on `true_data`**

Run: `python scripts/add_source_item.py --id x --query q --phrase "autoscal" --dry-run 2>&1 | tail -5`
Expected: a numbered list of matching filenames that includes `pods_autoscale.html`. It scans the whole pool, so allow a minute or two. Skipped-file warnings on stderr are acceptable.

- [ ] **Step 7: Commit (if authorised)**

```bash
git add eval/harness/corpus.py tests/eval_harness/test_corpus.py scripts/add_source_item.py
git commit -m "feat(eval): add corpus sampling, staging and a source-locating script

The noisy pool is gitignored, so the sample is recorded as filenames plus
hashes and staging refuses files that changed since the manifest was built."
```

---

### Task 10: Environment prep, corpus sample and ingestion (manual checkpoints)

**Files:**
- Create: `eval/sets/corpus_manifest.json` (generated)
- Ingests into the running Qdrant; nothing under `services/` or `pipelines/` changes.

**Interfaces:**
- Consumes: Task 0 (correct `qdrant-client`), Task 9 tooling.
- Produces: `rag_collection` populated with `true_data` plus the sampled PDFs, and `eval/sets/corpus_manifest.json`. Tasks 11 and 12 depend on both.

**Stop rule:** this task runs the Ray ingestion pipeline locally for the first time. If Step 6 or Step 8 fails, stop, report the exact error to the user, and do not continue to Task 11.

- [ ] **Step 1: Confirm the services are up**

Run: `curl -s localhost:6333/collections && echo && curl -s localhost:11434/api/tags | python -c "import sys,json; print([m['name'] for m in json.load(sys.stdin)['models']])" && python -c "from services.api.app.config import settings; print(settings.EMBED_DIM, settings.RAY_EMBED_ENDPOINT)"`
Expected: a Qdrant collection list, an Ollama model list containing `nomic-embed-text:latest`, and `768 http://localhost:11434/api/embeddings`. If Qdrant is down: `docker compose up -d qdrant` (Docker context must be `default`; check with `docker context show`).

- [ ] **Step 2: Locate the source PDFs for the four queries from `eval_pipeline.py`**

Run each as a dry run and note the filename each phrase should map to:
```bash
python scripts/add_source_item.py --id x --query q --phrase "selective state space" --dry-run
python scripts/add_source_item.py --id x --query q --phrase "KEY_VALUE_BASIC_INFORMATION" --pages 60 --dry-run
python scripts/add_source_item.py --id x --query q --phrase "oracle history table" --dry-run
python scripts/add_source_item.py --id x --query q --phrase "mutual information estimation" --dry-run
```
Expected: each prints one or more numbered filenames. If a phrase matches none, raise `--pages` or shorten the phrase. If it matches several, read the names and pick the one that is clearly the source. Record the four chosen filenames for Step 3.

- [ ] **Step 3: Build the corpus manifest, forcing those four files in**

Run: `python -m eval.harness.corpus build --seed 42 --n-random 60 --n-hard 20 --include "<file1>" "<file2>" "<file3>" "<file4>"` with the four filenames from Step 2, quoted.
Expected: `pool 934 pdfs; manifest eval/sets/corpus_manifest.json: true_data 6, forced 4, hard_negatives 20, random 60` (the pool count may differ slightly).

- [ ] **Step 4: Stage the corpus**

Run: `python -m eval.harness.corpus stage`
Expected: `staged 90 files in eval/datasets/eval_corpus` (6 true + 4 forced + 20 hard + 60 random). The directory is gitignored.

- [ ] **Step 5: Create the collection at the embedding dimension**

Run: `curl -s -X PUT localhost:6333/collections/rag_collection -H 'Content-Type: application/json' -d '{"vectors":{"size":768,"distance":"Cosine"}}'`
Expected: `{"result":true,"status":"ok",...}`. If it reports the collection already exists, check its size with `curl -s localhost:6333/collections/rag_collection` and delete and recreate it if the size is not 768.

- [ ] **Step 6: Smoke-ingest `true_data` alone**

Run: `time python -m pipelines.ingestion.main eval/datasets/true_data --no-graph 2>&1 | tail -15`
Expected: a log line `Ingestion complete.` and no traceback.
If it fails on a missing library (for example `fitz`), **stop** and report.

- [ ] **Step 7: Record the per-file point counts**

Run:
```bash
python - <<'EOF'
import json, urllib.request

def count(name):
    body = json.dumps({"exact": True, "filter": {"must": [{"key": "filename", "match": {"value": name}}]}}).encode()
    req = urllib.request.Request("http://localhost:6333/collections/rag_collection/points/count", body,
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))["result"]["count"]

for name in ["pods_autoscale.html", "cronjobs.docx", "architecture.pptx", "parallel_work_queue.txt"]:
    print(name, count(name))
EOF
```
Expected: four names with counts above zero. Save the numbers for Step 9. A zero for a file means the pipeline wrote a different payload key than `filename`; inspect one point with `curl -s -X POST localhost:6333/collections/rag_collection/points/scroll -H 'Content-Type: application/json' -d '{"limit":1,"with_payload":true}'` and stop if `filename` is absent.

- [ ] **Step 8: Ingest the full staged corpus**

Run: `time python -m pipelines.ingestion.main eval/datasets/eval_corpus --no-graph 2>&1 | tail -15`
Expected: `Ingestion complete.` The duration is unknown for the first run (about 90 files, one embedding call per chunk through Ollama); note it. If it exceeds about 30 minutes, run it in the background and poll the point count.

- [ ] **Step 9: Verify nothing was duplicated and the sample is present**

Rerun the Step 7 script.
Expected: the four counts are **identical** to Step 7 (the pipeline skips files whose content hash is unchanged). Then run: `curl -s localhost:6333/collections/rag_collection | python -c "import sys,json; r=json.load(sys.stdin)['result']; print(r['points_count'], r['config']['params']['vectors'])"`
Expected: a large points count and `{'size': 768, 'distance': 'Cosine'}`.
If any count doubled, re-ingestion duplicated points: stop, delete the collection, recreate it (Step 5), and ingest the staged directory once (Step 8) without the smoke ingest.

- [ ] **Step 10: Commit the manifest (if authorised)**

```bash
git add eval/sets/corpus_manifest.json
git commit -m "feat(eval): record the seeded corpus sample

The noisy PDFs are gitignored, so the manifest (filenames, hashes, seed) is
what makes the retrieval corpus reproducible."
```

---

### Task 11: Seed datasets

**Files:**
- Create: `eval/sets/routing.jsonl`, `eval/sets/qa.jsonl`, `scripts/seed_qa.py`

**Interfaces:**
- Consumes: Task 9's `extract_text`; Task 10's ingested corpus; `containment` (Task 2); `load_routing`, `load_qa` (Task 1).
- Produces: the two dataset files that Task 12 runs.

- [ ] **Step 1: Create `eval/sets/routing.jsonl`**

Write this exact content:

```
{"id":"r001","query":"hello there","expected_action":"direct_answer","expected_tool":null,"tags":["greeting"]}
{"id":"r002","query":"thank you","expected_action":"direct_answer","expected_tool":null,"tags":["greeting"]}
{"id":"r003","query":"good morning","expected_action":"direct_answer","expected_tool":null,"tags":["greeting"]}
{"id":"r004","query":"that's all for now, bye","expected_action":"direct_answer","expected_tool":null,"tags":["greeting","farewell"]}
{"id":"r005","query":"hlelo there","expected_action":"direct_answer","expected_tool":null,"tags":["greeting","typo"]}
{"id":"r006","query":"ok","expected_action":"direct_answer","expected_tool":null,"tags":["greeting","single-word"]}
{"id":"r010","query":"3*7","expected_action":"tool_use","expected_tool":"calculator","expected_tool_output":"21","tags":["arithmetic"]}
{"id":"r011","query":"55-31+21","expected_action":"tool_use","expected_tool":"calculator","expected_tool_output":"45","tags":["arithmetic"]}
{"id":"r012","query":"32-(14/2)","expected_action":"tool_use","expected_tool":"calculator","expected_tool_output":"25","tags":["arithmetic"]}
{"id":"r013","query":"sqrt(256)","expected_action":"tool_use","expected_tool":"calculator","expected_tool_output":"16","tags":["arithmetic","calculator-gap"]}
{"id":"r014","query":"sqrt(144) + 15% of 200","expected_action":"tool_use","expected_tool":"calculator","expected_tool_output":"42","tags":["arithmetic","calculator-gap"]}
{"id":"r015","query":"what is 12 times 12","expected_action":"tool_use","expected_tool":"calculator","expected_tool_output":"144","tags":["arithmetic","natural-language"]}
{"id":"r020","query":"what is the weather today","expected_action":"tool_use","expected_tool":"web_search","tags":["realtime"]}
{"id":"r021","query":"current bitcoin price","expected_action":"tool_use","expected_tool":"web_search","tags":["realtime"]}
{"id":"r022","query":"latest news headlines","expected_action":"tool_use","expected_tool":"web_search","tags":["realtime"]}
{"id":"r023","query":"what's the weather in Oslo today","expected_action":"tool_use","expected_tool":"web_search","tags":["realtime"]}
{"id":"r030","query":"run this python: print(sum(range(10)))","expected_action":"tool_use","expected_tool":"sandbox","tags":["code"]}
{"id":"r031","query":"```python\nprint('hello')\n```","expected_action":"tool_use","expected_tool":"sandbox","tags":["code"]}
{"id":"r040","query":"what entities are connected to the Horizontal Pod Autoscaler?","expected_action":"tool_use","expected_tool":"graph_search","tags":["graph"]}
{"id":"r041","query":"find entities related to Kubernetes","expected_action":"tool_use","expected_tool":"graph_search","tags":["graph"]}
{"id":"r050","query":"How does selective state space modeling work?","expected_action":"retrieve","expected_tool":null,"tags":["factual"]}
{"id":"r051","query":"What are the requirements for KEY_VALUE_BASIC_INFORMATION in Windows?","expected_action":"retrieve","expected_tool":null,"tags":["factual"]}
{"id":"r052","query":"How does Oracle optimize history table storage?","expected_action":"retrieve","expected_tool":null,"tags":["factual"]}
{"id":"r053","query":"What is mutual information estimation?","expected_action":"retrieve","expected_tool":null,"tags":["factual"]}
{"id":"r054","query":"Explain the difference between the Horizontal Pod Autoscaler and the Cluster Autoscaler in Kubernetes.","expected_action":"retrieve","expected_tool":null,"tags":["factual"]}
{"id":"r055","query":"Who won the 1998 football world cup?","expected_action":"retrieve","expected_tool":null,"also_accept":[["tool_use","web_search"]],"tags":["out-of-corpus"]}
{"id":"r060","query":"what's the weather in Oslo and what is 2+2","expected_action":"tool_use","expected_tool":"web_search","also_accept":[["tool_use","calculator"]],"tags":["multi-intent"]}
{"id":"r061","query":"and who maintains it?","expected_action":"retrieve","expected_tool":null,"tags":["coreference","known-limitation"]}
```

Items `r013` and `r014` are expected to fail their tool-execution check today, because the calculator has no `sqrt` and cannot parse `15% of 200`. Keeping them is deliberate: they document a real gap that `eval_pipeline.py` cannot see.

- [ ] **Step 2: Verify the routing dataset loads**

Run: `python -c "from eval.harness.datasets import load_routing; items = load_routing('eval/sets/routing.jsonl'); print(len(items), sum(i.expected_tool_output is not None for i in items))"`
Expected: `28 6`

- [ ] **Step 3: Create `scripts/seed_qa.py`**

```python
#!/usr/bin/env python3
"""
Seed eval/sets/qa.jsonl from eval/datasets/golden.json.

Every golden item becomes a fuzzy item. Where one true_data file clearly contains the reference text
(containment above MIN_SCORE and ahead of the runner-up by MIN_MARGIN), it is recorded as a
calibration source, making the item dual-labelled. Review the printed table before relying on it.
"""
import json
import sys
from pathlib import Path

# Scripts run by path start with scripts/ on sys.path, not the repo root, so the eval package would not import.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eval.harness.corpus import TRUE_DIR, extract_text  # noqa: E402
from eval.harness.stats import containment  # noqa: E402

GOLDEN = Path("eval/datasets/golden.json")
OUT = Path("eval/sets/qa.jsonl")
MIN_SCORE = 0.6
MIN_MARGIN = 0.05


def main():
    if OUT.exists():
        print(f"{OUT} already exists; delete it to re-seed", file=sys.stderr)
        return 1
    texts = {p.name: extract_text(p) for p in sorted(Path(TRUE_DIR).iterdir()) if p.is_file()}
    rows, review = [], []
    for n, item in enumerate(json.loads(GOLDEN.read_text()), start=1):
        reference = " ".join([item["ground_truth"], *item["contexts"]])
        ranked = sorted(((containment(reference, text), name) for name, text in texts.items()), reverse=True)
        (best, best_name), (second, _) = ranked[0], ranked[1]
        confident = best >= MIN_SCORE and best - second >= MIN_MARGIN
        item_id = f"g{n:03d}"
        rows.append({"id": item_id, "query": item["question"], "origin": "golden", "answerable": True,
                     "label_type": "fuzzy", "reference_contexts": item["contexts"],
                     "calibration_sources": [best_name] if confident else [],
                     "ground_truth": item["ground_truth"], "tags": ["golden"]})
        review.append((item_id, best_name, best, second, "yes" if confident else "NO"))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("".join(json.dumps(r) + "\n" for r in rows))
    print(f"{'id':<6}{'best file':<28}{'score':>7}{'second':>8}  calibrated")
    for item_id, name, best, second, flag in review:
        print(f"{item_id:<6}{name:<28}{best:>7.2f}{second:>8.2f}  {flag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run it and review the calibration mapping**

Run: `python scripts/seed_qa.py`
Expected: a 20-row table. Read it: for every row marked `yes`, confirm the named file plausibly answers that question (open the file if unsure). Edit `eval/sets/qa.jsonl` to clear `calibration_sources` (set it to `[]`) for any row you disagree with. Rows marked `NO` stay uncalibrated. Report to the user how many rows were calibrated.

- [ ] **Step 5: Append the unanswerable items**

Append this exact content to `eval/sets/qa.jsonl`:

```
{"id":"u001","query":"What is the boiling point of tungsten?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u002","query":"How do I bake sourdough bread?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u003","query":"Who painted the Sistine Chapel ceiling?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u004","query":"What is the tallest mountain in South America?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u005","query":"What are the rules of cricket?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u006","query":"How many moons does Neptune have?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u007","query":"What is the plot of Hamlet?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u008","query":"How do vaccines train the immune system?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u009","query":"What is the population of Nigeria?","origin":"hand","answerable":false,"tags":["unanswerable"]}
{"id":"u010","query":"How does a four-stroke engine work?","origin":"hand","answerable":false,"tags":["unanswerable"]}
```

- [ ] **Step 6: Check that none of the unanswerable topics appears in the staged corpus**

Run:
```bash
python - <<'EOF'
from pathlib import Path
from eval.harness.corpus import extract_text

terms = ["tungsten", "sourdough", "sistine", "south america", "cricket", "neptune", "hamlet", "vaccine", "nigeria", "four-stroke"]
hits = {t: [] for t in terms}
for path in sorted(Path("eval/datasets/eval_corpus").iterdir()):
    try:
        text = extract_text(path, max_pages=999).lower()
    except Exception:
        continue
    for t in terms:
        if t in text:
            hits[t].append(path.name)
print({t: files for t, files in hits.items() if files} or "no term found in the corpus")
EOF
```
Expected: `no term found in the corpus`. If a term appears, that item may be answerable: replace the query in `eval/sets/qa.jsonl` with a different off-topic question and rerun this check.

- [ ] **Step 7: Add hand-written source items**

Use the four filenames chosen in Task 10 Step 2, then add K8s items from `true_data`. Run each with `--pick N` only if the dry-run listed several files:
```bash
python scripts/add_source_item.py --id h001 --query "How does selective state space modeling work?" --phrase "selective state space" --tag ml
python scripts/add_source_item.py --id h002 --query "What are the requirements for KEY_VALUE_BASIC_INFORMATION in Windows?" --phrase "KEY_VALUE_BASIC_INFORMATION" --pages 60 --tag windows
python scripts/add_source_item.py --id h003 --query "How does Oracle optimize history table storage?" --phrase "oracle history table" --tag database
python scripts/add_source_item.py --id h004 --query "What is mutual information estimation?" --phrase "mutual information estimation" --tag ml
python scripts/add_source_item.py --id h005 --query "How do I autoscale pods in Kubernetes?" --phrase "autoscal" --tag k8s
python scripts/add_source_item.py --id h006 --query "How do I run a parallel work queue job?" --phrase "work queue" --tag k8s
python scripts/add_source_item.py --id h007 --query "How do I schedule a CronJob?" --phrase "cronjob" --tag k8s
python scripts/add_source_item.py --id h008 --query "How do I monitor a running Kubernetes job?" --phrase "monitor job" --tag k8s
```
Expected: each prints `added hNNN -> <filename>`. Where a command reports several matches, rerun with the right `--pick N`. For a K8s phrase that matches a noisy PDF instead of a `true_data` file, check the filename is one you would expect to answer the question before keeping it.

- [ ] **Step 8: Verify the retrieval dataset**

Run: `python -c "from eval.harness.datasets import load_qa; items = load_qa('eval/sets/qa.jsonl'); import collections; print(len(items), dict(collections.Counter((i.label_type, i.origin) for i in items)))"`
Expected: `38 {('fuzzy', 'golden'): 20, (None, 'hand'): 10, ('source', 'hand'): 8}`. If fewer than eight source items were added, report which ones failed to match.

- [ ] **Step 9: Commit (if authorised)**

```bash
git add eval/sets/routing.jsonl eval/sets/qa.jsonl scripts/seed_qa.py
git commit -m "feat(eval): add seed routing and retrieval datasets

Routing seeds come from eval_pipeline.py and the demo plan with tool labels
and calculator output checks; retrieval seeds mix golden fuzzy items,
hand-written source items and off-corpus unanswerable items."
```

---

### Task 12: Live runs and baselines

**Files:**
- Create: `eval/baselines/routing.json`, `eval/baselines/retrieval.json` (generated)

**Interfaces:**
- Consumes: everything above, with the stack up (Task 10) and the API's LLM endpoint reachable.
- Produces: two run directories under `eval/runs/` (gitignored) and two baselines.

Anything unexpected in these runs is a **finding to report**, not a test to force green. Do not change production code.

- [ ] **Step 1: Run routing with repeats**

Run: `python -m eval.harness routing --repeats 3; echo "exit=$?"`
Expected: `run written to eval/runs/<stamp>-routing`, headline lines for `pass_rate`, `action_accuracy` and `tool_accuracy`, and `exit=0`. If `exit=2` with `INVALID RUN`, the LLM endpoint is failing (check `RAY_LLM_ENDPOINT` and `LLM_API_KEY`); if preflight failed, follow the printed fix.

- [ ] **Step 2: Read the routing summary and check it for sanity**

Run: `cat eval/runs/*-routing/summary.md | head -80` (use the newest run directory).
Check, and report to the user:
- The manifest shows the model `openrouter/owl-alpha`, host `openrouter.ai`, and a git SHA.
- `planner_failed` is 0. If not, say what fraction.
- `r013` and `r014` appear under Failures with `tool_check_failed`, confirming the calculator gap.
- `r061` appears under the `known-limitation` tag, and is not counted in the headline.
- The Safeguards section shows a `value` and any fired safeguards, and `Simulation mismatches` is 0. A non-zero mismatch count means the harness's copy of the planner's coercion logic has drifted from `planner.py`; report it.
- Stability lists which items flip across the 3 repeats, if any.

- [ ] **Step 3: Run retrieval**

Run: `python -m eval.harness retrieval; echo "exit=$?"`
Expected: preflight warnings about Neo4j being unavailable (unless you started it), then `run written to eval/runs/<stamp>-retrieval`, `source_hit@1/3/5` lines, and `exit=0`. Preflight errors are printed with the fix command.

- [ ] **Step 4: Read the retrieval summary and check it for sanity**

Run: `cat eval/runs/*-retrieval/summary.md | head -80` (newest run directory).
Check, and report:
- `unscorable` is empty. Any listed item means its source file was not ingested; identify it.
- Source hit@5 for the `true_data`-based items (h005 to h008) is high. A low value means either the chunks are not being found or the label is wrong; inspect the failing rows.
- The Fuzzy section shows a threshold and an agreement rate, and states whether it is reliable. Report the number.
- The Unanswerable section shows an AUROC and says "descriptive only", with the suggested cutoff.
- Arms show vector documents greater than zero and graph documents 0.

- [ ] **Step 5: Confirm retrieval is deterministic by running it twice against a baseline**

Run:
```bash
python -m eval.harness retrieval --set-baseline
python -m eval.harness retrieval --compare; echo "exit=$?"
```
Expected: `baseline saved: eval/baselines/retrieval.json`, then a second run whose `## Comparison with baseline` table shows every delta `+0.0 pts` and `within noise`, and `exit=0`. Retrieval has no LLM in the loop, so any non-zero delta means something in the environment changed between runs; report it.

- [ ] **Step 6: Save the routing baseline and check the comparability guard**

Run:
```bash
python -m eval.harness routing --repeats 3 --set-baseline
python -m eval.harness routing --repeats 3 --compare; echo "exit=$?"
```
Expected: `baseline saved: eval/baselines/routing.json`, then a comparison table. Routing depends on a hosted model, so small deltas labelled `within noise` are normal; report the actual deltas.

Then check that the guard refuses a mismatched baseline. Run: `LLM_MODEL_ID=some-other-model python -m eval.harness routing --compare; echo "exit=$?"`
Expected: a warning that the baseline is not comparable (`model` differs), a run still written, and `exit=2`. (This run may fail preflight if the other model name is invalid for the endpoint; if so, note it and skip.)

- [ ] **Step 7: Commit the baselines (if authorised)**

```bash
git add eval/baselines/routing.json eval/baselines/retrieval.json
git commit -m "feat(eval): record first routing and retrieval baselines

Baselines carry their manifest so later runs can refuse to compare across a
different model, dataset or corpus."
```

---

### Task 13: Documentation sync

**Files:**
- Modify: `CLAUDE.md`, `docs/eval-harness-design.md`, `docs/component-six-questions.md`

**Interfaces:**
- Consumes: the baselines and summaries from Task 12.

- [ ] **Step 1: Document the commands in `CLAUDE.md`**

In the `## Source layout` block, replace the `eval/` subsection with:

```
eval/
  harness/               # routing + retrieval eval: python -m eval.harness routing|retrieval
  sets/                  # routing.jsonl, qa.jsonl, corpus_manifest.json
  baselines/             # committed baselines (each carries its run manifest)
  judges/llm_judge.py    # LLM-as-judge scoring (not wired to the harness)
  ragas/run.py           # RAGAS harness (ragas not installed; not wired)
```

After the `## Check ingestion status` section, add:

```
## Eval harness

Run from the repo root with the local stack up (Qdrant, Ollama; Neo4j optional):

    python -m eval.harness routing --repeats 3
    python -m eval.harness retrieval
    python -m eval.harness retrieval --set-baseline    # then later: --compare

Runs land in `eval/runs/` (gitignored). Exit code 2 means preflight failed or the run is invalid.
Run with `.venv-eval/bin/python` (or activate it): an overlay venv that pins `qdrant-client==1.7.3`, the API's version.
Recreate it with `python -m venv --system-site-packages .venv-eval && .venv-eval/bin/python -m pip install --no-deps qdrant-client==1.7.3`.
Corpus: `python -m eval.harness.corpus build|stage`.
```

- [ ] **Step 2: Update the design doc's status and prerequisite table**

In `docs/eval-harness-design.md`: change the line `Status: draft for review, 2026-09-21. Nothing in this document is implemented.` to `Status: slice 1 implemented; see docs/eval-harness-plan.md.` Update the prerequisites table so the `qdrant-client` row, the collection row and the ingest row read `Done`, with the actual point count and ingest duration you recorded in Task 10.

- [ ] **Step 3: Update the six-questions doc with measured numbers**

In `docs/component-six-questions.md`, for the Planner and Retriever entries, replace the `**Quality:**` lines with the measured results (headline rates with intervals, safeguard value, fuzzy agreement, unanswerable AUROC) and a pointer to `eval/baselines/`. Leave the Responder, Memory and Cache entries as `unmeasured`.

- [ ] **Step 4: Verify the whole suite still passes and the docs render**

Run: `python -m pytest tests/eval_harness -q`
Expected: `88 passed`

- [ ] **Step 5: Offer the DEVLOG entry, and commit if authorised**

Do not write to `DEVLOG.md` unprompted. Ask the user whether to log the design decisions worth keeping (in-process runner over HTTP, passive recorders instead of production changes, the `qdrant-client` pin finding, planner-failed versus invalid-JSON separation).

```bash
git add CLAUDE.md docs/eval-harness-design.md docs/component-six-questions.md
git commit -m "docs: document the eval harness and record measured baselines

The six-questions doc claimed planner and retriever quality was unmeasured;
it now points at the committed baselines."
```

---

## Self-Review

**Spec coverage** (design doc section to task):
- Purpose, scope, decisions: architecture header, Global Constraints.
- Layout and units: File Structure; Tasks 1 to 8 and 9.
- Runner behaviour (`.env`, `llm_client` start, sequential, warm-up via probes, `--repeats`, unchanged `retrieve_node`, loud recorder failure): Tasks 3, 4, 6, 8.
- Manifest: Task 6. Preflight (layer-specific): Task 6.
- Datasets: schemas Task 1; seeds Task 11.
- Routing scoring (verdicts, `planner_failed` vs invalid JSON, 20% invalid, safeguard value, stability, tool execution check): Task 4.
- Retrieval scoring (recorder-based ranking, source metrics, arms, fuzzy calibration, unanswerable AUROC): Tasks 3 and 5.
- Report, baselines, Wilson noise, comparability, exit codes: Tasks 7 and 8.
- Corpus (sample, manifest, staging): Tasks 9 and 10. Environment prerequisites: Tasks 0 and 10.
- Slice 3 interface commitments: the manifest `judge` field (Task 6). The `agent_app` streaming capability and the 3a and 3b checks are deliberately **not** in this plan; they belong to a later plan.
- Known limits: recorded in the design doc; Task 12 checks the ones observable in a run.

**Placeholder scan:** the only fill-in points are filenames the executor must read from a dry run (Task 10 Step 2, Task 11 Step 7), which cannot be known in advance; each has a command that prints them.

**Type consistency:** `RoutingResult` and `RetrievalResult` fields match between Tasks 4, 5, 7 and 8 (`asdict` rows use `item_id`, `verdict`, `ranked`, `reason`, `tool_check`, `actual_action`, `actual_tool`). `summarize_*` return `headline` as `{name: {"successes", "n"}}`, which `compare_headline` and `save_baseline` consume. `run_preflight` facts (`filenames`, `qdrant_points`, `qdrant_vector_size`, `neo4j_nodes`) are the keys `cli._run` reads. Test totals: 13 + 7 + 8 + 14 + 10 + 5 + 11 + 9 + 6 + 5 = 88.
