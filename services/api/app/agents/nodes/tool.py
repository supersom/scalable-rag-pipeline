# services/api/app/agents/nodes/tool.py
import asyncio
import inspect
import logging
import time
from datetime import datetime, timezone
from services.api.app.agents.state import AgentState
from services.api.app.tools.registry import TOOL_REGISTRY
from libs.observability.tracing import domain_span, failure_status, mark_failure
from langchain_core.runnables import RunnableConfig
from services.api.app.evaluation.context import bind_collector, collector_from_config, content_hash
from services.api.app.evaluation.capture import current_capture, bind_capture
from services.api.app.evaluation.delivery_models import PayloadSlot

logger = logging.getLogger(__name__)

async def tool_node(state: AgentState, config: RunnableConfig = None) -> dict:
    name = state.get("tool_name", "")
    collector = collector_from_config(config)
    capture = current_capture.get()
    provenance = None
    if capture is not None:
        ordinal = capture.next_ordinal('tool')
        provenance = capture.source(state.get('tool_input', '')).model_copy(update={'tool_name': name})
        arguments = PayloadSlot(kind='tool_arguments', ordinal=ordinal)
        if name == 'calculator':
            capture.record(arguments, {'expression': state.get('tool_input', '')}, provenance)
        else:
            capture.omit(arguments, 'unapproved_tool')
            provenance = capture.unknown()
    started_at, started = datetime.now(timezone.utc), time.perf_counter()
    with bind_capture(capture, provenance), bind_collector(collector, purpose="graph_search" if name == "graph_search" else "tool"), domain_span("tool.execute", attributes={"tool.name": name if name in TOOL_REGISTRY else "unknown"}) as span:
        if name not in TOOL_REGISTRY:
            mark_failure(span, ValueError(), status="invalid")
        result = None
        try:
            result = await _execute(state)
            if capture is not None:
                slot = PayloadSlot(kind='tool_result', ordinal=ordinal)
                if name == 'calculator' and not capture.has_payload(arguments):
                    # A result derived from denied arguments is never captured.
                    capture.omit(slot, 'unapproved_source')
                    provenance = capture.unknown()
                elif name == 'calculator':
                    capture.record(slot, {'value': result['documents'][0]}, provenance)
                    # An unsupported/error calculator result cannot authorize a prompt.
                    if not capture.has_payload(slot):
                        provenance = capture.unknown()
                else:
                    capture.omit(slot, 'unapproved_tool')
                    provenance = capture.unknown()
                for document in result['documents']:
                    capture.remember(document, provenance)
                for message in result['messages']:
                    capture.remember(message['content'], provenance)
            return result
        except BaseException as exc:
            if capture is not None:
                capture.omit(PayloadSlot(kind='tool_result', ordinal=ordinal), 'not_observed')
            mark_failure(span, exc)
            raise
        finally:
            if collector is not None:
                collector.record_tool_step(tool_name=name if name in TOOL_REGISTRY else "unknown", registered=name in TOOL_REGISTRY,
                    arguments_hash=content_hash(state.get("tool_input", "")),
                    result_hash=content_hash(result["documents"]) if result is not None else None,
                    started_at=started_at, latency_ms=(time.perf_counter() - started) * 1000,
                    status=span.attributes["operation.status"], error_type=span.attributes.get("error.type"))


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
