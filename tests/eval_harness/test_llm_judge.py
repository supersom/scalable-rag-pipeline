import asyncio
import json

from eval.judges.llm_judge import Grade, grade_answer, grade_faithfulness


def chat_returning(text):
    async def fn(messages, temperature=0.0):
        return text
    return fn


def chat_raising(exc):
    async def fn(messages, temperature=0.0):
        raise exc
    return fn


def test_grade_answer_parses_score_and_reasoning():
    chat_fn = chat_returning(json.dumps({"score": 5, "reasoning": "matches ground truth exactly"}))
    grade = asyncio.run(grade_answer("q", "gt", "answer", chat_fn))
    assert grade == Grade(score=5, reasoning="matches ground truth exactly")


def test_grade_answer_prompt_includes_question_ground_truth_and_answer():
    captured = {}

    async def fn(messages, temperature=0.0):
        captured["prompt"] = messages[0]["content"]
        return json.dumps({"score": 3, "reasoning": "partial"})

    asyncio.run(grade_answer("What is HPA?", "HPA scales pods", "HPA scales replicas", fn))
    assert "What is HPA?" in captured["prompt"]
    assert "HPA scales pods" in captured["prompt"]
    assert "HPA scales replicas" in captured["prompt"]


def test_grade_answer_degrades_to_zero_on_chat_failure():
    grade = asyncio.run(grade_answer("q", "gt", "a", chat_raising(RuntimeError("down"))))
    assert grade.score == 0 and "Judge Error" in grade.reasoning and "down" in grade.reasoning


def test_grade_answer_degrades_to_zero_on_unparseable_response():
    grade = asyncio.run(grade_answer("q", "gt", "a", chat_returning("not json")))
    assert grade.score == 0 and "Judge Error" in grade.reasoning


def test_grade_faithfulness_parses_score_and_reasoning():
    chat_fn = chat_returning(json.dumps({"score": 4, "reasoning": "mostly grounded"}))
    grade = asyncio.run(grade_faithfulness("q", "the retrieved context text", "answer", chat_fn))
    assert grade == Grade(score=4, reasoning="mostly grounded")


def test_grade_faithfulness_prompt_uses_context_not_ground_truth():
    captured = {}

    async def fn(messages, temperature=0.0):
        captured["prompt"] = messages[0]["content"]
        return json.dumps({"score": 2, "reasoning": "unsupported claim"})

    asyncio.run(grade_faithfulness("What is HPA?", "HPA is a Kubernetes autoscaler.", "HPA runs on GPUs.", fn))
    assert "HPA is a Kubernetes autoscaler." in captured["prompt"]
    assert "HPA runs on GPUs." in captured["prompt"]
    assert "ground truth" not in captured["prompt"].lower()


def test_grade_faithfulness_degrades_to_zero_on_chat_failure():
    grade = asyncio.run(grade_faithfulness("q", "ctx", "a", chat_raising(RuntimeError("down"))))
    assert grade.score == 0 and "Judge Error" in grade.reasoning
