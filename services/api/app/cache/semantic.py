# services/api/app/cache/semantic.py
import json
import logging
from typing import Optional
from qdrant_client.http import models as qdrant_models
from services.api.app.clients.ray_embed import embed_client
from services.api.app.clients.qdrant import qdrant_client
from services.api.app.config import settings

logger = logging.getLogger(__name__)

class SemanticCache:
    """
    Implements Semantic Caching using Vector Search.
    Instead of exact string matching, we match by meaning.
    """

    async def ensure_collection(self):
        """Create the semantic_cache collection in Qdrant if it doesn't exist."""
        collections = await qdrant_client.client.get_collections()
        names = {c.name for c in collections.collections}
        if "semantic_cache" not in names:
            await qdrant_client.client.create_collection(
                collection_name="semantic_cache",
                vectors_config=qdrant_models.VectorParams(
                    size=settings.EMBED_DIM,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
            logger.info("Created semantic_cache collection in Qdrant")

    async def get_cached_response(self, query: str, threshold: float = 0.95) -> Optional[str]:
        """
        Check if a similar query exists in the cache.
        """
        try:
            await self.ensure_collection()
            # 1. Embed the incoming query (Fast CPU/GPU call)
            vector = await embed_client.embed_query(query)
            
            # 2. Search in a specific 'cache' collection in Qdrant 
            # (or Redis Vector if configured)
            results = await qdrant_client.client.search(
                collection_name="semantic_cache",
                query_vector=vector,
                limit=1,
                with_payload=True,
                score_threshold=threshold # Only extremely similar queries
            )
            
            if results:
                logger.info(f"Semantic Cache Hit! Score: {results[0].score}")
                return results[0].payload["answer"]
            
        except Exception as e:
            logger.warning(f"Semantic cache lookup failed: {e}")
            
        return None

    async def set_cached_response(self, query: str, answer: str):
        """
        Save a Q&A pair to the cache.
        """
        try:
            # 1. Embed query
            vector = await embed_client.embed_query(query)
            
            # 2. Save to Vector DB
            import uuid
            from qdrant_client.http import models
            
            await qdrant_client.client.upsert(
                collection_name="semantic_cache",
                points=[
                    models.PointStruct(
                        id=str(uuid.uuid4()),
                        vector=vector,
                        payload={"query": query, "answer": answer}
                    )
                ]
            )
        except Exception as e:
            logger.warning(f"Failed to write to semantic cache: {e}")

semantic_cache = SemanticCache()