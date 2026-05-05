# services/api/app/agents/nodes/tool.py
import asyncio
import inspect
import logging
from services.api.app.agents.state import AgentState
from services.api.app.tools.registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)

async def tool_node(state: AgentState) -> dict:
    tool_name = state.get("tool_name", "")
    tool_input = state.get("tool_input", "")

    tool = TOOL_REGISTRY.get(tool_name)
    if not tool:
        result = f"Unknown tool: {tool_name!r}. Available: {list(TOOL_REGISTRY)}"
        logger.warning(result)
    else:
        logger.info(f"Executing {tool_name}: {tool_input}")
        handler = tool["handler"]
        result = await handler(tool_input) if inspect.iscoroutinefunction(handler) else handler(tool_input)

    return {
        "documents": [result],
        "messages": [{"role": "user", "content": f"Tool Output: {result}"}],
    }
