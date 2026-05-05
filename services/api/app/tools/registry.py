# services/api/app/tools/registry.py
from services.api.app.tools.calculator import calculate
from services.api.app.tools.web_search import web_search_tool
from services.api.app.tools.sandbox import run_python_code
from services.api.app.tools.graph_search import search_graph_tool

NO_ANSWER_PHRASES = [
    "i don't have", "i do not have",
    "i couldn't find", "i could not find",
    "no information", "not found",
    "i'm not sure", "i am not sure",
    "outside my knowledge", "i don't know", "i do not know",
    "no relevant", "unable to find", "cannot find",
]

# Each entry: description (shown to the planner LLM) + handler (called by tool node)
TOOL_REGISTRY: dict[str, dict] = {
    "calculator": {
        "description": "Evaluate a numeric math expression the user explicitly wants computed (e.g. '2+2', 'sqrt(144)', '15% of 200'). Only use when the query IS the expression itself, not a conceptual math question.",
        "handler": calculate,
        "cacheable": True,
    },
    "web_search": {
        "description": "Search the live web for information that is inherently real-time and cannot exist in any static document: today's weather, current stock prices, live scores, breaking news. Do NOT use for general knowledge or questions answerable from documents.",
        "handler": web_search_tool,
        "cacheable": False,
    },
    "sandbox": {
        "description": "Execute Python code that the user has explicitly written or asked to run. Only use when the user provides actual code or asks to run a specific script. Do NOT use for how-to or conceptual programming questions.",
        "handler": run_python_code,
        "cacheable": True,
    },
    "graph_search": {
        "description": "Query the internal knowledge graph for named entities and their relationships (e.g. 'what is X connected to', 'find entities related to Y').",
        "handler": search_graph_tool,
        "cacheable": True,
    },
}
