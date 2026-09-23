# eval/judges/llm_judge.py
import json

import httpx
from pydantic import BaseModel

OLLAMA_ENDPOINT = "http://localhost:11434/v1/chat/completions"
# qwen3.5:latest (9.7B) was tried first but measured >2min/call on this hardware (mostly
# CPU-bound — only ~2.2GB of the 6.6GB model fit in VRAM), timing out ollama_chat's own
# 120s client timeout. llama3.2:3b is small enough to run fully in VRAM and finish in seconds.
OLLAMA_JUDGE_MODEL = "llama3.2:3b"


class Grade(BaseModel):
    score: int
    reasoning: str


CORRECTNESS_PROMPT = """
You are an impartial judge evaluating a RAG system.
You will be given a Question, a Ground Truth Answer, and the System's Answer.

Rate the System's Answer on a scale of 1 to 5:
1: Completely wrong or hallucinated.
3: Partially correct but missing key details.
5: Perfect, comprehensive, and matches Ground Truth logic.

Output JSON only: {{"score": int, "reasoning": "string"}}

Question: {question}
Ground Truth: {ground_truth}
System Answer: {system_answer}
"""

FAITHFULNESS_PROMPT = """
You are an impartial judge auditing a RAG system's answer for faithfulness to its source
context ONLY. Do not judge whether the answer is factually correct in general — judge only
whether every claim in the System's Answer is supported by the Retrieved Context.

Rate the System's Answer on a scale of 1 to 5:
1: The answer makes claims with no support in the retrieved context (hallucinated).
3: The answer is partly grounded, but includes some unsupported claims.
5: Every claim in the answer is directly supported by the retrieved context.

Output JSON only: {{"score": int, "reasoning": "string"}}

Question: {question}
Retrieved Context: {context}
System Answer: {system_answer}
"""


async def _judge(prompt, chat_fn):
    try:
        response_text = await chat_fn(messages=[{"role": "user", "content": prompt}], temperature=0.0)
        data = json.loads(response_text)
        return Grade(**data)
    except Exception as e:
        return Grade(score=0, reasoning=f"Judge Error: {e}")


async def grade_answer(question: str, ground_truth: str, system_answer: str, chat_fn) -> Grade:
    """Calls chat_fn to grade a single QA pair against its ground truth."""
    prompt = CORRECTNESS_PROMPT.format(question=question, ground_truth=ground_truth, system_answer=system_answer)
    return await _judge(prompt, chat_fn)


async def grade_faithfulness(question: str, context: str, system_answer: str, chat_fn) -> Grade:
    """Calls chat_fn to grade whether system_answer is grounded in the retrieved context."""
    prompt = FAITHFULNESS_PROMPT.format(question=question, context=context, system_answer=system_answer)
    return await _judge(prompt, chat_fn)


async def ollama_chat(messages, temperature=0.0, model=OLLAMA_JUDGE_MODEL, endpoint=OLLAMA_ENDPOINT):
    """Talks to a local Ollama model via its OpenAI-compatible endpoint. Deliberately
    separate from services.api.app.clients.ray_llm.RayLLMClient, which hardcodes the
    system-under-test's endpoint/model from settings with no per-call override — the judge
    needs to be a genuinely different model so it isn't grading its own homework."""
    async with httpx.AsyncClient(timeout=120.0) as client:
        response = await client.post(endpoint, json={"model": model, "messages": messages, "temperature": temperature})
        response.raise_for_status()
        body = response.json()
        return body["choices"][0]["message"]["content"]
