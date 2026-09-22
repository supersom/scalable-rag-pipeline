# services/api/app/clients/ray_embed.py
import httpx
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
    async def start(self):
        pass

    async def close(self):
        pass

    async def embed_query(self, text: str) -> list[float]:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                settings.RAY_EMBED_ENDPOINT,
                json={"model": settings.EMBED_MODEL_NAME, "prompt": _prefixed(text, "search_query")}
            )
            response.raise_for_status()
            return response.json()["embedding"]

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """Used during ingestion — batches via sequential Ollama calls"""
        async with httpx.AsyncClient(timeout=60.0) as client:
            embeddings = []
            for text in texts:
                response = await client.post(
                    settings.RAY_EMBED_ENDPOINT,
                    json={"model": settings.EMBED_MODEL_NAME, "prompt": _prefixed(text, "search_document")}
                )
                response.raise_for_status()
                embeddings.append(response.json()["embedding"])
            return embeddings

embed_client = RayEmbedClient()