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
            if settings.EMBED_API_ENDPOINT:
                if self.client is None:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        vectors = await self._hosted_request(client, [text], prefix, span)
                else:
                    vectors = await self._hosted_request(self.client, [text], prefix, span)
                return vectors[0]
            if self.client is None:
                async with httpx.AsyncClient(timeout=60.0) as client:
                    return await self._request(client, text, prefix, span)
            return await self._request(self.client, text, prefix, span)

    async def _hosted_request(self, client, texts, prefix, span):
        headers = {}
        api_key = settings.EMBED_API_KEY or settings.LLM_API_KEY
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        response = await client.post(
            settings.EMBED_API_ENDPOINT,
            json={"model": settings.EMBED_MODEL_NAME,
                  "input": [_prefixed(text, prefix) for text in texts]},
            headers=headers,
            timeout=60.0,
        )
        span.set_attribute("http.response.status_code", response.status_code)
        response.raise_for_status()
        try:
            rows = response.json()["data"]
            if not isinstance(rows, list) or len(rows) != len(texts):
                raise ValueError("invalid embedding batch")
            indexed = {}
            for row in rows:
                index = row["index"]
                vector = row["embedding"]
                if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(texts):
                    raise ValueError("invalid embedding index")
                if index in indexed or not isinstance(vector, list) or not vector or any(
                    isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
                    for value in vector
                ):
                    raise ValueError("invalid embedding vector")
                indexed[index] = vector
            if len(indexed) != len(texts):
                raise ValueError("incomplete embedding batch")
            vectors = [indexed[index] for index in range(len(texts))]
        except (KeyError, TypeError, ValueError):
            raise ValueError("Invalid embedding response") from None
        span.set_attribute("embedding.dimensions", len(vectors[0]))
        return vectors

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
        """Embed documents; hosted endpoints batch requests, Ray remains compatible."""
        if settings.EMBED_API_ENDPOINT and texts:
            with domain_span("embedding.create", attributes={
                "embedding.model": settings.EMBED_MODEL_NAME, "embedding.input_count": len(texts),
            }) as span:
                if self.client is None:
                    async with httpx.AsyncClient(timeout=60.0) as client:
                        return await self._hosted_request(client, texts, "search_document", span)
                return await self._hosted_request(self.client, texts, "search_document", span)
        return [await self._embed(text, "search_document") for text in texts]

embed_client = RayEmbedClient()
