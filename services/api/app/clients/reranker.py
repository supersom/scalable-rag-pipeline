"""Hosted reranking client using the OpenRouter rerank API contract."""
from dataclasses import dataclass
import math

import httpx

from libs.observability.tracing import domain_span
from services.api.app.config import settings


@dataclass(frozen=True)
class RerankResult:
    index: int
    score: float


class OpenRouterRerankerClient:
    """Optional hosted reranker; OpenAI-compatible chat/embedding APIs do not define rerank."""

    def __init__(self):
        self.client: httpx.AsyncClient | None = None

    @property
    def enabled(self) -> bool:
        return bool(settings.RERANK_API_ENDPOINT)

    async def start(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=settings.RERANK_TIMEOUT)

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def rerank(self, query: str, documents: list[str]) -> list[RerankResult]:
        if not self.enabled:
            return []
        if not settings.RERANK_MODEL_NAME:
            raise ValueError("RERANK_MODEL_NAME is required when reranking is enabled")
        if not documents:
            return []

        with domain_span("retrieval.rerank", attributes={
            "rerank.model": settings.RERANK_MODEL_NAME,
            "rerank.input_count": len(documents),
        }) as span:
            headers = {}
            api_key = settings.RERANK_API_KEY or settings.LLM_API_KEY
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
            payload = {
                "model": settings.RERANK_MODEL_NAME,
                "query": query,
                "documents": documents,
                "top_n": len(documents),
            }
            if self.client is None:
                async with httpx.AsyncClient(timeout=settings.RERANK_TIMEOUT) as client:
                    response = await client.post(settings.RERANK_API_ENDPOINT, json=payload, headers=headers)
            else:
                response = await self.client.post(settings.RERANK_API_ENDPOINT, json=payload, headers=headers)
            span.set_attribute("http.response.status_code", response.status_code)
            response.raise_for_status()
            try:
                rows = response.json()["results"]
                if not isinstance(rows, list):
                    raise ValueError("invalid rerank results")
                results = []
                seen = set()
                for row in rows:
                    index = row["index"]
                    score = row["relevance_score"]
                    if (isinstance(index, bool) or not isinstance(index, int) or
                            not 0 <= index < len(documents) or index in seen):
                        raise ValueError("invalid rerank index")
                    if isinstance(score, bool) or not isinstance(score, (float, int)) or not math.isfinite(score):
                        raise ValueError("invalid rerank score")
                    seen.add(index)
                    results.append(RerankResult(index=index, score=float(score)))
            except (KeyError, TypeError, ValueError):
                raise ValueError("Invalid reranker response") from None
            span.set_attribute("rerank.result_count", len(results))
            return results


reranker_client = OpenRouterRerankerClient()
