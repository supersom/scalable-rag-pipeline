# services/api/app/agents/nodes/responder.py
import logging
from services.api.app.agents.state import AgentState
from services.api.app.clients.ray_llm import llm_client
from libs.observability.tracing import domain_span

logger = logging.getLogger(__name__)

async def generate_node(state: AgentState) -> dict:
    with domain_span("responder", attributes={"response.type": state.get("action", "retrieve")}):
        return await _generate(state)


async def _generate(state: AgentState) -> dict:
    """
    Synthesizes the final answer using retrieved documents or tool output.
    """
    query = state["current_query"]
    documents = state.get("documents", [])
    action = state.get("action", "retrieve")

    context_str = "\n\n".join(documents)

    if action == "direct_answer":
        prompt = f"""You are a friendly assistant. Respond naturally and briefly to the user's message.

User: {query}"""
    elif action == "tool_use":
        prompt = f"""You are a helpful assistant. A tool was called to answer the user's query and returned the result below.

Tool result: {context_str}

User query: {query}

Present the tool result as a direct, concise answer. Do not say you lack information."""
    else:
        prompt = f"""You are a helpful Enterprise Assistant. Use the context below to answer the user's question.

Context:
{context_str}

Question:
{query}

Instructions:
1. Cite sources using [Source: Filename].
2. If the answer is not in the context, say "I don't have that information in my documents."
3. Be concise and professional."""
    
    logger.info("Responder Node: generating answer with %d docs", len(documents))
    answer = await llm_client.chat_completion_result(
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3
    )
    logger.info("Responder Node: answer generated")

    return {
        "messages": [{"role": "assistant", "content": answer.text}]
    }
