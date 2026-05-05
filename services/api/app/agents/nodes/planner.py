# services/api/app/agents/nodes/planner.py
import json
import logging
from services.api.app.agents.state import AgentState
from services.api.app.clients.ray_llm import llm_client
from services.api.app.tools.registry import TOOL_REGISTRY

logger = logging.getLogger(__name__)

def _build_system_prompt() -> str:
    tool_lines = "\n".join(
        f'   - "{name}": {info["description"]}'
        for name, info in TOOL_REGISTRY.items()
    )
    return f"""You are a RAG Planning Agent.
Analyze the User Query and decide the next step.

Rules:
1. Output "direct_answer" if the user is greeting, making small talk, or asking something answerable without any external data (e.g. "Hello", "Thanks").
2. Output "tool_use" ONLY when the query strictly matches one of the tools below — read each description carefully to avoid false matches:
{tool_lines}
3. Output "retrieve" for everything else — factual questions, how-to questions, explanations, conceptual questions, historical information. When in doubt, choose "retrieve".

Output JSON format ONLY — no extra text:
{{
    "action": "retrieve" | "direct_answer" | "tool_use",
    "tool_name": {json.dumps(list(TOOL_REGISTRY.keys()) + [None])},
    "refined_query": "The standalone search query or expression to evaluate",
    "reasoning": "Why you chose this action and tool"
}}"""

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
        
        # Parse JSON
        plan = json.loads(response_text)
        
        action = plan.get("action", "retrieve")
        tool_name = plan.get("tool_name") or ""
        if isinstance(tool_name, list):
            tool_name = tool_name[0] if tool_name else ""
        refined_query = plan.get("refined_query") or user_query
        logger.info(f"Plan derived: {action}" + (f" / tool: {tool_name}" if tool_name else ""))

        return {
            "action": action,
            "tool_name": tool_name,
            "current_query": refined_query,
            "tool_input": refined_query if action == "tool_use" else "",
            "plan": [plan["reasoning"]],
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