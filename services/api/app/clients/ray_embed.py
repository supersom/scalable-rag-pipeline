# services/api/app/clients/ray_embed.py
import httpx
from services.api.app.config import settings

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
                json={"model": settings.EMBED_MODEL_NAME, "prompt": text}
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
                    json={"model": settings.EMBED_MODEL_NAME, "prompt": text}
                )
                response.raise_for_status()
                embeddings.append(response.json()["embedding"])
            return embeddings

embed_client = RayEmbedClient()