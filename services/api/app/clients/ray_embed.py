# services/api/app/clients/ray_embed.py
import httpx
import math
from libs.observability.tracing import domain_span
from services.api.app.config import settings

# nomic-embed-text was contrastively trained on task-prefixed inputs and its
# model card says the prefix "must" be included; BAAI/bge-m3 (production)
# explicitly does the opposite — its card says it "no longer requires adding
# instructions" — so prefixing bge-m3 input would feed it literal instruction
# text it was never trained to strip. Gate on EMBED_MODEL_NAME, not a global
# toggle, so the same client code stays correct across both.
def _prefixed(text: str, prefix: str) -> str:
    if "nomic-embed-text" in settings.EMBED_MODEL_NAME:
        return f"{prefix}: {text}"
    return text

class RayEmbedClient:
    """
    Client for the Ray Serve Embedding Service.
    Uses HTTPX for async non-blocking HTTP calls.
    """
    def __init__(self):
        self.client: httpx.AsyncClient | None = None

    async def start(self):
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=60.0)

    async def close(self):
        if self.client is not None:
            await self.client.aclose()
            self.client = None

    async def embed_query(self, text: str) -> list[float]:
        return await self._embed(text, "search_query")

    async def _embed(self, text, prefix):
        with domain_span("embedding.create", attributes={"embedding.model": settings.EMBED_MODEL_NAME}) as span:
            if self.client is None:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    return await self._request(client, text, prefix, span)
            return await self._request(self.client, text, prefix, span)

    async def _request(self, client, text, prefix, span):
        response = await client.post(settings.RAY_EMBED_ENDPOINT,
            json={"model": settings.EMBED_MODEL_NAME, "prompt": _prefixed(text, prefix)},
            timeout=5.0 if prefix == "search_query" else 60.0)
        span.set_attribute("http.response.status_code", response.status_code)
        response.raise_for_status()
        try:
            vector = response.json()["embedding"]
            if not isinstance(vector, list) or not vector or any(
                isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector
            ):
                raise ValueError("invalid vector")
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid embedding response") from None
        span.set_attribute("embedding.dimensions", len(vector))
        return vector

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Used during ingestion — batches via sequential Ollama calls"""
        return [await self._embed(text, "search_document") for text in texts]

embed_client = RayEmbedClient()
