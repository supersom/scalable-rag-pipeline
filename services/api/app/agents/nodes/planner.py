# services/api/app/agents/nodes/planner.py
import json
import re
import logging
from services.api.app.agents.state import AgentState
from services.api.app.clients.ray_llm import llm_client
from services.api.app.tools.registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)

def _build_system_prompt() -> str:
    tool_list = "\n".join(
        f'  "{name}": {info["description"]}'
        for name, info in TOOL_REGISTRY.items()
    )
    return (
        'Classify the user query and respond with ONLY a JSON object. No prose, no markdown.\n\n'
        'Schema: {"action": "...", "tool_name": "...", "reasoning": "...", "refined_query": "..."}\n\n'
        'action must be exactly one of:\n'
        '  "direct_answer" — greeting or small talk only ("hi", "hello", "thanks", "bye")\n'
        '  "tool_use"      — query matches one of the tools below; put the tool key in tool_name\n'
        '  "retrieve"      — all other questions (factual, how-to, explanatory)\n\n'
        f'Tools:\n{tool_list}\n\n'
        'Rules:\n'
        '- tool_name must be null when action != "tool_use"\n'
        '- refined_query is the standalone search string or expression\n'
        '- reasoning is a brief explanation of why you chose this action\n\n'
        'Examples (follow exactly):\n'
        'User: "hi" → {"action": "direct_answer", "tool_name": null, "reasoning": "greeting", "refined_query": "hi"}\n'
        'User: "thanks" → {"action": "direct_answer", "tool_name": null, "reasoning": "small talk", "refined_query": "thanks"}\n'
        'User: "2+6" → {"action": "tool_use", "tool_name": "calculator", "reasoning": "arithmetic expression", "refined_query": "2+6"}\n'
        'User: "sqrt(144)" → {"action": "tool_use", "tool_name": "calculator", "reasoning": "math function", "refined_query": "sqrt(144)"}\n'
        'User: "what is todays weather" → {"action": "tool_use", "tool_name": "web_search", "reasoning": "real-time info needed", "refined_query": "todays weather"}\n'
        'User: "what is machine learning" → {"action": "retrieve", "tool_name": null, "reasoning": "factual question", "refined_query": "what is machine learning"}\n'
        'User: "how does RAG work" → {"action": "retrieve", "tool_name": null, "reasoning": "explanatory question", "refined_query": "how does RAG work"}\n'
    )

def _extract_json(text: str) -> dict:
    """Strip markdown fences and decode the first JSON object, ignoring trailing content."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    idx = text.find("{")
    if idx == -1:
        raise ValueError(f"No JSON object found in: {text!r}")
    obj, _ = json.JSONDecoder().raw_decode(text, idx)
    return obj

SYSTEM_PROMPT = _build_system_prompt()

async def planner_node(state: AgentState) -> dict:
    """
    Decides the path through the LangGraph.
    """
    logger.info("Planner Node: Analyzing query...")
    
    # Extract latest user message
    # state['messages'] is a list of dicts or objects
    last_message = state["messages"][-1]
    user_query = last_message.content if hasattr(last_message, 'content') else last_message['content']

    # Call LLM to plan
    try:
        response_text = await llm_client.chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_query}
            ],
            temperature=0.0 # Deterministic planning
        )
        
        # _extract_json strips markdown fences and returns the first JSON object
        plan = _extract_json(response_text)

        action = plan.get("action", "retrieve")
        tool_name = plan.get("tool_name") or ""
        if isinstance(tool_name, list):
            tool_name = tool_name[0] if tool_name else ""
        # Safeguard: model sometimes outputs a valid tool_name but wrong action
        if tool_name and tool_name in TOOL_REGISTRY and action != "tool_use":
            action = "tool_use"
        refined_query = plan.get("refined_query") or plan.get("query") or user_query
        logger.info(f"Plan derived: {action}" + (f" / tool: {tool_name}" if tool_name else ""))

        return {
            "action": action,
            "tool_name": tool_name,
            "current_query": refined_query,
            "tool_input": refined_query if action == "tool_use" else "",
            "plan": [f"action={action}"],
        }

    except Exception as e:
        logger.error(f"Planning failed: {e}")
        return {
            "action": "retrieve",
            "tool_name": "",
            "current_query": user_query,
            "tool_input": "",
            "plan": ["Error in planning, defaulting to retrieval."],
        }