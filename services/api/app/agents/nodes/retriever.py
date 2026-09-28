"""Ranked retrieval records are normalized before prompt formatting."""
import asyncio
import hashlib
from dataclasses import dataclass, replace
from typing import Literal

from libs.observability.tracing import domain_span, failure_status, mark_failure
from services.api.app.agents.state import AgentState
from services.api.app.clients.qdrant import qdrant_client
from services.api.app.clients.neo4j import neo4j_client
from services.api.app.clients.ray_embed import embed_client
from services.api.app.config import settings
from langchain_core.runnables import RunnableConfig
from services.api.app.evaluation.context import bind_collector, collector_from_config, current_collector, content_hash
from services.api.app.evaluation.models import RetrievalEvidence, RetrievedEvidence
from services.api.app.evaluation.capture import current_capture
from services.api.app.evaluation.delivery_models import PayloadSlot

CITATION_TEMPLATE = '{text} [Source: {filename}]'


@dataclass(frozen=True)
class RetrievedItem:
    retrieval_source: Literal["qdrant", "neo4j"]
    document_id: str | None
    document_version: str | None
    chunk_id: str | None
    content: str
    content_hash: str
    score: float | None
    rank: int
    metadata: dict


@dataclass(frozen=True)
class RetrievalResult:
    items: list[RetrievedItem]
    vector_status: str = "success"
    graph_status: str = "success"
    graph_error_type: str | None = None


def _item(source, payload, rank, score=None, point_id=None):
    metadata = {**payload, **(payload.get("metadata") or {})}
    content = payload.get("text", "")
    document_id = metadata.get("document_id")
    if document_id is None and metadata.get("source_path"):
        document_id = "sha256:" + hashlib.sha256(metadata["source_path"].encode()).hexdigest()
    chunk_id = metadata.get("chunk_id") or payload.get("relationship_id") or point_id
    return RetrievedItem(
        source, str(document_id) if document_id is not None else None,
        metadata.get("document_version") or metadata.get("content_hash"),
        str(chunk_id) if chunk_id is not None else None,
        content, hashlib.sha256(content.encode()).hexdigest(), score, rank, metadata,
    )


def _deduplicate(items):
    ordered = {}
    for item in items:
        key = (item.retrieval_source, item.document_id, item.chunk_id, item.content_hash)
        previous = ordered.get(key)
        if previous is None:
            ordered[key] = item
        elif item.score is not None and (previous.score is None or item.score > previous.score):
            ordered[key] = replace(previous, score=item.score)
    return list(ordered.values())


async def retrieve_context(query: str) -> RetrievalResult:
    with domain_span("retrieval") as span:
        try:
            query_vector = await embed_client.embed_query(query)
        except BaseException as exc:
            _record_evidence(query, RetrievalResult([], vector_status=failure_status(exc), graph_status="skipped"))
            raise
        async def vector_search():
            results = await qdrant_client.search(vector=query_vector, limit=5)
            return [_item("qdrant", r.payload or {}, rank, r.score, r.id)
                    for rank, r in enumerate(results, 1)]

        async def graph_search():
            try:
                rows = await neo4j_client.query("""
                    CALL db.index.fulltext.queryNodes("entity_index", $query) YIELD node, score
                    MATCH (node)-[r]->(neighbor)
                    RETURN node.name + ' ' + type(r) + ' ' + neighbor.name AS text,
                           elementId(r) AS relationship_id, score
                    ORDER BY score DESC, relationship_id
                    LIMIT 5
                """, {"query": query})
                return [_item("neo4j", r, rank, r.get("score")) for rank, r in enumerate(rows, 1)], None
            except Exception as exc:
                return [], exc

        tasks = [asyncio.create_task(vector_search()), asyncio.create_task(graph_search())]
        try:
            vector_items, (graph_items, error) = await asyncio.gather(*tasks)
        except BaseException:
            # gather does not cancel siblings on failure; finish child work before
            # closing retrieval so failures/disconnects cannot leave orphan spans.
            for task in tasks:
                task.cancel()
            vector, graph = await asyncio.gather(*tasks, return_exceptions=True)
            vector_items = vector if isinstance(vector, list) else []
            graph_items, graph_error = graph if isinstance(graph, tuple) else ([], graph)
            _record_evidence(query, RetrievalResult(
                _deduplicate(vector_items + graph_items),
                vector_status="success" if isinstance(vector, list) else failure_status(vector),
                graph_status=failure_status(graph_error) if graph_error is not None else "success",
                graph_error_type=type(graph_error).__name__ if graph_error is not None else None,
            ))
            raise
        items = _deduplicate(vector_items + graph_items)
        if error is not None:
            mark_failure(span, error, status="partial")
        span.set_attribute("retrieval.result_count", len(items))
        span.set_attribute("retrieval.vector_status", "success")
        span.set_attribute("retrieval.graph_status", failure_status(error) if error is not None else "success")
        result = RetrievalResult(items, graph_status=failure_status(error) if error is not None else "success",
                                 graph_error_type=type(error).__name__ if error is not None else None)
        _record_evidence(query, result)
        return result


def _record_evidence(query, result):
    capture = current_capture.get()
    if capture is not None:
        ordinal = capture.next_ordinal('retrieval')
        capture.record(PayloadSlot(kind='retrieval_query', ordinal=ordinal),
                       {'text': query}, capture.source(query))
        for index, item in enumerate(result.items, 1):
            proof = capture.proof('retrieval_chunk', item.content,
                                  collection=settings.QDRANT_COLLECTION, document_id=item.document_id)
            capture.record(PayloadSlot(kind='retrieval_chunk', ordinal=ordinal, item_ordinal=index),
                           {'text': item.content}, proof)
            if item.retrieval_source == 'qdrant':
                filename = item.metadata.get('filename', 'unknown')
                formatted = CITATION_TEMPLATE.format(text=item.content, filename=filename)
                proof = capture.combine(proof, capture.proof('filename', filename),
                    capture.proof('template', CITATION_TEMPLATE, template_id='retrieval-citation-v1'))
            else:
                formatted = item.content
            capture.remember(formatted, proof)
    collector = current_collector.get()
    if collector is not None:
        collector.record_retrieval(RetrievalEvidence(
            query_hash=content_hash(query), collection=settings.QDRANT_COLLECTION,
            vector_status=result.vector_status, graph_status=result.graph_status,
            graph_error_type=result.graph_error_type,
            items=[RetrievedEvidence(retrieval_source=i.retrieval_source, document_id=i.document_id,
                document_version=i.document_version, chunk_id=i.chunk_id, rank=i.rank,
                score=i.score, content_hash=i.content_hash) for i in result.items],
        ))


async def retrieve_node(state: AgentState, config: RunnableConfig = None) -> dict:
    with bind_collector(collector_from_config(config)):
        result = await retrieve_context(state["current_query"])
    return {"documents": [
        f"{item.content} [Source: {item.metadata.get('filename', 'unknown')}]"
        if item.retrieval_source == "qdrant" else item.content
        for item in result.items
    ]}
