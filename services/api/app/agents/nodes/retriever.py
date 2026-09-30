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
from services.api.app.clients.reranker import reranker_client
from services.api.app.config import settings
from langchain_core.runnables import RunnableConfig

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
    retrieval_rank: int | None = None
    rerank_score: float | None = None


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
        content, hashlib.sha256(content.encode()).hexdigest(), score, rank, metadata, rank,
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
    with domain_span("retrieval", schema_version=1) as span:
        span.record('request', facts={
            'query_hash': 'sha256:' + hashlib.sha256(query.encode()).hexdigest(),
            'collection': settings.QDRANT_COLLECTION,
        }, content={'query': span.content(query, provenance=span.provenance.source(query))})
        try:
            query_vector = await embed_client.embed_query(query)
        except BaseException as exc:
            _record_result(span, RetrievalResult([], vector_status=failure_status(exc), graph_status="skipped"))
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
            _record_result(span, RetrievalResult(
                _deduplicate(vector_items + graph_items),
                vector_status="success" if isinstance(vector, list) else failure_status(vector),
                graph_status=failure_status(graph_error) if graph_error is not None else "success",
                graph_error_type=type(graph_error).__name__ if graph_error is not None else None,
            ))
            raise
        items = _deduplicate(vector_items + graph_items)
        if error is not None:
            mark_failure(span, error, status="partial")
        if items and reranker_client.enabled:
            try:
                ranked = await reranker_client.rerank(query, [item.content for item in items])
                if ranked:
                    items = [replace(items[result.index], rank=rank, rerank_score=result.score)
                             for rank, result in enumerate(ranked, 1)]
                span.set_attribute("retrieval.reranker_status", "success")
            except Exception as exc:
                # Reranking is an optional quality enhancement: provider outages
                # must not turn otherwise successful retrieval into a hard failure.
                span.set_attribute("retrieval.reranker_status", "error")
                mark_failure(span, exc, status="partial")
        else:
            span.set_attribute("retrieval.reranker_status", "skipped")
        span.set_attribute("retrieval.result_count", len(items))
        span.set_attribute("retrieval.vector_status", "success")
        span.set_attribute("retrieval.graph_status", failure_status(error) if error is not None else "success")
        result = RetrievalResult(items, graph_status=failure_status(error) if error is not None else "success",
                                 graph_error_type=type(error).__name__ if error is not None else None)
        _record_result(span, result)
        return result


def _record_result(span, result):
    for item in result.items:
        proof = span.provenance.proof('retrieval_chunk', item.content,
                                    collection=settings.QDRANT_COLLECTION, document_id=item.document_id)
        span.record('hit', facts={key: value for key, value in {
            'retrieval_source': item.retrieval_source, 'document_id': item.document_id,
            'document_version': item.document_version, 'chunk_id': item.chunk_id,
            'rank': item.rank, 'retrieval_rank': item.retrieval_rank,
            'score': item.score, 'rerank_score': item.rerank_score,
            'content_hash': item.content_hash,
        }.items() if value is not None}, content={'chunk': span.content(item.content, provenance=proof)})
        if item.retrieval_source == 'qdrant':
            filename = item.metadata.get('filename', 'unknown')
            formatted = CITATION_TEMPLATE.format(text=item.content, filename=filename)
            proof = span.provenance.combine(proof, span.provenance.proof('filename', filename),
                span.provenance.template(CITATION_TEMPLATE, template_id='retrieval-citation-v1'))
        else:
            formatted = item.content
        span.provenance.remember(formatted, proof)
    span.record('result', facts={key: value for key, value in {
        'collection': settings.QDRANT_COLLECTION, 'vector_status': result.vector_status,
        'graph_status': result.graph_status, 'graph_error_type': result.graph_error_type,
    }.items() if value is not None})


async def retrieve_node(state: AgentState, config: RunnableConfig = None) -> dict:
    result = await retrieve_context(state["current_query"])
    return {"documents": [
        f"{item.content} [Source: {item.metadata.get('filename', 'unknown')}]"
        if item.retrieval_source == "qdrant" else item.content
        for item in result.items
    ]}
