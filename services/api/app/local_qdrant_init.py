"""Initialize the local RAG collection before the API accepts requests."""

import os
import time

import httpx
from qdrant_client import QdrantClient, models


def ensure_collection(client: QdrantClient, name: str, vector_size: int) -> None:
    if name in {collection.name for collection in client.get_collections().collections}:
        vectors = client.get_collection(name).config.params.vectors
        if not isinstance(vectors, models.VectorParams):
            raise ValueError(f"Collection {name!r} does not use an unnamed vector")
        if vectors.size != vector_size or vectors.distance != models.Distance.COSINE:
            raise ValueError(
                f"Collection {name!r} has vector configuration "
                f"{vectors.size}/{vectors.distance}; expected {vector_size}/Cosine"
            )
        return

    client.create_collection(
        collection_name=name,
        vectors_config=models.VectorParams(size=vector_size, distance=models.Distance.COSINE),
    )


def main() -> None:
    host = os.environ.get("QDRANT_HOST", "qdrant")
    port = int(os.environ.get("QDRANT_PORT", "6333"))
    name = os.environ.get("QDRANT_COLLECTION", "rag_collection")
    vector_size = int(os.environ.get("EMBED_DIM", "1024"))
    client = QdrantClient(host=host, port=port, timeout=5)
    for attempt in range(30):
        try:
            ensure_collection(client, name, vector_size)
            print(f"Qdrant collection {name!r} ready ({vector_size}-dimensional Cosine)")
            return
        except (OSError, TimeoutError, httpx.RequestError) as exc:
            if attempt == 29:
                raise RuntimeError("Qdrant did not become available") from exc
            time.sleep(1)


if __name__ == "__main__":
    main()
