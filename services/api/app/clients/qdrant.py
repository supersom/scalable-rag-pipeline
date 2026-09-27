# services/api/app/clients/qdrant.py
from qdrant_client import QdrantClient, AsyncQdrantClient
from services.api.app.config import settings
from libs.observability.tracing import domain_span

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

    async def search(self, vector: list[float], limit: int = 5, *, collection_name: str | None = None, score_threshold: float | None = None):
        """
        Performs Semantic Search.
        """
        collection = collection_name or settings.QDRANT_COLLECTION
        with domain_span("qdrant.search", attributes={"db.collection.name": collection, "retrieval.limit": limit}) as span:
            kwargs = dict(collection_name=collection, limit=limit, with_payload=True, score_threshold=score_threshold)
            if hasattr(self.client, "search"):
                results = await self.client.search(query_vector=vector, **kwargs)
            else:
                # Qdrant >=1.16 removed search; the pinned API image still uses it.
                results = (await self.client.query_points(query=vector, **kwargs)).points
            span.set_attribute("retrieval.result_count", len(results))
            return results

# Global instance
qdrant_client = VectorDBClient()
