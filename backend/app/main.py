"""
FastAPI application factory — the API process entry point.

Mounts all four routers under /api/v1 (ingest, query, analysis, graph — the
analysis router existed before but was never mounted, so it was dead code)
and installs structured JSON logging at boot.

Run (dev):      uvicorn app.main:app --reload      (from backend/)
Run (prod):     gunicorn -k uvicorn.workers.UvicornWorker app.main:app
Worker process: python -m app.workers.ingestion_worker   (separate terminal)
"""

import logging

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.core.config import settings
from app.core.logging import configure_logging

configure_logging(level=logging.DEBUG if settings.DEBUG else logging.INFO,
                  json_logs=settings.LOG_JSON)
logger = logging.getLogger("copilot_platform")

# Routers import services; import AFTER logging so their boot logs are structured.
from app.api.analysis import router as analysis_router   # noqa: E402
from app.api.graph import router as graph_router         # noqa: E402
from app.api.ingest import router as ingest_router       # noqa: E402
from app.api.query import router as query_router         # noqa: E402


def create_app() -> FastAPI:
    """Factory function to configure and return the FastAPI application instance."""
    app = FastAPI(
        title=settings.APP_NAME,
        description="Hybrid multi-source GraphRAG: code knowledge graph + semantic "
                    "retrieval + curated knowledge (OKF) + LLM reasoning.",
        version=settings.APP_VERSION,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # tighten to real domains in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(ingest_router, prefix=settings.API_V1_STR)
    app.include_router(query_router, prefix=settings.API_V1_STR)
    app.include_router(analysis_router, prefix=settings.API_V1_STR)
    app.include_router(graph_router, prefix=settings.API_V1_STR)

    @app.get("/health", tags=["System"])
    async def health_check():
        """Liveness probe for container orchestration (Docker/K8s)."""
        return {"status": "healthy", "service": "copilot-core-api",
                "version": settings.APP_VERSION}

    return app


app = create_app()

if __name__ == "__main__":
    logger.info("Starting up Distributed AI Engineering Copilot Platform...")
    uvicorn.run("app.main:app", host=settings.HOST, port=settings.PORT, reload=True)
