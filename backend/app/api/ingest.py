import os
import json
import uuid
import logging
import redis
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, HttpUrl, Field

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["Ingestion Engine"])

# Connect to the Redis instance managed by your Node system
redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
redis_client = redis.from_url(redis_url, decode_responses=True)
INGEST_QUEUE = os.getenv("REDIS_INGEST_QUEUE", "repo_ingestion_queue")

class IngestRequest(BaseModel):
    repo_id: str = Field(..., description="A unique namespace identifier for the repository.")
    repo_url: HttpUrl = Field(..., description="The Git clone URL of the repository.")

class IngestResponse(BaseModel):
    message: str
    job_id: str
    repo_id: str
    status: str

@router.post("/", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingestion(request: IngestRequest):
    """
    Pushes an ingestion job to the distributed Redis queue.
    """
    job_id = f"job-{uuid.uuid4()}"
    
    payload = {
        "job_id": job_id,
        "repo_id": request.repo_id,
        "repo_url": str(request.repo_url),
        "task_type": "vector_ingestion"
    }

    try:
        # Push standard JSON payload to Redis List
        redis_client.rpush(INGEST_QUEUE, json.dumps(payload))
        
        logger.info(f"Queued job {job_id} for repo {request.repo_id}")
        
        return IngestResponse(
            message="Ingestion job submitted to distributed queue.",
            job_id=job_id,
            repo_id=request.repo_id,
            status="queued"
        )

    except redis.ConnectionError:
        logger.error("Redis connection failed during job submission.")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, 
            detail="Job queue is temporarily unavailable."
        )
    except Exception as e:
        logger.error(f"Failed to queue ingestion job: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, 
            detail="Failed to enqueue repository ingestion."
        )