# services/api/app/clients/ray_llm.py
import httpx
import logging
import backoff
import time
from typing import List, Dict, Optional
from pydantic import BaseModel, ConfigDict, Field
from libs.observability.tracing import domain_span, failure_status, set_span_attribute
from services.api.app.evaluation.context import current_collector, model_purpose, content_hash
from services.api.app.evaluation.models import ModelCall
from services.api.app.evaluation.capture import current_capture, current_provenance
from services.api.app.evaluation.delivery_models import PayloadSlot
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

    async def chat_completion_result(self, messages: List[Dict], temperature: float = 0.7, json_mode: bool = False, max_tokens: int = 1024) -> LLMResult:
        started = time.perf_counter()
        collector = current_collector.get()
        capture = current_capture.get()
        ordinal = provenance = None
        if capture is not None:
            provenance = current_provenance.get()
            if provenance is None:
                provenance = capture.messages_provenance(messages)
            ordinal = capture.begin_model(provenance)
            capture.record(PayloadSlot(kind='model_request', ordinal=ordinal), {
                'messages': messages, 'model': settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
                'temperature': temperature, 'max_tokens': max_tokens}, provenance)
            if not capture.has_payload(PayloadSlot(kind='model_request', ordinal=ordinal)):
                provenance = capture.unknown()
        result = failure = None
        with domain_span("llm.chat", attributes={
            "gen_ai.request.model": settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
            "gen_ai.request.temperature": temperature, "retry.count": 0,
        }) as span:
            try:
                result = await self._completion(messages, temperature, max_tokens, span)
                if capture is not None:
                    capture.record(PayloadSlot(kind='model_response', ordinal=ordinal), {'text': result.text}, provenance)
                    capture.remember(result.text, provenance)
                return result
            except BaseException as exc:
                failure = exc
                raise
            finally:
                if collector is not None:
                    metadata_index = collector.record_model_call(ModelCall(
                        purpose=model_purpose.get(), request_hash=content_hash({"messages": messages, "temperature": temperature, "max_tokens": max_tokens}),
                        requested_model=settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
                        **(result.model_dump(exclude={"text"}) if result is not None else {}),
                        status=failure_status(failure) if failure is not None else "success",
                        error_type=type(failure).__name__ if failure is not None else None,
                        retry_count=span.attributes.get("retry.count", 0), latency_ms=(time.perf_counter() - started) * 1000,
                    ))
                    if capture is not None:
                        capture.finish_model(ordinal, metadata_index)
                if capture is not None and result is None:
                    capture.omit(PayloadSlot(kind='model_response', ordinal=ordinal), 'not_observed')

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
