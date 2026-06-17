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
      "transformers>=4.40,<4.45"

WORKDIR /app
COPY services /app/services
ENV PYTHONPATH=/app
