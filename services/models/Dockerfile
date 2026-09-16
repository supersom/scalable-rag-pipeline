# services/models/Dockerfile
# Custom Ray Serve GPU image for in-cluster LLM + embedding inference.
#
# Why a custom image (vs runtime_env.working_dir): GPU workers scale from zero on
# Karpenter spot nodes and churn often. Baking vllm/torch/sentence-transformers and
# the app code into the image means a fresh GPU worker is serving in seconds instead
# of pip-installing ~3GB on every cold start.
#
# Build/push (run from repo root):
#   aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin 764988411032.dkr.ecr.us-east-1.amazonaws.com
#   docker build -t 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:<tag> -f services/models/Dockerfile .
#   docker push 764988411032.dkr.ecr.us-east-1.amazonaws.com/services/ray-serve:<tag>
#
# VERSION MATRIX — VALIDATE ON FIRST BUILD:
#   Ray base is 2.9.0 to match the RayService manifests (rayVersion) and the installed
#   KubeRay operator. Llama-3 needs vLLM >= 0.4.0, which is newer than the Ray 2.9.0 era,
#   so vllm/torch may conflict with Ray 2.9.0's pinned deps. If the build or engine init
#   fails on dependency conflicts, the likely fix is to bump BOTH the Ray base image here
#   AND rayVersion in deploy/ray/ray-serve-*.yaml (and confirm the KubeRay operator supports
#   that Ray version) — keep them in lockstep.
FROM rayproject/ray:2.9.0-py310-gpu

# vLLM (brings a matching torch), plus embedding deps. Pinned conservatively to the
# earliest stable vLLM with Llama-3 support to stay close to the Ray 2.9.0 dependency set.
RUN pip install --no-cache-dir \
      "vllm==0.4.2" \
      "sentence-transformers==2.7.0" \
      "transformers>=4.40,<4.45" \
      "fastapi==0.109.0" \
      "awscli>=1.32,<2"

# FastAPI 0.109 avoids the newer non-serializable route lock. Starlette 0.35
# uses Middleware.kwargs, while Ray 2.9 still reads Middleware.options, so patch
# that single compatibility point and validate the result.
RUN python - <<'PY'
from pathlib import Path
import fastapi
import starlette
from starlette.middleware import Middleware

middleware = Middleware(object)
if fastapi.__version__ != "0.109.0" or not hasattr(middleware, "kwargs"):
    raise SystemExit(
        f"incompatible Serve web stack: fastapi={fastapi.__version__}, "
        f"starlette={starlette.__version__}"
    )

proxy = Path("/home/ray/anaconda3/lib/python3.10/site-packages/ray/serve/_private/proxy.py")
text = proxy.read_text()
old = "self.wrapped_http_proxy, **middleware.options"
new = "self.wrapped_http_proxy, **middleware.kwargs"
if old not in text:
    raise SystemExit(f"expected Ray proxy middleware expression not found in {proxy}")
text = text.replace(old, new)

# Starlette 0.35 derives the route path from the full ASGI path and root_path.
# Ray 2.9 strips the prefix as well as setting root_path, causing every FastAPI
# endpoint below a non-root Serve route prefix to return 404.
old_path = 'proxy_request.set_path(route_path.replace(route_prefix, "", 1))'
new_path = "proxy_request.set_path(route_path)"
if old_path not in text:
    raise SystemExit(f"expected Ray proxy route rewrite not found in {proxy}")
proxy.write_text(text.replace(old_path, new_path))
compile(proxy.read_text(), str(proxy), "exec")
PY

# Model actors run as the non-root `ray` user and sync weights here at startup.
USER root
RUN mkdir -p /model-cache && chown ray:users /model-cache
USER ray

WORKDIR /app
COPY services /app/services
ENV PYTHONPATH=/app

# Importing these symbols forces Ray to serialize both FastAPI ingress apps.
RUN python - <<'PY'
import shutil
import subprocess

from services.api.app.models.embedding_engine import app as embedding_app
from services.api.app.models.vllm_engine import app as llm_app

assert embedding_app is not None
assert llm_app is not None
assert shutil.which("aws") is not None
subprocess.run(["aws", "--version"], check=True)
PY
