# services/api/main.py
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI

import services.api.app.logging  # noqa: F401 — activates JSON log handler
from services.api.app.cache.semantic import semantic_cache
from services.api.app.clients.neo4j import neo4j_client
from services.api.app.clients.ray_embed import embed_client
from services.api.app.clients.ray_llm import llm_client
from services.api.app.config import settings
from services.api.app.evaluation.delivery_config import DeliveryConfig
from services.api.app.evaluation.operation_schemas import register_api_operation_schemas
from services.api.app.evaluation.runtime import install_runtime, start_evidence_runtime
from services.api.app.memory.postgres import Base, engine
from services.api.app.routes import chat, health, upload
from services.api.app.observability import setup_observability, shutdown_observability


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Centralized Resource Management.
    Initialize all connection pools here.
    """
    evidence = None
    try:
        register_api_operation_schemas()
        print("Initializing clients...")
        neo4j_client.connect()
        await llm_client.start()
        await embed_client.start()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        await semantic_cache.ensure_collection()
        # None when disabled (the default). Evidence migrations are applied out of band, not here.
        evidence = await start_evidence_runtime(DeliveryConfig.from_settings(settings), settings)
        install_runtime(evidence)
        yield
    finally:
        print("Closing clients...")
        install_runtime(None)
        try:
            if evidence is not None:
                await evidence.close(time.monotonic() + evidence.config.shutdown_drain_seconds)
        finally:
            try:
                await neo4j_client.close()
                await llm_client.close()
                await embed_client.close()
            finally:
                await shutdown_observability(app)

# FastAPI Application
app = FastAPI(title="Enterprise RAG Platform", version="1.0.0", lifespan=lifespan)
setup_observability(app)

# Include Routes
app.include_router(chat.router, prefix="/api/v1/chat", tags=["Chat"])
app.include_router(upload.router, prefix="/api/v1/upload", tags=["Upload"])
app.include_router(health.router, prefix="/health", tags=["Health"])

if __name__ == "__main__":
    import uvicorn
    # In production, this is run via Gunicorn/Uvicorn in Docker
    uvicorn.run(app, host="0.0.0.0", port=8000)
