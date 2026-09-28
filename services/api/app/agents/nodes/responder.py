# services/api/app/agents/nodes/responder.py
import logging
from services.api.app.agents.state import AgentState
from services.api.app.clients.ray_llm import llm_client
from libs.observability.tracing import domain_span
from langchain_core.runnables import RunnableConfig
from services.api.app.evaluation.context import bind_collector, collector_from_config
from services.api.app.evaluation.capture import current_capture, bind_capture
from services.api.app.evaluation.delivery_models import PayloadSlot

DIRECT_TEMPLATE = """You are a friendly assistant. Respond naturally and briefly to the user's message.

User: {query}"""
TOOL_TEMPLATE = """You are a helpful assistant. A tool was called to answer the user's query and returned the result below.

Tool result: {context_str}

User query: {query}

Present the tool result as a direct, concise answer. Do not say you lack information."""
RETRIEVAL_TEMPLATE = """You are a helpful Enterprise Assistant. Use the context below to answer the user's question.

Context:
{context_str}

Question:
{query}

Instructions:
1. Cite sources using [Source: Filename].
2. If the answer is not in the context, say "I don't have that information in my documents."
3. Be concise and professional."""

logger = logging.getLogger(__name__)

async def generate_node(state: AgentState, config: RunnableConfig = None) -> dict:
    with bind_collector(collector_from_config(config), purpose="responder"), domain_span("responder", attributes={"response.type": state.get("action", "retrieve")}):
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
        template, template_id = DIRECT_TEMPLATE, 'responder-direct-v1'
    elif action == "tool_use":
        template, template_id = TOOL_TEMPLATE, 'responder-tool-v1'
    else:
        template, template_id = RETRIEVAL_TEMPLATE, 'responder-retrieve-v1'
    prompt = template.format(query=query, context_str=context_str)
    capture = current_capture.get()
    provenance = None
    if capture is not None:
        provenance = capture.combine(capture.source(query),
            capture.proof('template', template, template_id=template_id),
            *(capture.source(document) for document in documents if action != 'direct_answer'))
    
    logger.info("Responder Node: generating answer with %d docs", len(documents))
    with bind_capture(capture, provenance):
        answer = await llm_client.chat_completion_result(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3
        )
    if capture is not None:
        capture.record(PayloadSlot(kind='output', ordinal=1), {'text': answer.text}, capture.source(answer.text))
    logger.info("Responder Node: answer generated")

    return {
        "messages": [{"role": "assistant", "content": answer.text}]
    }
