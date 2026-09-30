from types import SimpleNamespace

import pytest
from qdrant_client import models

from services.api.app.local_qdrant_init import ensure_collection


class FakeClient:
    def __init__(self, vectors=None):
        self.vectors = vectors
        self.created = []

    def get_collections(self):
        collections = [SimpleNamespace(name="rag_collection")] if self.vectors is not None else []
        return SimpleNamespace(collections=collections)

    def get_collection(self, name):
        return SimpleNamespace(config=SimpleNamespace(params=SimpleNamespace(vectors=self.vectors)))

    def create_collection(self, **kwargs):
        self.created.append(kwargs)


def test_creates_missing_collection_with_embedding_dimension():
    client = FakeClient()
    ensure_collection(client, "rag_collection", 1024)
    assert client.created == [{
        "collection_name": "rag_collection",
        "vectors_config": models.VectorParams(size=1024, distance=models.Distance.COSINE),
    }]


def test_preserves_compatible_existing_collection():
    client = FakeClient(models.VectorParams(size=1024, distance=models.Distance.COSINE))
    ensure_collection(client, "rag_collection", 1024)
    assert client.created == []


def test_rejects_incompatible_existing_collection_without_recreating():
    client = FakeClient(models.VectorParams(size=768, distance=models.Distance.COSINE))
    with pytest.raises(ValueError, match="expected 1024/Cosine"):
        ensure_collection(client, "rag_collection", 1024)
    assert client.created == []
