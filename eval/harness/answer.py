import re
import time
from dataclasses import dataclass
from typing import Optional

from eval.harness.stats import percentile

CITATION_RE = re.compile(r"\[Source:\s*([^\]]+)\]")

# Substrings the actual tool/retriever code returns on failure (services/api/app/tools/*.py,
# services/api/app/agents/nodes/tool.py). The responder prompt has no special-casing for tool
# errors, so these can end up presented to the user as if they were a real answer.
ERROR_LEAK_PATTERNS = (
    "Error:", "Execution Error:", "Sandbox Error:", "Sandbox Connection Failed:",
    "Search Error:", "Graph search error:", "Unknown tool:", "Web Search Error:",
)


@dataclass
class AnswerResult:
    item_id: str
    verdict: str
    answer: str = ""
    action: Optional[str] = None
    answerable: Optional[bool] = None
    citations_total: int = 0
    citations_hallucinated: int = 0
    refused: Optional[bool] = None
    error_leak: bool = False
    expected_number: Optional[str] = None
    number_present: Optional[bool] = None
    latency_ms: Optional[float] = None
    reason: Optional[str] = None


def citation_filenames(answer):
    return [c.strip() for c in CITATION_RE.findall(answer)]


def is_refusal(answer, no_answer_phrases):
    lower = answer.lower()
    return any(phrase in lower for phrase in no_answer_phrases)


def has_error_leak(answer):
    return any(p in answer for p in ERROR_LEAK_PATTERNS)


async def _invoke(item_id, query, agent_fn, recorder, no_answer_phrases, *, answerable=None, expected_number=None):
    recorder.reset()
    state = {"messages": [{"role": "user", "content": query}], "current_query": query,
             "action": "retrieve", "tool_name": "", "tool_input": "", "documents": [], "plan": []}
    started = time.perf_counter()
    try:
        out = await agent_fn(state)
    except Exception as e:
        return AnswerResult(item_id=item_id, verdict="error", answerable=answerable, expected_number=expected_number,
                            reason=f"{type(e).__name__}: {e}", latency_ms=(time.perf_counter() - started) * 1000)
    latency_ms = (time.perf_counter() - started) * 1000
    messages = out.get("messages") or []
    answer = messages[-1]["content"] if messages else ""
    retrieved = {h["filename"] for h in (recorder.last() or [])}
    citations = citation_filenames(answer)
    hallucinated = sum(1 for c in citations if c not in retrieved)
    number_present = (expected_number in answer) if expected_number is not None else None
    return AnswerResult(
        item_id=item_id, verdict="scored", answer=answer, action=out.get("action"), answerable=answerable,
        citations_total=len(citations), citations_hallucinated=hallucinated,
        refused=is_refusal(answer, no_answer_phrases), error_leak=has_error_leak(answer),
        expected_number=expected_number, number_present=number_present, latency_ms=latency_ms)


async def run_qa_answers(items, agent_fn, recorder, no_answer_phrases):
    return [await _invoke(i.id, i.query, agent_fn, recorder, no_answer_phrases, answerable=i.answerable)
            for i in items]


async def run_calculator_answers(items, agent_fn, recorder, no_answer_phrases):
    return [await _invoke(i.id, i.query, agent_fn, recorder, no_answer_phrases, expected_number=i.expected_tool_output)
            for i in items]


def _rate(successes, n):
    return {"successes": successes, "n": n}


def summarize_answer(qa_results, calc_results):
    scored = [r for r in qa_results if r.verdict == "scored"]
    with_citations = [r for r in scored if r.citations_total > 0]
    answerable = [r for r in scored if r.answerable is True]
    unanswerable = [r for r in scored if r.answerable is False]
    calc_scored = [r for r in calc_results if r.verdict == "scored"]

    headline = {}
    if with_citations:
        headline["citation_clean_rate"] = _rate(
            sum(r.citations_hallucinated == 0 for r in with_citations), len(with_citations))
    if unanswerable:
        headline["correct_refusal_rate"] = _rate(sum(r.refused for r in unanswerable), len(unanswerable))
    if answerable:
        headline["false_refusal_rate"] = _rate(sum(r.refused for r in answerable), len(answerable))
    if calc_scored:
        headline["calculator_number_present_rate"] = _rate(
            sum(bool(r.number_present) for r in calc_scored), len(calc_scored))

    all_results = qa_results + calc_results
    checked = [r for r in all_results if r.verdict == "scored"]
    latencies = [r.latency_ms for r in all_results if r.latency_ms is not None]

    return {
        "headline": headline,
        "citation_validity": {"n_citations": sum(r.citations_total for r in scored),
                              "n_hallucinated": sum(r.citations_hallucinated for r in scored),
                              "n_items_with_citations": len(with_citations)},
        "error_leak": {"n_checked": len(checked), "n_leaked": sum(r.error_leak for r in checked)},
        "errors": sum(r.verdict == "error" for r in all_results),
        "invalid": False,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "n_items": len(qa_results) + len(calc_results),
    }
