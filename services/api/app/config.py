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
    EVAL_EVIDENCE_ENABLED: bool = False
    EVAL_CAPTURE_CONTENT: bool = False
    EVAL_QUEUE_URL: Optional[str] = None
    EVAL_CONTENT_POLICY: Optional[str] = None
    EVAL_FIXTURE_MANIFEST: Optional[str] = None
    EVAL_S3_BUCKET: Optional[str] = None
    EVAL_KMS_KEY_ID: Optional[str] = None
    EVAL_S3_ENDPOINT_URL: Optional[str] = None
    EVAL_PAYLOAD_BYTES: int = 65536
    EVAL_RUN_BYTES: int = 1048576
    EVAL_PAYLOAD_SLOTS: int = 64
    EVAL_ACTIVE_RUNS: int = 32
    EVAL_ACTIVE_BYTES: int = 16777216
    EVAL_PENDING_RUNS: int = 32
    EVAL_PENDING_BYTES: int = 16777216
    EVAL_RUNTIME_WORKERS: int = 2
    EVAL_S3_WORKERS: int = 2
    EVAL_DEADLINE_SECONDS: float = 20
    EVAL_COMMIT_RESERVE_SECONDS: float = 3
    EVAL_SDK_CONNECT_TIMEOUT_SECONDS: float = 1
    EVAL_SDK_READ_TIMEOUT_SECONDS: float = 2
    EVAL_SDK_ATTEMPTS: int = 2
    EVAL_QUEUE_BODY_BYTES: int = 131072
    EVAL_DELIVERY_BATCH: int = 10
    EVAL_SEND_WORKERS: int = 2
    EVAL_CLAIM_LEASE_SECONDS: float = 60
    EVAL_PRODUCER_ATTEMPTS: int = 8
    EVAL_BACKOFF_BASE_SECONDS: float = 1
    EVAL_BACKOFF_CAP_SECONDS: float = 60
    EVAL_SHUTDOWN_DRAIN_SECONDS: float = 10
    EVAL_INTENT_LEASE_SECONDS: float = 60
    EVAL_ORPHAN_GRACE_SECONDS: float = 86400
    EVAL_CONTENT_RETENTION_DAYS: int = 30
    EVAL_METADATA_RETENTION_DAYS: int = 90
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
