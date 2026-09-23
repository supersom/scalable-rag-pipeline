import asyncio

from eval.harness.answer import (
    AnswerResult, citation_filenames, has_error_leak, is_refusal, run_calculator_answers, run_qa_answers,
    summarize_answer,
)
from eval.harness.datasets import QAItem, RoutingItem

NO_ANSWER_PHRASES = ["i don't have", "i do not have", "no relevant"]


class FakeRecorder:
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def last(self):
        return self.value


def qa(id, answerable=True):
    return QAItem(id=id, query="q", origin="hand", answerable=answerable, label_type=None,
                 expected_sources=(), reference_contexts=(), calibration_sources=(), ground_truth=None, tags=())


def calc_item(id, query="3*7", output="21"):
    return RoutingItem(id=id, query=query, expected_action="tool_use", expected_tool="calculator", tags=(),
                       also_accept=(), expected_tool_output=output)


def hits(*filenames):
    return [{"filename": f, "page": 1, "score": 0.9, "text": "t"} for f in filenames]


def agent(recorder, filenames, answer, action="retrieve"):
    async def fn(state):
        recorder.value = hits(*filenames)
        return {"messages": [{"role": "assistant", "content": answer}], "action": action}
    return fn


def test_citation_filenames_extracts_and_strips():
    text = "Fact one [Source: a.pdf]. Fact two [Source:  b.pdf ]."
    assert citation_filenames(text) == ["a.pdf", "b.pdf"]
    assert citation_filenames("no citations here") == []


def test_is_refusal_matches_case_insensitively():
    assert is_refusal("I don't have that information in my documents.", NO_ANSWER_PHRASES)
    assert is_refusal("NO RELEVANT documents found", NO_ANSWER_PHRASES)
    assert not is_refusal("The HPA scales pods based on CPU.", NO_ANSWER_PHRASES)


def test_has_error_leak_matches_known_tool_error_strings():
    assert has_error_leak("The result is Sandbox Error: timeout")
    assert has_error_leak("Unknown tool: 'frobnicate'. Available: []")
    assert not has_error_leak("The Horizontal Pod Autoscaler scales replicas.")


def test_run_qa_answers_counts_hallucinated_citations():
    rec = FakeRecorder()
    fn = agent(rec, ["a.pdf"], "The answer is X [Source: a.pdf], also see [Source: ghost.pdf].")
    [result] = asyncio.run(run_qa_answers([qa("s1")], fn, rec, NO_ANSWER_PHRASES))
    assert result.verdict == "scored"
    assert result.citations_total == 2 and result.citations_hallucinated == 1
    assert result.refused is False


def test_run_qa_answers_detects_refusal_on_unanswerable_item():
    rec = FakeRecorder()
    fn = agent(rec, [], "I don't have that information in my documents.")
    [result] = asyncio.run(run_qa_answers([qa("u1", answerable=False)], fn, rec, NO_ANSWER_PHRASES))
    assert result.refused is True and result.answerable is False


def test_run_qa_answers_records_errors():
    async def boom(state):
        raise RuntimeError("llm down")

    [result] = asyncio.run(run_qa_answers([qa("s1")], boom, FakeRecorder(), NO_ANSWER_PHRASES))
    assert result.verdict == "error" and "RuntimeError: llm down" in result.reason
    assert result.answerable is True


def test_run_calculator_answers_checks_number_present():
    rec = FakeRecorder()
    fn = agent(rec, [], "3 times 7 is 21.", action="tool_use")
    [result] = asyncio.run(run_calculator_answers([calc_item("c1")], fn, rec, NO_ANSWER_PHRASES))
    assert result.number_present is True and result.expected_number == "21"


def test_run_calculator_answers_flags_missing_number():
    rec = FakeRecorder()
    fn = agent(rec, [], "I'm not sure what the result is.", action="tool_use")
    [result] = asyncio.run(run_calculator_answers([calc_item("c1")], fn, rec, NO_ANSWER_PHRASES))
    assert result.number_present is False


def scored(item_id, **kwargs):
    defaults = dict(verdict="scored", latency_ms=5.0)
    defaults.update(kwargs)
    return AnswerResult(item_id=item_id, **defaults)


def test_summarize_answer():
    qa_results = [
        scored("s1", answerable=True, citations_total=2, citations_hallucinated=0, refused=False),
        scored("s2", answerable=True, citations_total=1, citations_hallucinated=1, refused=False),
        scored("s3", answerable=True, citations_total=0, refused=True),  # false refusal, no citations
        scored("u1", answerable=False, refused=True),
        scored("u2", answerable=False, refused=False, error_leak=True),  # missed refusal + leaked error
    ]
    calc_results = [
        scored("c1", expected_number="21", number_present=True),
        scored("c2", expected_number="45", number_present=False),
    ]
    s = summarize_answer(qa_results, calc_results)
    assert s["headline"]["citation_clean_rate"] == {"successes": 1, "n": 2}
    assert s["headline"]["correct_refusal_rate"] == {"successes": 1, "n": 2}
    assert s["headline"]["false_refusal_rate"] == {"successes": 1, "n": 3}
    assert s["headline"]["calculator_number_present_rate"] == {"successes": 1, "n": 2}
    assert s["citation_validity"] == {"n_citations": 3, "n_hallucinated": 1, "n_items_with_citations": 2}
    assert s["error_leak"] == {"n_checked": 7, "n_leaked": 1}
    assert s["errors"] == 0 and s["invalid"] is False
    assert s["latency_ms"] == {"p50": 5.0, "p95": 5.0}
    assert s["n_items"] == 7


def test_summarize_answer_omits_headline_metrics_with_no_items():
    s = summarize_answer([scored("s1", answerable=True, refused=False)], [])
    assert "correct_refusal_rate" not in s["headline"]
    assert "calculator_number_present_rate" not in s["headline"]
    assert s["headline"]["false_refusal_rate"] == {"successes": 0, "n": 1}


def test_summarize_answer_counts_errors_separately_from_scored():
    qa_results = [scored("s1", answerable=True, refused=False), AnswerResult(item_id="s2", verdict="error")]
    s = summarize_answer(qa_results, [])
    assert s["errors"] == 1 and s["n_items"] == 2
