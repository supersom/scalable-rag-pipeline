# pipelines/ingestion/embedding/compute.py
import os
import httpx
from typing import Dict, Any

# Reads from env so the same code works locally (Ollama) and in K8s (Ray Serve)
EMBED_ENDPOINT = os.getenv("RAY_EMBED_ENDPOINT", "http://localhost:11434/api/embeddings")
EMBED_MODEL    = os.getenv("EMBED_MODEL_NAME",   "nomic-embed-text")

# Derive batch endpoint: swap /api/embeddings → /api/embed (Ollama ≥0.3.6 batch API)
_base = EMBED_ENDPOINT.rsplit("/", 1)[0]
EMBED_BATCH_ENDPOINT = f"{_base}/embed"


class BatchEmbedder:
    """
    Callable class for Ray Data map_batches.
    Embeds a whole batch in one request via Ollama's /api/embed endpoint.
    """
    def __init__(self):
        self.batch_endpoint = EMBED_BATCH_ENDPOINT
        self.model          = EMBED_MODEL
        self.client         = httpx.Client(timeout=120.0)

    def __call__(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        texts = batch["text"].tolist()  # ndarray → list[str] for JSON serialization
        response = self.client.post(
            self.batch_endpoint,
            json={"model": self.model, "input": texts}
        )
        response.raise_for_status()
        batch["vector"] = response.json()["embeddings"]
        return batch
