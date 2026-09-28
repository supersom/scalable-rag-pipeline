# services/api/app/agents/nodes/tool.py
import asyncio
import inspect
import logging
import time
import hashlib
import json
from services.api.app.agents.state import AgentState
from services.api.app.tools.registry import TOOL_REGISTRY
from libs.observability.tracing import domain_span, failure_status, mark_failure
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)


def _content_hash(value):
    raw = value if isinstance(value, str) else json.dumps(value, sort_keys=True, separators=(',', ':'))
    return 'sha256:' + hashlib.sha256(raw.encode()).hexdigest()

async def tool_node(state: AgentState, config: RunnableConfig = None) -> dict:
    name = state.get("tool_name", "")
    started = time.perf_counter()
    with domain_span("tool.execute", attributes={"tool.name": name if name in TOOL_REGISTRY else "unknown"}, schema_version=1) as span:
        tool_input = state.get('tool_input', '')
        provenance = span.provenance.source(tool_input, tool_name=name)
        span.record('request', facts={
            'tool_name': name if name in TOOL_REGISTRY else 'unknown',
            'registered': name in TOOL_REGISTRY,
            'arguments_hash': _content_hash(tool_input),
        }, content={'arguments': span.content({'expression': tool_input}, provenance=provenance)})
        provenance = span.provenance.effective(span.operation_id, provenance)
        if name not in TOOL_REGISTRY:
            mark_failure(span, ValueError(), status="invalid")
        result = None
        try:
            with span.provenance.bind(provenance):
                result = await _execute(state)
            return result
        except BaseException as exc:
            mark_failure(span, exc)
            raise
        finally:
            span.record('result', facts={key: value for key, value in {
                'result_hash': _content_hash(result['documents']) if result is not None else None,
                'status': span.attributes['operation.status'],
                'error_type': span.attributes.get('error.type'),
                'retry_count': 0, 'latency_ms': (time.perf_counter() - started) * 1000,
            }.items() if value is not None}, content={
                'result': span.content(result, provenance=provenance)
            } if result is not None else None)


async def _execute(state: AgentState) -> dict:
    tool_name = state.get("tool_name", "")
    tool_input = state.get("tool_input", "")

    tool = TOOL_REGISTRY.get(tool_name)
    if not tool:
        result = f"Unknown tool: {tool_name!r}. Available: {list(TOOL_REGISTRY)}"
        logger.warning(result)
    else:
        logger.info("Executing %s", tool_name)
        handler = tool["handler"]
        result = await handler(tool_input) if inspect.iscoroutinefunction(handler) else handler(tool_input)

    return {
        "documents": [result],
        "messages": [{"role": "user", "content": f"Tool Output: {result}"}],
    }
