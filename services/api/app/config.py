# services/api/app/config.py
from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Literal
from typing import Optional

class Settings(BaseSettings):
    """
    Application Configuration.
    Reads environment variables automatically (case-insensitive).
    """
    # General
    ENV: str = "prod"
    LOG_LEVEL: str = "INFO"

    # OTLP/gRPC when an endpoint is set; console export is opt-in.
    OTEL_SERVICE_NAME: str = "rag-api-service"
    OTEL_SERVICE_VERSION: str = "unknown"
    OTEL_EXPORTER_OTLP_ENDPOINT: Optional[str] = None
    OTEL_CONSOLE_EXPORT_ENABLED: bool = False
    OTEL_TRACES_SAMPLER: Literal[
        "always_on", "always_off", "traceidratio", "parentbased_always_on",
        "parentbased_always_off", "parentbased_traceidratio",
    ] = "parentbased_always_on"
    OTEL_TRACES_SAMPLER_ARG: float = Field(default=1.0, ge=0.0, le=1.0)
    OTEL_EXPORTER_OTLP_TIMEOUT: float = Field(default=5.0, gt=0.0)
    # Optional separate key for tenant-scoped identity pseudonyms. No raw user ID
    # enters evidence when unset; durable storage/publication belongs to PR 4.
    EVAL_IDENTITY_HASH_KEY: Optional[str] = None
    AGENT_BUILD_SHA: Optional[str] = None  # supplied by build/deployment, never inferred from the working tree
    
    # Database (Aurora Postgres)
    DATABASE_URL: str  # e.g., postgresql+asyncpg://user:pass@host:5432/db
    
    # Redis (Cache)
    REDIS_URL: str     # e.g., redis://elasticache-endpoint:6379/0
    
    # Vector DB (Qdrant)
    QDRANT_HOST: str = "qdrant-service"
    QDRANT_PORT: int = 6333
    QDRANT_COLLECTION: str = "rag_collection"
    
    # Graph DB (Neo4j)
    NEO4J_URI: str = "bolt://neo4j-cluster:7687"
    NEO4J_USER: str = "neo4j"
    NEO4J_PASSWORD: str # Sensitive
    
    # AWS S3 (Documents)
    AWS_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str
    
    # Ray Serve / Ollama (Internal LLM/Embeddings) / External LLM API
    RAY_LLM_ENDPOINT: str = "http://llm-service:8000/llm/chat/completions"
    RAY_EMBED_ENDPOINT: str = "http://embed-service:8000/embed/embeddings"
    LLM_MODEL_NAME: str = "llama3.2:3b"
    LLM_MODEL_ID: Optional[str] = None
    LLM_API_KEY: Optional[str] = None  # set for HuggingFace, Together AI, etc.
    LLM_TIMEOUT: float = 120.0
    EMBED_MODEL_NAME: str = "nomic-embed-text"
    EMBED_DIM: int = 1024  # BGE-M3 output dimension; set 768 for local nomic-embed-text
    
    # Security
    JWT_SECRET_KEY: str
    JWT_ALGORITHM: str = "HS256"

    class Config:
        env_file = ".env"
        extra = "ignore"

# Instantiate singleton
settings = Settings()
