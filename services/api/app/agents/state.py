# services/api/app/agents/state.py
from typing import TypedDict, Annotated, List, Union
import operator

class AgentState(TypedDict):
    """
    The state object passed between nodes in the LangGraph.
    Tracks the conversation history and current step data.
    """
    # Using 'operator.add' means new messages are appended, not overwritten
    messages: Annotated[List[dict], operator.add] 
    
    # Context retrieved from RAG (Vector + Graph)
    documents: List[str] 
    
    # The current question being processed
    current_query: str

    # Planner's routing decision: "retrieve" | "direct_answer" | "tool_use"
    action: str

    # Which tool to invoke when action == "tool_use"
    tool_name: str

    # Input to pass to the tool
    tool_input: str

    # Internal scratchpad for the planner
    plan: List[str]