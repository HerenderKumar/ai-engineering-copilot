import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    """
    Core application settings managed via environment variables (12-factor).
    Pydantic validates types on startup; a `.env` file overrides defaults.
    Every tunable of the pipeline lives here so behaviour is config, not code.
    """

    # Application
    APP_NAME: str = "Distributed AI Engineering Copilot"
    APP_VERSION: str = "2.0.0"
    ENVIRONMENT: str = Field("development", description="Set to 'production' in prod")
    DEBUG: bool = Field(False, description="Enable debug logging")
    LOG_JSON: bool = Field(True, description="Structured JSON logs (Phase 0). False = human-readable.")

    # API layer
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    API_V1_STR: str = "/api/v1"

    # AI & reasoning (Gemini today — pluggable by design, see services/llm/)
    # Optional at startup so ingestion/retrieval/graph work without a key;
    # only the answer-generation step requires it.
    GEMINI_API_KEY: SecretStr = Field(SecretStr(""), description="Google Gemini API key")
    GEMINI_MODEL: str = Field("gemini-2.5-flash", description="Target Gemini model")

    # Local-LLM fallback (Ollama). When enabled, any Gemini failure —
    # missing/invalid key, network black-hole, rate limit — falls back to a
    # local `ollama serve` instance, so chat works fully offline.
    OLLAMA_ENABLED: bool = Field(True, description="Fall back to Ollama when the primary LLM fails")
    OLLAMA_BASE_URL: str = Field("http://localhost:11434", description="Ollama server (compose overrides with host.docker.internal)")
    OLLAMA_MODEL: str = Field("qwen2.5", description="Any locally pulled tag from `ollama list`")
    OLLAMA_TIMEOUT_S: float = Field(180.0, description="Read timeout — local models cold-load weights and stream slowly")
    OLLAMA_NUM_CTX: int = Field(8192, description="Context window — Ollama's 4096 default truncates RAG prompts")
    OLLAMA_KEEP_ALIVE: str = Field("30m", description="Keep the model loaded between queries (skip reload latency)")

    # Embeddings (Phase 1: code-trained model + versioning)
    # Locked decision: BGE-M3 (code-trained, 1024-dim) as self-hosted default.
    # Lightweight fallback for laptops: EMBEDDING_MODEL_NAME=all-MiniLM-L6-v2, EMBEDDING_DIM=384.
    EMBEDDING_MODEL_NAME: str = Field("BAAI/bge-m3")
    EMBEDDING_DIM: int = Field(1024, description="Must match the model's output dimension")
    EMBEDDING_NORMALIZE: bool = Field(True, description="L2-normalize → cosine similarity via inner product")
    DATA_DIR: str = Field("data", description="Base dir for FAISS indices, SQLite metadata, graph DB")
    REPOS_DIR: str = Field("data/repos", description="Persistent working copies of ingested repos (analysis + OKF need the source)")

    # Distributed queue (Redis)
    REDIS_URL: str = Field("redis://localhost:6379/0")
    REDIS_INGEST_QUEUE: str = Field("repo_ingestion_queue")
    REDIS_RESULTS_QUEUE: str = Field("repo_ingestion_results")

    # Caching (Phase 1) — degrades gracefully to in-process memory if Redis is down
    CACHE_ENABLED: bool = Field(True)
    EMBEDDING_CACHE_TTL: int = Field(7 * 24 * 3600, description="content-hash → vector")
    QUERY_CACHE_TTL: int = Field(300, description="normalized query → results")

    # RAG pipeline tuning
    MAX_CHUNK_SIZE: int = 1500
    CHUNK_OVERLAP: int = 200
    DEFAULT_TOP_K: int = 10
    RRF_K: int = Field(60, description="RRF smoothing constant (standard value from the literature)")
    QUERY_REWRITE_ENABLED: bool = Field(True, description="Expand query into 2-3 sub-queries")
    RERANK_CANDIDATE_CAP: int = Field(50, description="Max candidates sent to the cross-encoder")
    RERANKER_MODEL_NAME: str = Field("cross-encoder/ms-marco-MiniLM-L-6-v2")

    # Knowledge graph (Phase 2/3)
    GRAPH_ENABLED: bool = Field(True)
    GRAPH_EXPANSION_ENABLED: bool = Field(True, description="Pull graph neighbors of top hits into candidates")
    GRAPH_EXPANSION_HOPS: int = Field(1, description="1-2 hops; more = noisier context")
    GRAPH_MAX_NEIGHBORS: int = Field(10, description="Cap neighbors per seed chunk")
    GRAPH_LAYOUT_ON_INGEST: bool = Field(True, description="Precompute 3D layout + Louvain at index time")

    # OKF curated-knowledge layer (Phase 3)
    OKF_ON_INGEST: bool = Field(True, description="Emit + embed the .knowledge/ bundle after ingestion")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )


def get_settings() -> Settings:
    """Instantiates and validates the settings payload."""
    try:
        return Settings()
    except Exception as e:
        logger.critical(f"Configuration validation failed: {e}")
        raise RuntimeError(f"Application failed to start due to invalid configuration: {e}")


settings = get_settings()  # Singleton imported across the app
