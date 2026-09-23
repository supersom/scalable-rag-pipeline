import re
import time
from dataclasses import dataclass, replace
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

# llm_judge.py's rubric: 5=perfect, 3=partially correct. 4 is the "good enough" cutoff.
JUDGE_PASS_THRESHOLD = 4


@dataclass
class AnswerResult:
    item_id: str
    verdict: str
    answer: str = ""
    action: Optional[str] = None
    answerable: Optional[bool] = None
    origin: Optional[str] = None
    context: str = ""
    citations_total: int = 0
    citations_hallucinated: int = 0
    refused: Optional[bool] = None
    error_leak: bool = False
    expected_number: Optional[str] = None
    number_present: Optional[bool] = None
    correctness_score: Optional[int] = None
    correctness_reasoning: Optional[str] = None
    faithfulness_score: Optional[int] = None
    faithfulness_reasoning: Optional[str] = None
    latency_ms: Optional[float] = None
    reason: Optional[str] = None


def citation_filenames(answer):
    return [c.strip() for c in CITATION_RE.findall(answer)]


def is_refusal(answer, no_answer_phrases):
    lower = answer.lower()
    return any(phrase in lower for phrase in no_answer_phrases)


def has_error_leak(answer):
    return any(p in answer for p in ERROR_LEAK_PATTERNS)


async def _invoke(item_id, query, agent_fn, recorder, no_answer_phrases, *, answerable=None, origin=None,
                  expected_number=None):
    recorder.reset()
    state = {"messages": [{"role": "user", "content": query}], "current_query": query,
             "action": "retrieve", "tool_name": "", "tool_input": "", "documents": [], "plan": []}
    started = time.perf_counter()
    try:
        out = await agent_fn(state)
    except Exception as e:
        return AnswerResult(item_id=item_id, verdict="error", answerable=answerable, origin=origin,
                            expected_number=expected_number, reason=f"{type(e).__name__}: {e}",
                            latency_ms=(time.perf_counter() - started) * 1000)
    latency_ms = (time.perf_counter() - started) * 1000
    messages = out.get("messages") or []
    answer = messages[-1]["content"] if messages else ""
    context = "\n\n".join(out.get("documents") or [])
    retrieved = {h["filename"] for h in (recorder.last() or [])}
    citations = citation_filenames(answer)
    hallucinated = sum(1 for c in citations if c not in retrieved)
    number_present = (expected_number in answer) if expected_number is not None else None
    return AnswerResult(
        item_id=item_id, verdict="scored", answer=answer, action=out.get("action"), answerable=answerable,
        origin=origin, context=context, citations_total=len(citations), citations_hallucinated=hallucinated,
        refused=is_refusal(answer, no_answer_phrases), error_leak=has_error_leak(answer),
        expected_number=expected_number, number_present=number_present, latency_ms=latency_ms)


async def run_qa_answers(items, agent_fn, recorder, no_answer_phrases):
    return [await _invoke(i.id, i.query, agent_fn, recorder, no_answer_phrases, answerable=i.answerable,
                          origin=i.origin)
            for i in items]


async def run_calculator_answers(items, agent_fn, recorder, no_answer_phrases):
    return [await _invoke(i.id, i.query, agent_fn, recorder, no_answer_phrases, expected_number=i.expected_tool_output)
            for i in items]


async def judge_qa_answers(items, results, correctness_fn, faithfulness_fn):
    by_id = {i.id: i for i in items}
    judged = []
    for r in results:
        item = by_id[r.item_id]
        if r.verdict != "scored" or not item.ground_truth:
            judged.append(r)
            continue
        correctness = await correctness_fn(item.query, item.ground_truth, r.answer)
        faithfulness = await faithfulness_fn(item.query, r.context, r.answer)
        judged.append(replace(r, correctness_score=correctness.score, correctness_reasoning=correctness.reasoning,
                              faithfulness_score=faithfulness.score, faithfulness_reasoning=faithfulness.reasoning))
    return judged


def _rate(successes, n):
    return {"successes": successes, "n": n}


def _mean(values):
    return sum(values) / len(values) if values else None


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

    # score==0 is llm_judge.py's degrade-on-failure sentinel (the 1-5 rubric never emits it
    # for a real judgement), so it means "the judge call itself failed", not "the answer failed".
    correctness_judged = [r for r in scored if r.correctness_score is not None and r.correctness_score > 0]
    faithfulness_judged = [r for r in scored if r.faithfulness_score is not None and r.faithfulness_score > 0]
    n_judge_errors = sum(r.correctness_score == 0 for r in scored) + sum(r.faithfulness_score == 0 for r in scored)
    if correctness_judged:
        headline["correctness_pass_rate"] = _rate(
            sum(r.correctness_score >= JUDGE_PASS_THRESHOLD for r in correctness_judged), len(correctness_judged))
    if faithfulness_judged:
        headline["faithfulness_pass_rate"] = _rate(
            sum(r.faithfulness_score >= JUDGE_PASS_THRESHOLD for r in faithfulness_judged), len(faithfulness_judged))

    groups = {}
    for origin in sorted({r.origin for r in scored if r.origin}):
        rows = [r for r in scored if r.origin == origin]
        ans = [r for r in rows if r.answerable is True]
        unans = [r for r in rows if r.answerable is False]
        cited = [r for r in rows if r.citations_total > 0]
        entry = {"n": len(rows)}
        if ans:
            entry["false_refusal_rate"] = sum(r.refused for r in ans) / len(ans)
        if unans:
            entry["correct_refusal_rate"] = sum(r.refused for r in unans) / len(unans)
        if cited:
            entry["citation_clean_rate"] = sum(r.citations_hallucinated == 0 for r in cited) / len(cited)
        groups[origin] = entry

    all_results = qa_results + calc_results
    checked = [r for r in all_results if r.verdict == "scored"]
    latencies = [r.latency_ms for r in all_results if r.latency_ms is not None]

    summary = {
        "headline": headline,
        "groups": groups,
        "citation_validity": {"n_citations": sum(r.citations_total for r in scored),
                              "n_hallucinated": sum(r.citations_hallucinated for r in scored),
                              "n_items_with_citations": len(with_citations)},
        "error_leak": {"n_checked": len(checked), "n_leaked": sum(r.error_leak for r in checked)},
        "errors": sum(r.verdict == "error" for r in all_results),
        "invalid": False,
        "latency_ms": {"p50": percentile(latencies, 50), "p95": percentile(latencies, 95)},
        "n_items": len(qa_results) + len(calc_results),
    }
    if correctness_judged or faithfulness_judged:
        summary["judge_quality"] = {
            "n_judged": max(len(correctness_judged), len(faithfulness_judged)),
            "mean_correctness": _mean([r.correctness_score for r in correctness_judged]),
            "mean_faithfulness": _mean([r.faithfulness_score for r in faithfulness_judged]),
            "n_judge_errors": n_judge_errors,
        }
    return summary
