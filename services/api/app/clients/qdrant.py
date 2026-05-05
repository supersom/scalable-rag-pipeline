# services/api/app/clients/qdrant.py
from qdrant_client import QdrantClient, AsyncQdrantClient
from services.api.app.config import settings

class VectorDBClient:
    """
    Async Client for Qdrant.
    """
    def __init__(self):
        self.client = AsyncQdrantClient(
            host=settings.QDRANT_HOST,
            port=settings.QDRANT_PORT,
            prefer_grpc=False  # gRPC port 6334 not exposed in docker-compose; use REST
        )

    async def search(self, vector: list[float], limit: int = 5):
        """
        Performs Semantic Search.
        """
        return await self.client.search(
            collection_name=settings.QDRANT_COLLECTION,
            query_vector=vector,
            limit=limit,
            with_payload=True
        )

# Global instance
qdrant_client = VectorDBClient()