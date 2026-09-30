# services/api/app/routes/chat.py
import uuid
import json
import logging
import asyncio
import time
from contextlib import aclosing
from typing import AsyncGenerator

import httpx
import anyio
from opentelemetry import context, trace
from opentelemetry.trace import Status, StatusCode
from libs.observability.tracing import domain_span
from services.api.app.evaluation.context import RunEvidenceCollector
from services.api.app.evaluation.instrumentation_adapter import EvidenceInstrumentation, bind_instrumentation
from services.api.app.evaluation.delivery_models import PayloadSlot
from services.api.app.evaluation.runtime import active_runtime
from services.api.app.evaluation.configuration import configuration_fingerprint
from services.api.app.config import settings

from fastapi import APIRouter, Depends, BackgroundTasks, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from services.api.app.auth.jwt import get_current_user
# Import classes for type hinting
from services.api.app.cache.semantic import SemanticCache, semantic_cache as global_cache
from services.api.app.memory.postgres import PostgresMemory, postgres_memory as global_memory
from services.api.app.clients.ray_llm import RayLLMClient, llm_client as global_llm
from services.api.app.agents.graph import agent_app
from services.api.app.agents.state import AgentState
from services.api.app.tools.registry import TOOL_REGISTRY, NO_ANSWER_PHRASES

router = APIRouter()
logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)


class RunStreamingResponse(StreamingResponse):
    """Close the run generator when ASGI disconnects during a body send."""

    def __init__(self, *args, on_close, evidence_collector=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.on_close = on_close
        self.evidence_collector = evidence_collector

    @property
    def evidence(self):
        return self.evidence_collector.event if self.evidence_collector is not None else None

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            try:
                with anyio.CancelScope(shield=True):
                    await self.body_iterator.aclose()
            finally:
                self.on_close()

def _is_successful_answer(text: str) -> bool:
    lower = text.lower()
    return not any(phrase in lower for phrase in NO_ANSWER_PHRASES)

# --- Dependency Providers (DI) ---
# These wrappers allow us to override dependencies easily in pytest
# e.g., app.dependency_overrides[get_llm_client] = MockLLMClient

def get_semantic_cache() -> SemanticCache:
    return global_cache

def get_memory() -> PostgresMemory:
    return global_memory

def get_llm_client() -> RayLLMClient:
    return global_llm

# --- Schemas ---
class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, description="The user's query")
    session_id: str = Field(default=None, description="UUID for the conversation thread")

# --- Routes ---

@router.post("/stream")
async def chat_stream(
    req: ChatRequest, 
    background_tasks: BackgroundTasks,
    user: dict = Depends(get_current_user),
    # Inject dependencies via FastAPI Depends
    cache: SemanticCache = Depends(get_semantic_cache),
    memory: PostgresMemory = Depends(get_memory),
    llm: RayLLMClient = Depends(get_llm_client),
    load_test_cache_bypass: str | None = Header(default=None, alias="X-Load-Test-Bypass-Cache"),
):
    """
    Main Chat Endpoint (Streaming).
    Orchestrates the RAG flow: Cache -> History -> Agent -> Stream.
    """
    # One run per accepted attempt, including cache hits and client retries.
    run_id = str(uuid.uuid4())
    session_id = req.session_id or str(uuid.uuid4())
    user_id = user["id"]
    bypass_cache = (
        settings.ENV in {"dev", "local"}
        and settings.LOCAL_LOAD_TEST_CACHE_BYPASS_ENABLED
        and load_test_cache_bypass == "1"
    )
    parent_context = context.get_current()
    started_at = time.time_ns()
    span = tracer.start_span(
        "agent.run", context=parent_context, start_time=started_at,
        attributes={"run_id": run_id, "cache.hit": False, "cache.bypassed": bypass_cache},
    )
    finished = False
    error_type = None
    trace_context = span.get_span_context()
    collector = RunEvidenceCollector(
        run_id=run_id, session_id=session_id,
        trace_id=f"{trace_context.trace_id:032x}" if trace_context.is_valid else None,
        input_text=req.message, user_id=user_id, tenant_id=user.get("tenant_id"),
        identity_hash_key=settings.EVAL_IDENTITY_HASH_KEY, configuration=configuration_fingerprint(),
    )
    # Evidence delivery is fail-open: None when disabled, and no failure here may affect the chat.
    evidence = active_runtime()
    capture = None
    if evidence is not None:
        try:
            capture = evidence.open_capture(run_id, user.get("tenant_id"))
            if capture is not None:
                capture.record(PayloadSlot(kind="input", ordinal=1), {"text": req.message}, capture.source(req.message))
        except Exception:
            if capture is not None:
                capture.close()
            capture = None
    instrumentation = EvidenceInstrumentation(collector, capture)

    def finish_run(outcome):
        nonlocal finished
        if not finished:
            finished = True
            span.set_attribute("run.status", outcome)
            try:
                event = collector.finish(outcome, error_type=error_type)
                if evidence is not None:
                    # Synchronous hand-off only: no IO or await on this terminal path.
                    try:
                        evidence.admit(capture, event)
                    except Exception:
                        logger.warning("Evidence admission failed")
            finally:
                try:
                    if capture is not None:
                        capture.close()
                finally:
                    span.end()

    def encode_event(**fields):
        if fields.get("type") == "answer":
            collector.mark_first_output()
            collector.record_output(fields.get("content", ""))
        return json.dumps({**fields, "run_id": run_id, "session_id": session_id}) + "\n"

    async def stream_events() -> AsyncGenerator[str, None]:
        final_answer = ""
        is_cacheable = True  # default; overridden by planner output

        with domain_span("cache.lookup") as lookup:
            if bypass_cache:
                cached_ans = None
                lookup.set_attribute("operation.status", "skipped")
                lookup.set_attribute("cache.skip_reason", "local_load_test")
            else:
                cached_ans = await cache.get_cached_response(req.message)
            lookup.set_attribute("cache.hit", bool(cached_ans))
        trace.get_current_span().set_attribute("cache.hit", bool(cached_ans))
        if cached_ans:
            if capture is not None:
                # Cached text has no retained source proof; equal fixture text
                # cannot establish the provenance of this cached computation.
                capture.omit(PayloadSlot(kind="output", ordinal=1), "unapproved_source")
            yield encode_event(type="answer", content=cached_ans)
            # Include cached persistence in the run lifecycle, just like normal answers.
            await persist(cached_ans)
            return

        with domain_span("memory.load") as load:
            history_objs = await memory.get_history(session_id, limit=6)
            load.set_attribute("memory.message_count", len(history_objs))
        history_dicts = [{"role": msg.role, "content": msg.content} for msg in history_objs]
        history_dicts.append({"role": "user", "content": req.message})
        initial_state = AgentState(
            messages=history_dicts, current_query=req.message, action="retrieve",
            tool_name="", tool_input="", documents=[], plan=[],
        )

        async with aclosing(agent_app.astream(
            initial_state,
            config={"configurable": {"llm": llm, "user_id": user_id, "evidence_collector": collector}},
        )) as graph_events:
            async for event in graph_events:
                node_name = list(event.keys())[0]
                node_data = event[node_name]

                if node_name == "planner":
                    action = node_data.get("action", "retrieve")
                    tool_name = node_data.get("tool_name", "")
                    if action == "tool_use" and tool_name:
                        is_cacheable = TOOL_REGISTRY.get(tool_name, {}).get("cacheable", True)
                    yield encode_event(type="routing", action=action, tool=tool_name or None)

                yield encode_event(type="status", node=node_name, info=f"Completed step: {node_name}")

                if node_name == "responder" and node_data.get("messages"):
                    final_answer = node_data["messages"][-1].get("content", "")
                    yield encode_event(type="answer", content=final_answer)

        if final_answer:
            await persist(final_answer)
            with domain_span("cache.write") as write:
                if bypass_cache:
                    write.set_attribute("operation.status", "skipped")
                    write.set_attribute("cache.skip_reason", "local_load_test")
                elif is_cacheable and _is_successful_answer(final_answer):
                    await cache.set_cached_response(req.message, final_answer)
                else:
                    write.set_attribute("operation.status", "skipped")
                    write.set_attribute("cache.skip_reason", "tool_not_cacheable" if not is_cacheable else "abstention")

    async def persist(answer):
        with domain_span("memory.persist") as persist_span:
            await memory.add_message(session_id, "user", req.message, user_id)
            await memory.add_message(session_id, "assistant", answer, user_id)
            persist_span.set_attribute("memory.message_count", 2)

    async def event_generator() -> AsyncGenerator[str, None]:
        nonlocal error_type
        events = stream_events()
        outcome = "cancelled"
        try:
            while True:
                # Attach only while advancing the generator. Holding a context token
                # across yield can leak context or fail when ASGI closes another task.
                with bind_instrumentation(collector, capture, adapter=instrumentation), trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    try:
                        event = await anext(events)
                    except StopAsyncIteration:
                        outcome = "success"
                        break
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except Exception as exc:
            error_type = type(exc).__name__
            outcome = "timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) else "error"
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(Status(StatusCode.ERROR))
            logger.error("Chat run %s failed (%s)", run_id, type(exc).__name__)
            yield encode_event(type="error", content="An internal error occurred.")
        finally:
            try:
                with bind_instrumentation(collector, capture, adapter=instrumentation), trace.use_span(span, end_on_exit=False, record_exception=False, set_status_on_exception=False):
                    await events.aclose()
            finally:
                finish_run(outcome)

    return RunStreamingResponse(
        event_generator(), media_type="application/x-ndjson", headers={"X-Run-ID": run_id},
        on_close=lambda: finish_run("cancelled"),
        evidence_collector=collector,
    )
