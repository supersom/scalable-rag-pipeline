# services/api/app/models/embedding_engine.py
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from ray import serve

_DEFAULT_MODEL_ID = "BAAI/bge-m3"

_app = FastAPI()


@serve.deployment(
    num_replicas=1,
    ray_actor_options={"num_gpus": 0.5},
)
@serve.ingress(_app)
class EmbedDeployment:
    def __init__(
        self,
        model_id: str = _DEFAULT_MODEL_ID,
        compile: bool = True,
    ):
        import torch
        from sentence_transformers import SentenceTransformer
        # Load on CPU first to avoid float32 OOM spike on GPU, then convert to fp16 and move to CUDA
        self.model = SentenceTransformer(model_id, trust_remote_code=True, device="cpu")
        self.model.half()
        self.model.to("cuda")
        if compile:
            self.model = torch.compile(self.model)

    @_app.post("/embeddings")
    async def embeddings(self, request: Request) -> JSONResponse:
        body = await request.json()
        # Accept {"prompt": "..."} (Ollama style) or {"text": "..."}
        text = body.get("prompt") or body.get("text", "")
        if isinstance(text, list):
            text = text[0]

        embedding = self.model.encode(text, normalize_embeddings=True).tolist()
        return JSONResponse({"embedding": embedding})


def build_app(overrides: dict | None = None):
    """Build the embedding Serve application.

    KubeRay RayService imports this module's `app` symbol (import_path: ...embedding_engine:app);
    config comes from env vars set in the RayService manifest. The local serve.py path calls
    EmbedDeployment.options(...).bind(...) directly instead.
    """
    cfg = {
        "model_id": os.getenv("EMBED_MODEL_ID", _DEFAULT_MODEL_ID),
        "compile": os.getenv("EMBED_COMPILE", "true").lower() == "true",
    }
    if overrides:
        cfg.update(overrides)
    num_gpus = float(os.getenv("EMBED_NUM_GPUS", "0.5"))
    return EmbedDeployment.options(ray_actor_options={"num_gpus": num_gpus}).bind(**cfg)


# Entrypoint for KubeRay RayService.
app = build_app()
