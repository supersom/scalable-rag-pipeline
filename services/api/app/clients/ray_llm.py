# services/api/app/clients/ray_llm.py
import httpx
import logging
import backoff
from typing import List, Dict, Optional
from services.api.app.config import settings

logger = logging.getLogger(__name__)

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
    )
    async def chat_completion(self, messages: List[Dict], temperature: float = 0.7, json_mode: bool = False, max_tokens: int = 1024) -> str:
        if not self.client:
            raise RuntimeError("Client not initialized. Call start() first.")

        payload = {
            "model": settings.LLM_MODEL_ID or settings.LLM_MODEL_NAME,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        response = await self.client.post(self.endpoint, json=payload)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"LLM provider error: {body['error']}")
        return body["choices"][0]["message"]["content"]

# Global Instance (Managed by Lifespan in main.py)
llm_client = RayLLMClient()