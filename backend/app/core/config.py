import os
import logging
from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, SecretStr
from typing import Optional

logger = logging.getLogger(__name__)

class Settings(BaseSettings):
    """
    Core application settings managed via environment variables.
    Pydantic automatically validates types and required fields on startup.
    """
    # Application Config
    APP_NAME: str = "Distributed AI Engineering Copilot"
    APP_VERSION: str = "1.0.0"
    ENVIRONMENT: str = Field("development", description="Set to 'production' in prod")
    DEBUG: bool = Field(False, description="Enable debug logging")

    # API Layer
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    API_V1_STR: str = "/api/v1"

    # AI & Reasoning (Gemini)
    GEMINI_API_KEY: SecretStr = Field(
        ..., 
        description="Google Gemini API Key. Mandatory for reasoning layer."
    )
    GEMINI_MODEL: str = Field(
        "gemini-2.5-flash", 
        description="The target Gemini model for RAG."
    )

    # Embedding & Vector Store (Local MiniLM & FAISS/SQLite)
    EMBEDDING_MODEL_NAME: str = Field("all-MiniLM-L6-v2")
    EMBEDDING_DIM: int = Field(384, description="Dimension size for MiniLM")
    DATA_DIR: str = Field(
        "data", 
        description="Base directory for FAISS indices and SQLite metadata"
    )

    # Distributed Queue (Redis / BullMQ)
    REDIS_URL: str = Field("redis://localhost:6379/0", description="Redis connection URL")
    REDIS_INGEST_QUEUE: str = Field("repo_ingestion_queue", description="BullMQ ingest queue name")
    REDIS_RESULTS_QUEUE: str = Field("repo_ingestion_results", description="BullMQ results queue name")

    # RAG Pipeline Tuning
    MAX_CHUNK_SIZE: int = 1500
    CHUNK_OVERLAP: int = 200
    DEFAULT_TOP_K: int = 10

    # Pydantic V2 syntax for loading from a local .env file
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

def get_settings() -> Settings:
    """Instantiates and validates the settings payload."""
    try:
        settings = Settings()
        return settings
    except Exception as e:
        logger.critical(f"Configuration validation failed: {e}")
        raise RuntimeError(f"Application failed to start due to invalid configuration: {e}")


settings = get_settings() # Exposes a singleton instance to be imported across the app
