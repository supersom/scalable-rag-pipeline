import asyncio

from eval.harness.answer import (
    AnswerResult, citation_filenames, has_error_leak, is_refusal, judge_qa_answers, run_calculator_answers,
    run_qa_answers, summarize_answer,
)
from eval.harness.datasets import QAItem, RoutingItem
from eval.judges.llm_judge import Grade

NO_ANSWER_PHRASES = ["i don't have", "i do not have", "no relevant"]


class FakeRecorder:
    def __init__(self):
        self.value = None

    def reset(self):
        self.value = None

    def last(self):
        return self.value


def qa(id, answerable=True, ground_truth=None, origin="hand"):
    return QAItem(id=id, query="q", origin=origin, answerable=answerable, label_type=None,
                 expected_sources=(), reference_contexts=(), calibration_sources=(), ground_truth=ground_truth,
                 tags=())


def calc_item(id, query="3*7", output="21"):
    return RoutingItem(id=id, query=query, expected_action="tool_use", expected_tool="calculator", tags=(),
                       also_accept=(), expected_tool_output=output)


def hits(*filenames):
    return [{"filename": f, "page": 1, "score": 0.9, "text": "t"} for f in filenames]


def agent(recorder, filenames, answer, action="retrieve", documents=None):
    async def fn(state):
        recorder.value = hits(*filenames)
        return {"messages": [{"role": "assistant", "content": answer}], "action": action,
                "documents": documents if documents is not None else []}
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


def test_run_qa_answers_captures_origin():
    rec = FakeRecorder()
    fn = agent(rec, [], "hi")
    [result] = asyncio.run(run_qa_answers([qa("g1", origin="golden")], fn, rec, NO_ANSWER_PHRASES))
    assert result.origin == "golden"


def test_run_qa_answers_records_errors():
    async def boom(state):
        raise RuntimeError("llm down")

    [result] = asyncio.run(run_qa_answers([qa("s1")], boom, FakeRecorder(), NO_ANSWER_PHRASES))
    assert result.verdict == "error" and "RuntimeError: llm down" in result.reason
    assert result.answerable is True


def test_run_qa_answers_captures_joined_retrieved_context():
    rec = FakeRecorder()
    fn = agent(rec, ["a.pdf"], "an answer", documents=["chunk one [Source: a.pdf]", "chunk two [Source: a.pdf]"])
    [result] = asyncio.run(run_qa_answers([qa("s1")], fn, rec, NO_ANSWER_PHRASES))
    assert result.context == "chunk one [Source: a.pdf]\n\nchunk two [Source: a.pdf]"


def test_run_qa_answers_context_is_empty_string_when_no_documents():
    rec = FakeRecorder()
    fn = agent(rec, [], "hi there", action="direct_answer")
    [result] = asyncio.run(run_qa_answers([qa("s1")], fn, rec, NO_ANSWER_PHRASES))
    assert result.context == ""


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


def judge_fn(*grades):
    calls = []

    async def fn(question, reference, answer):
        calls.append((question, reference, answer))
        return grades[len(calls) - 1]
    fn.calls = calls
    return fn


def test_judge_qa_answers_scores_items_with_ground_truth():
    items = [qa("s1", ground_truth="the real answer"), qa("s2")]
    results = [
        AnswerResult("s1", "scored", answer="an answer", context="ctx"),
        AnswerResult("s2", "scored", answer="another answer", context="ctx2"),
    ]
    correctness = judge_fn(Grade(score=5, reasoning="matches"))
    faithfulness = judge_fn(Grade(score=4, reasoning="grounded"))
    judged = asyncio.run(judge_qa_answers(items, results, correctness, faithfulness))
    s1, s2 = judged
    assert s1.correctness_score == 5 and s1.correctness_reasoning == "matches"
    assert s1.faithfulness_score == 4 and s1.faithfulness_reasoning == "grounded"
    assert s2.correctness_score is None and s2.faithfulness_score is None
    assert results[0].correctness_score is None  # original result untouched, not mutated


def test_judge_qa_answers_passes_ground_truth_to_correctness_and_context_to_faithfulness():
    items = [qa("s1", ground_truth="the real answer")]
    results = [AnswerResult("s1", "scored", answer="an answer", context="the retrieved context")]
    correctness = judge_fn(Grade(score=5, reasoning="ok"))
    faithfulness = judge_fn(Grade(score=5, reasoning="ok"))
    asyncio.run(judge_qa_answers(items, results, correctness, faithfulness))
    assert correctness.calls == [("q", "the real answer", "an answer")]
    assert faithfulness.calls == [("q", "the retrieved context", "an answer")]


def test_judge_qa_answers_skips_error_and_no_ground_truth_results():
    items = [qa("s1", ground_truth="gt"), qa("s2")]
    results = [AnswerResult("s1", "error", reason="boom"), AnswerResult("s2", "scored", answer="a")]
    correctness = judge_fn()
    faithfulness = judge_fn()
    judged = asyncio.run(judge_qa_answers(items, results, correctness, faithfulness))
    assert correctness.calls == [] and faithfulness.calls == []
    assert all(r.correctness_score is None for r in judged)


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


def test_summarize_answer_omits_judge_metrics_when_no_results_were_judged():
    s = summarize_answer([scored("s1", answerable=True, refused=False)], [])
    assert "correctness_pass_rate" not in s["headline"] and "faithfulness_pass_rate" not in s["headline"]
    assert "judge_quality" not in s


def test_summarize_answer_reports_judge_pass_rates_and_mean_scores():
    qa_results = [
        scored("s1", answerable=True, refused=False, correctness_score=5, correctness_reasoning="ok",
              faithfulness_score=2, faithfulness_reasoning="unsupported"),
        scored("s2", answerable=True, refused=False, correctness_score=3, correctness_reasoning="partial",
              faithfulness_score=4, faithfulness_reasoning="mostly grounded"),
        scored("s3", answerable=True, refused=False),  # not judged (no ground_truth)
    ]
    s = summarize_answer(qa_results, [])
    assert s["headline"]["correctness_pass_rate"] == {"successes": 1, "n": 2}  # score>=4 -> only s1 passes
    assert s["headline"]["faithfulness_pass_rate"] == {"successes": 1, "n": 2}  # only s2 passes
    assert s["judge_quality"] == {"n_judged": 2, "mean_correctness": 4.0, "mean_faithfulness": 3.0,
                                  "n_judge_errors": 0}


def test_summarize_answer_excludes_judge_call_failures_from_rates_and_means():
    qa_results = [
        scored("s1", answerable=True, refused=False, correctness_score=5, correctness_reasoning="ok",
              faithfulness_score=0, faithfulness_reasoning="Judge Error: down"),
        scored("s2", answerable=True, refused=False, correctness_score=0, correctness_reasoning="Judge Error: down",
              faithfulness_score=4, faithfulness_reasoning="grounded"),
    ]
    s = summarize_answer(qa_results, [])
    assert s["headline"]["correctness_pass_rate"] == {"successes": 1, "n": 1}
    assert s["headline"]["faithfulness_pass_rate"] == {"successes": 1, "n": 1}
    assert s["judge_quality"] == {"n_judged": 1, "mean_correctness": 5.0, "mean_faithfulness": 4.0,
                                  "n_judge_errors": 2}


def test_summarize_answer_groups_refusal_and_citation_metrics_by_origin():
    qa_results = [
        scored("h1", origin="hand", answerable=True, refused=False, citations_total=1, citations_hallucinated=0),
        scored("h2", origin="hand", answerable=False, refused=True),
        scored("g1", origin="golden", answerable=True, refused=True),
        scored("g2", origin="golden", answerable=True, refused=True),
    ]
    s = summarize_answer(qa_results, [])
    assert s["groups"]["hand"] == {"n": 2, "false_refusal_rate": 0.0, "correct_refusal_rate": 1.0,
                                   "citation_clean_rate": 1.0}
    assert s["groups"]["golden"] == {"n": 2, "false_refusal_rate": 1.0}
