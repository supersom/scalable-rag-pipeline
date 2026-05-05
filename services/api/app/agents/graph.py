# services/api/app/agents/graph.py
from langgraph.graph import StateGraph, END
from services.api.app.agents.state import AgentState
from services.api.app.agents.nodes.retriever import retrieve_node
from services.api.app.agents.nodes.responder import generate_node
from services.api.app.agents.nodes.planner import planner_node
from services.api.app.agents.nodes.tool import tool_node

def route_after_planner(state: AgentState) -> str:
    action = state.get("action", "retrieve")
    if action == "direct_answer":
        return "responder"
    if action == "tool_use":
        return "tool"
    return "retriever"

workflow = StateGraph(AgentState)

workflow.add_node("planner", planner_node)
workflow.add_node("retriever", retrieve_node)
workflow.add_node("tool", tool_node)
workflow.add_node("responder", generate_node)

workflow.set_entry_point("planner")

workflow.add_conditional_edges(
    "planner",
    route_after_planner,
    {"retriever": "retriever", "tool": "tool", "responder": "responder"},
)
workflow.add_edge("retriever", "responder")
workflow.add_edge("tool", "responder")
workflow.add_edge("responder", END)

agent_app = workflow.compile()