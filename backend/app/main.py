import logging
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

# Import our modular routers
from app.api.ingest import router as ingest_router
from app.api.query import router as query_router

# Configure global logging for the application
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()]
)

logger = logging.getLogger("copilot_platform")

def create_app() -> FastAPI:
    """Factory function to configure and return the FastAPI application instance."""
    app = FastAPI(
        title="Distributed AI Engineering Copilot",
        description="Production API for repository ingestion, RAG, and AI code reasoning.",
        version="1.0.0",
    )

    # Configure CORS - allow specific origins in production
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # Update this to specific domains/IPs in production
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount API Routers
    app.include_router(ingest_router, prefix="/api/v1")
    app.include_router(query_router, prefix="/api/v1")

    @app.get("/health", tags=["System"])
    async def health_check():
        """Basic health check for container orchestration (Docker/K8s)."""
        return {"status": "healthy", "service": "copilot-core-api"}

    return app

app = create_app()

if __name__ == "__main__":
    logger.info("Starting up Distributed AI Engineering Copilot Platform...")
    # Run via uvicorn for local development; in prod, use gunicorn with uvicorn workers
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)