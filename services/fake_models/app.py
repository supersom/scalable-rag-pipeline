import hashlib
import json
import math
import os
from typing import Any

from fastapi import FastAPI, Header, HTTPException


app = FastAPI(title="Deterministic Local Model Service", version="1.0.0")

DEFAULT_LLM_MODEL = "fake-local-llm"
DEFAULT_EMBED_MODEL = "fake-local-embed"
DEFAULT_RESPONSE = "This is a deterministic local response."
GREETING_WORDS = {"hello", "hi", "hey", "thanks", "thank you", "bye"}


def _token_count(text: str) -> int:
    return len(text.split())


def _last_message(messages: list[dict[str, Any]]) -> str:
    if not messages:
        return ""
    return str(messages[-1].get("content", ""))


def _is_planner_request(messages: list[dict[str, Any]]) -> bool:
    return any(
        message.get("role") == "system"
        and "Classify the user query" in str(message.get("content", ""))
        for message in messages
    )


def _planner_response(query: str) -> str:
    normalized = query.strip().lower()
    first_word = normalized.split(maxsplit=1)[0] if normalized else ""
    direct = normalized in GREETING_WORDS or first_word in GREETING_WORDS
    return json.dumps(
        {
            "action": "direct_answer" if direct else "retrieve",
            "tool_name": None,
            "reasoning": (
                "local deterministic greeting"
                if direct
                else "local deterministic factual question"
            ),
            "refined_query": query,
        },
        separators=(",", ":"),
    )


def _embedding(text: str, dimensions: int) -> list[float]:
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    values = [(digest[index % len(digest)] - 127.5) / 127.5 for index in range(dimensions)]
    magnitude = math.sqrt(sum(value * value for value in values)) or 1.0
    return [value / magnitude for value in values]


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/v1/chat/completions")
async def chat_completions(
    body: dict[str, Any],
    x_fake_scenario: str | None = Header(default=None),
) -> dict[str, Any]:
    if x_fake_scenario == "error":
        raise HTTPException(status_code=503, detail="fake model forced error")
    if x_fake_scenario == "malformed":
        return {"unexpected": True}

    messages = body.get("messages") or []
    query = _last_message(messages)
    content = _planner_response(query) if _is_planner_request(messages) else DEFAULT_RESPONSE
    prompt = " ".join(str(message.get("content", "")) for message in messages)
    prompt_tokens = _token_count(prompt)
    completion_tokens = _token_count(content)
    request_hash = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "id": f"fake-chat-{request_hash}",
        "object": "chat.completion",
        "model": body.get("model") or DEFAULT_LLM_MODEL,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@app.post("/api/embeddings")
async def embeddings(
    body: dict[str, Any],
    x_fake_scenario: str | None = Header(default=None),
) -> dict[str, Any]:
    if x_fake_scenario == "error":
        raise HTTPException(status_code=503, detail="fake embedding model forced error")

    text = body.get("prompt") or body.get("text") or ""
    if isinstance(text, list):
        text = text[0] if text else ""
    dimensions = int(os.getenv("FAKE_EMBED_DIM", "1024"))
    return {
        "model": body.get("model") or DEFAULT_EMBED_MODEL,
        "embedding": _embedding(str(text), dimensions),
    }
