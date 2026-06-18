# pipelines/ingestion/indexing/qdrant.py
from qdrant_client import QdrantClient
from qdrant_client.http import models
from typing import List, Dict, Any
import os
import uuid

class QdrantIndexer:
    """
    Writes vectors to Qdrant.
    """
    def __init__(self):
        host = os.getenv("QDRANT_HOST", "qdrant-service")
        port = int(os.getenv("QDRANT_PORT", 6333))
        self.collection_name = os.getenv("QDRANT_COLLECTION", "rag_collection")
        
        self.client = QdrantClient(host=host, port=port)
        
    def write(self, batch: List[Dict[str, Any]]):
        """
        Uploads points in batch.
        """
        points = []
        
        for row in batch:
            # Skip if embedding failed
            if "vector" not in row or len(row["vector"]) == 0:
                continue
                
            # Construct Payload (Metadata)
            payload = {
                "text": row["text"],
                "filename": row["metadata"]["filename"],
                "source_path": row["metadata"].get(
                    "source_path", row["metadata"]["filename"]
                ),
                "page": row["metadata"].get("page_number", 0)
            }
            
            # Create Point
            point_key = (
                f"{payload['source_path']}:{payload['page']}:{payload['text']}"
            )
            points.append(models.PointStruct(
                id=str(uuid.uuid5(uuid.NAMESPACE_URL, point_key)),
                vector=row["vector"],
                payload=payload
            ))
            
        if points:
            # Upsert is atomic
            self.client.upsert(
                collection_name=self.collection_name,
                points=points
            )
