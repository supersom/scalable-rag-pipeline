# services/api/app/models/embedding_engine.py
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


embed_app = EmbedDeployment.bind()  # default args; override in serve.py for cloud
