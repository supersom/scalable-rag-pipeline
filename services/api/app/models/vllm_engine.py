# services/api/app/models/vllm_engine.py
import os
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from ray import serve

_DEFAULT_MODEL_ID = "meta-llama/Meta-Llama-3-70B-Instruct"

_app = FastAPI()


@serve.deployment(
    autoscaling_config={"min_replicas": 1, "max_replicas": 4},
    ray_actor_options={"num_gpus": 1},
)
@serve.ingress(_app)
class LLMDeployment:
    def __init__(
        self,
        model_id: str = _DEFAULT_MODEL_ID,
        quantization: str | None = None,
        gpu_memory_utilization: float = 0.90,
        max_model_len: int = 8192,
        cpu_offload_gb: float = 0.0,
        enforce_eager: bool = False,
    ):
        from vllm import AsyncLLMEngine, EngineArgs
        from transformers import AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        engine_args = EngineArgs(
            model=model_id,
            quantization=quantization,
            gpu_memory_utilization=gpu_memory_utilization,
            max_model_len=max_model_len,
            cpu_offload_gb=cpu_offload_gb,
            enforce_eager=enforce_eager,
        )
        # vLLM 0.20.1 bug: v1 engine accesses enable_log_requests which is missing from EngineArgs
        if not hasattr(engine_args, "enable_log_requests"):
            engine_args.enable_log_requests = False
        self.engine = AsyncLLMEngine.from_engine_args(engine_args)

    @_app.post("/chat/completions")
    async def chat_completions(self, request: Request) -> JSONResponse:
        from vllm import SamplingParams

        body = await request.json()
        prompt = self.tokenizer.apply_chat_template(
            body.get("messages", []),
            tokenize=False,
            add_generation_prompt=True,
        )
        params = SamplingParams(
            temperature=float(body.get("temperature", 0.7)),
            max_tokens=int(body.get("max_tokens", 1024)),
            stop_token_ids=[
                self.tokenizer.eos_token_id,
                self.tokenizer.convert_tokens_to_ids("<|eot_id|>"),
            ],
        )
        final = None
        async for out in self.engine.generate(prompt, params, os.urandom(8).hex()):
            final = out

        return JSONResponse({
            "choices": [{"message": {"role": "assistant", "content": final.outputs[0].text}}]
        })


llm_app = LLMDeployment.bind()  # default args; override in serve.py for cloud
