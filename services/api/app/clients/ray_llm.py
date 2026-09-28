# services/api/app/clients/ray_llm.py
import httpx
import logging
import backoff
import time
import hashlib
import json
from typing import List, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field
from libs.observability.tracing import domain_span, failure_status, set_span_attribute
from services.api.app.config import settings

logger = logging.getLogger(__name__)


class LLMResult(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True)
    text: str
    model: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    cached_tokens: int | None = Field(default=None, ge=0)
    finish_reason: str | None = None
    provider_request_id: str | None = None
    completion_id: str | None = None


def _retry_observed(details):
    from opentelemetry import trace
    set_span_attribute(trace.get_current_span(), "retry.count", details["tries"])

class RayLLMClient:
    """
    Async Client with proper Connection Pooling.
    """
    def __init__(self):
        self.endpoint = settings.RAY_LLM_ENDPOINT 
        # Client is initialized in startup_event
        self.client: Optional[httpx.AsyncClient] = None

    async def start(self):
        """Called during App Startup"""
        limits = httpx.Limits(max_keepalive_connections=20, max_connections=50)
        headers = {}
        if settings.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {settings.LLM_API_KEY}"
        self.client = httpx.AsyncClient(
            timeout=settings.LLM_TIMEOUT,
            limits=limits,
            headers=headers,
        )
        logger.info("Ray LLM Client initialized.")

    async def close(self):
        """Called during App Shutdown"""
        if self.client:
            await self.client.aclose()

    @backoff.on_exception(
        backoff.expo,
        (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError),
        max_tries=3,
        jitter=backoff.full_jitter,
        on_backoff=_retry_observed,
        logger=None,
    )
    async def _request(self, payload):
        return await self.client.post(self.endpoint, json=payload)

    async def chat_completion_result(self, messages: List[Dict], temperature: float = 0.7, json_mode: bool = False, max_tokens: int = 1024, *, provenance=None, purpose=None) -> LLMResult:
        """`provenance` proves these exact messages (e.g. a flattened, component-proven prompt);
        without it, provenance is derived from the message bytes themselves."""
        started = time.perf_counter()
        result = failure = None
        with domain_span("llm.chat", attributes={
            "gen_ai.request.model": settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
            "gen_ai.request.temperature": temperature, "retry.count": 0,
        }, schema_version=1) as span:
            proof = span.provenance.messages(messages, explicit=provenance)
            request = {
                'messages': messages, 'model': settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
                'temperature': temperature, 'max_tokens': max_tokens,
            }
            digest_value = {'messages': messages, 'temperature': temperature, 'max_tokens': max_tokens}
            request_hash = 'sha256:' + hashlib.sha256(
                json.dumps(digest_value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
            span.record('request', facts={
                **({'purpose': purpose} if purpose is not None else {}), 'request_hash': request_hash,
                'requested_model': settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
                'temperature': float(temperature), 'max_tokens': max_tokens,
            }, content={'request': span.content(request, provenance=proof)})
            try:
                result = await self._completion(messages, temperature, max_tokens, span)
                return result
            except BaseException as exc:
                failure = exc
                raise
            finally:
                facts = {
                    'status': failure_status(failure) if failure is not None else 'success',
                    'retry_count': span.attributes.get('retry.count', 0),
                    'latency_ms': (time.perf_counter() - started) * 1000,
                }
                if failure is not None:
                    facts['error_type'] = type(failure).__name__
                if result is not None:
                    facts.update({key: value for key, value in result.model_dump(exclude={'text'}).items()
                                  if value is not None})
                span.record('result', facts=facts, content={
                    'response': span.content({'text': result.text}, provenance=proof)
                } if result is not None else None)

    async def _completion(self, messages, temperature, max_tokens, span):
        if not self.client:
            raise RuntimeError("Client not initialized. Call start() first.")

        payload = {
            "model": settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        response = await self._request(payload)
        span.set_attribute("http.response.status_code", response.status_code)
        response.raise_for_status()
        try:
            body = response.json()
            if "error" in body:
                raise ValueError("provider error")
            choice = body["choices"][0]
            usage = body.get("usage") or {}
            result = LLMResult(
                text=choice["message"]["content"], model=body.get("model"),
                input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
                cached_tokens=(usage.get("prompt_tokens_details") or {}).get("cached_tokens"),
                finish_reason=choice.get("finish_reason"), provider_request_id=response.headers.get("x-request-id"),
                completion_id=body.get("id"),
            )
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise ValueError("Invalid LLM response") from None
        for key, value in {
            "gen_ai.response.model": result.model, "gen_ai.usage.input_tokens": result.input_tokens,
            "gen_ai.usage.output_tokens": result.output_tokens, "gen_ai.usage.cached_tokens": result.cached_tokens,
            "gen_ai.response.finish_reason": result.finish_reason, "gen_ai.response.id": result.completion_id,
            "provider.request_id": result.provider_request_id,
        }.items():
            if value is not None:
                span.set_attribute(key, value)
        return result

    async def chat_completion(self, messages: List[Dict], temperature: float = 0.7, json_mode: bool = False, max_tokens: int = 1024) -> str:
        """Compatibility boundary for existing string callers."""
        return (await self.chat_completion_result(messages, temperature, json_mode, max_tokens)).text

# Global Instance (Managed by Lifespan in main.py)
llm_client = RayLLMClient()
