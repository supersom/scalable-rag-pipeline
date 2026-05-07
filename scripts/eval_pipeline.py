#!/usr/bin/env python3
"""
Pipeline evaluation script.

Sends a battery of test queries, captures timing and routing from uvicorn logs,
and prints a summary table of latency, routing accuracy, and answer quality.

Usage:
    python scripts/eval_pipeline.py
    python scripts/eval_pipeline.py --url http://localhost:8000 --session eval-1
"""
import argparse
import asyncio
import json
import os
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx
from jose import jwt
from dotenv import load_dotenv

load_dotenv()

API_BASE = os.getenv("API_BASE", "http://localhost:8000")
JWT_SECRET = os.getenv("JWT_SECRET_KEY", "change_this_to_a_secure_random_string_for_jwt")
JWT_ALGORITHM = "HS256"

# ---------------------------------------------------------------------------
# Test cases: (query, expected_action, description)
# expected_action: "direct_answer" | "tool_use" | "retrieve" | None (don't check)
# ---------------------------------------------------------------------------
TEST_CASES = [
    # Direct answer
    ("hello there",                                         "direct_answer", "greeting"),
    ("thank you",                                           "direct_answer", "small talk"),
    ("good morning",                                        "direct_answer", "small talk"),
    # Calculator
    ("3*7",                                                 "tool_use",      "simple mult"),
    ("55-31+21",                                            "tool_use",      "multi-op"),
    ("32-(14/2)",                                           "tool_use",      "parens"),
    ("sqrt(256)",                                           "tool_use",      "sqrt"),
    # Web search
    ("what is the weather today",                           "tool_use",      "weather"),
    ("current bitcoin price",                               "tool_use",      "live price"),
    ("latest news headlines",                               "tool_use",      "breaking news"),
    # Retrieval
    ("How does selective state space modeling work?",       "retrieve",      "mamba SSM"),
    ("What are the requirements for KEY_VALUE_BASIC_INFORMATION in Windows?",
                                                            "retrieve",      "windows kernel"),
    ("How does Oracle optimize history table storage?",     "retrieve",      "oracle"),
    ("What is mutual information estimation?",              "retrieve",      "mutual info"),
]

QDRANT_BASE = os.getenv("QDRANT_HOST", "localhost")
QDRANT_PORT = os.getenv("QDRANT_PORT", "6333")
QDRANT_URL  = f"http://{QDRANT_BASE}:{QDRANT_PORT}"

ANSI_GREEN  = "\033[92m"
ANSI_RED    = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_RESET  = "\033[0m"
ANSI_BOLD   = "\033[1m"


async def purge_eval_cache(client: httpx.AsyncClient, queries: list[str]) -> int:
    """Delete semantic_cache entries whose payload.query matches any eval query."""
    deleted = 0
    for query in queries:
        resp = await client.post(
            f"{QDRANT_URL}/collections/semantic_cache/points/delete",
            json={"filter": {"must": [{"key": "query", "match": {"value": query}}]}},
        )
        if resp.status_code == 200:
            deleted += 1
    return deleted


def make_token(user_id: str = "eval-user") -> str:
    return jwt.encode(
        {"sub": user_id, "role": "admin", "exp": int(time.time()) + 3600},
        JWT_SECRET,
        algorithm=JWT_ALGORITHM,
    )


@dataclass
class Result:
    query: str
    description: str
    expected_action: Optional[str]
    actual_action: Optional[str]     # parsed from SSE routing header or None
    answer: str
    elapsed_s: float
    cache_hit: bool = False
    error: Optional[str] = None


async def run_query(client: httpx.AsyncClient, token: str, session_id: str,
                    query: str) -> tuple[str, float, bool, Optional[str], Optional[str]]:
    """Returns (answer_text, elapsed_seconds, cache_hit, action, tool)."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"session_id": session_id, "user_id": "eval-user", "message": query}

    t0 = time.perf_counter()
    answer_chunks = []
    cache_hit = False
    action: Optional[str] = None
    tool: Optional[str] = None

    async with client.stream("POST", f"{API_BASE}/api/v1/chat/stream",
                             headers=headers, json=payload, timeout=120.0) as resp:
        resp.raise_for_status()
        async for line in resp.aiter_lines():
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "answer":
                answer_chunks.append(event.get("content", ""))
            elif etype == "routing":
                action = event.get("action")
                tool = event.get("tool") or None
            elif etype == "answer" and event.get("cache_hit"):
                cache_hit = True

    # Cache hits don't emit routing events — infer from presence
    if action is None and answer_chunks:
        cache_hit = True

    elapsed = time.perf_counter() - t0
    return "".join(answer_chunks), elapsed, cache_hit, action, tool


def color_action(actual: Optional[str], expected: Optional[str]) -> str:
    if actual is None:
        return f"{ANSI_YELLOW}unknown{ANSI_RESET}"
    if expected is None:
        return actual
    if actual == expected:
        return f"{ANSI_GREEN}{actual}{ANSI_RESET}"
    return f"{ANSI_RED}{actual}{ANSI_RESET}"


def truncate(s: str, n: int = 80) -> str:
    return s[:n] + "…" if len(s) > n else s


async def main(args):
    token = make_token()
    session_id = args.session

    print(f"\n{ANSI_BOLD}Pipeline Evaluation{ANSI_RESET}  →  {API_BASE}")
    print(f"Session: {session_id}   Queries: {len(TEST_CASES)}\n")

    results: list[Result] = []

    async with httpx.AsyncClient() as client:
        for i, (query, expected, desc) in enumerate(TEST_CASES, 1):
            print(f"[{i:2d}/{len(TEST_CASES)}] {desc:<20} ", end="", flush=True)
            try:
                answer, elapsed, cache_hit, actual_action, tool = await run_query(
                    client, token, f"{session_id}-{i}", query
                )
                r = Result(
                    query=query,
                    description=desc,
                    expected_action=expected,
                    actual_action=actual_action,
                    answer=answer,
                    elapsed_s=elapsed,
                    cache_hit=cache_hit,
                )
                if cache_hit:
                    tag = f"{ANSI_YELLOW}CACHE{ANSI_RESET}"
                else:
                    routing_ok = (expected is None or actual_action == expected)
                    routing_str = color_action(actual_action, expected)
                    tag = f"{routing_str:<14}  {elapsed:.1f}s"
                print(tag)
            except Exception as e:
                r = Result(
                    query=query,
                    description=desc,
                    expected_action=expected,
                    actual_action=None,
                    answer="",
                    elapsed_s=0.0,
                    error=str(e),
                )
                print(f"{ANSI_RED}ERROR: {e}{ANSI_RESET}")

            results.append(r)
            # Small gap so LLM isn't overwhelmed
            await asyncio.sleep(1.0)

    # ------------------------------------------------------------------
    # Summary table
    # ------------------------------------------------------------------
    print(f"\n{'─'*120}")
    print(f"{'#':>3}  {'Desc':<20}  {'Query':<38}  {'Expected':<14}  {'Actual':<14}  {'Time':>7}  Answer")
    print(f"{'─'*120}")

    total_time = 0.0
    errors = 0
    routing_correct = 0
    routing_total = 0

    for i, r in enumerate(results, 1):
        if r.error:
            errors += 1
            print(f"{i:>3}  {r.description:<20}  {truncate(r.query,38):<38}  "
                  f"{r.expected_action or '?':<14}  {'ERR':<14}  {'ERR':>7}  "
                  f"{ANSI_RED}{r.error}{ANSI_RESET}")
            continue

        total_time += r.elapsed_s
        time_str = f"{ANSI_YELLOW}  CACHE{ANSI_RESET}" if r.cache_hit else f"{r.elapsed_s:>7.1f}s"
        ans_preview = truncate(r.answer.replace('\n', ' '), 45)

        if r.cache_hit:
            actual_str = f"{ANSI_YELLOW}cached{ANSI_RESET}"
        else:
            actual_str = color_action(r.actual_action, r.expected_action)
            if r.expected_action is not None and r.actual_action is not None:
                routing_total += 1
                if r.actual_action == r.expected_action:
                    routing_correct += 1

        print(f"{i:>3}  {r.description:<20}  {truncate(r.query,38):<38}  "
              f"{r.expected_action or '?':<14}  {actual_str:<14}  {time_str}  {ans_preview}")

    ok = len(results) - errors
    routing_pct = f"{routing_correct}/{routing_total} ({100*routing_correct//max(routing_total,1)}%)" if routing_total else "n/a"
    print(f"{'─'*120}")
    print(f"\nCompleted: {ok}/{len(results)}  |  Errors: {errors}  |  "
          f"Routing accuracy: {routing_pct}  |  "
          f"Total time: {total_time:.1f}s  |  Avg: {total_time/max(ok,1):.1f}s\n")

    # Clean up: remove eval queries from semantic cache so they don't skew future runs
    print("Purging eval queries from semantic cache...", end=" ", flush=True)
    async with httpx.AsyncClient() as client:
        all_queries = [q for q, _, _ in TEST_CASES]
        deleted = await purge_eval_cache(client, all_queries)
    print(f"done ({deleted}/{len(all_queries)} entries removed)\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=API_BASE)
    parser.add_argument("--session", default="eval")
    args = parser.parse_args()
    API_BASE = args.url
    asyncio.run(main(args))
