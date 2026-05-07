# services/models/serve.py
# Deploys LLM and Embedding models as Ray Serve endpoints on a single port.
# Mirrors Ollama's API — switch backends by updating .env, no client code changes.
#
# Endpoints (both on port 8001):
#   LLM  : POST http://localhost:8001/v1/chat/completions
#   Embed: POST http://localhost:8001/api/embeddings
#
# Usage:
#   python services/models/serve.py
#   or: make serve-models

import os
from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), "../../.env"))

import ray
from ray import serve

from ..api.app.models.vllm_engine import LLMDeployment
from ..api.app.models.embedding_engine import EmbedDeployment

LOCAL = {
    "llm": dict(
        model_id="AMead10/Llama-3.2-1B-Instruct-AWQ",
        quantization="awq_marlin",
        gpu_memory_utilization=0.52,
        max_model_len=4096,
        cpu_offload_gb=1.1,
        enforce_eager=True,
        num_gpus=0.7,
    ),
    "embed": dict(
        model_id="nomic-ai/nomic-embed-text-v1",
        compile=False,
        num_gpus=0.3,
    ),
}

CLOUD = {
    "llm": dict(
        model_id="meta-llama/Meta-Llama-3-70B-Instruct",
        quantization=None,
        gpu_memory_utilization=0.90,
        max_model_len=8192,
        cpu_offload_gb=0.0,
        enforce_eager=False,
        num_gpus=1,
    ),
    "embed": dict(
        model_id="BAAI/bge-m3",
        compile=True,
        num_gpus=0.5,
    ),
}

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=["local", "cloud"], default="local")
    args = parser.parse_args()

    profile = LOCAL if args.profile == "local" else CLOUD

    # Propagate .env vars to Ray worker processes.
    # VLLM_USE_V1=0 forces the stable v0 engine — v1 in 0.20.1 has a bug with EngineArgs.
    env_vars = {k: v for k, v in os.environ.items()}
    env_vars["VLLM_USE_V1"] = "0"
    ray.init(runtime_env={"env_vars": env_vars})
    serve.start(http_options={"host": "0.0.0.0", "port": 8001})

    # Route prefix maps to the path prefix before the FastAPI route.
    # LLMDeployment  has @app.post("/chat/completions") → /v1/chat/completions
    # EmbedDeployment has @app.post("/embeddings")       → /api/embeddings
    llm_cfg = profile["llm"]
    llm_deployment = LLMDeployment.options(
        ray_actor_options={"num_gpus": llm_cfg.pop("num_gpus")}
    ).bind(**llm_cfg)

    embed_cfg = profile["embed"]
    embed_deployment = EmbedDeployment.options(
        ray_actor_options={"num_gpus": embed_cfg.pop("num_gpus")}
    ).bind(**embed_cfg)

    serve.run(llm_deployment, route_prefix="/v1", name="llm")
    serve.run(embed_deployment, route_prefix="/api", name="embed")

    print("Ray Serve running:")
    print("  LLM  : http://localhost:8001/v1/chat/completions")
    print("  Embed: http://localhost:8001/api/embeddings")
    print("Update .env to switch from Ollama:")
    print("  RAY_LLM_ENDPOINT=http://localhost:8001/v1/chat/completions")
    print("  RAY_EMBED_ENDPOINT=http://localhost:8001/api/embeddings")

    import signal
    signal.pause()
