"""
Ingestion API — accepts jobs, hands them to the distributed queue.

Pattern: the API process NEVER ingests inline (cloning + embedding can take
minutes; HTTP handlers must return in milliseconds). It validates, enqueues
a JSON payload onto a Redis list, and returns 202 Accepted with a job_id.
The worker process (app/workers/ingestion_worker.py) consumes the queue.

New: GET /ingest/status/{job_id} — the worker mirrors job state into a Redis
hash so the frontend can poll progress instead of guessing.
"""

import json
import logging
import os
import uuid

import redis
from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, field_validator

from app.core.config import settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ingest", tags=["Ingestion Engine"])

redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)

STATUS_KEY = "ingest:status:{job_id}"  # worker writes, API reads
STATUS_TTL = 24 * 3600


class IngestRequest(BaseModel):
    repo_id: str = Field(..., description="A unique namespace identifier for the repository.")
    repo_url: str = Field(..., description="Git clone URL (http/https/ssh) OR an existing local folder path.")

    @field_validator("repo_url")
    @classmethod
    def _valid_source(cls, v: str) -> str:
        """Accept a git URL or a local directory (works fully offline)."""
        v = v.strip()
        if v.startswith(("http://", "https://", "git@", "ssh://")):
            return v
        local = os.path.abspath(os.path.expanduser(v))
        if os.path.isdir(local):
            return local
        raise ValueError(
            "repo_url must be an http(s)/ssh git URL or an existing local folder path")


class IngestResponse(BaseModel):
    message: str
    job_id: str
    repo_id: str
    status: str


@router.post("/", response_model=IngestResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_ingestion(request: IngestRequest):
    """Pushes an ingestion job to the distributed Redis queue."""
    job_id = f"job-{uuid.uuid4()}"
    payload = {
        "job_id": job_id,
        "repo_id": request.repo_id,
        "repo_url": str(request.repo_url),
        "task_type": "vector_ingestion",
    }
    try:
        redis_client.rpush(settings.REDIS_INGEST_QUEUE, json.dumps(payload))
        redis_client.setex(STATUS_KEY.format(job_id=job_id), STATUS_TTL,
                           json.dumps({"status": "queued", "repo_id": request.repo_id}))
        logger.info(f"Queued job {job_id} for repo {request.repo_id}")
        return IngestResponse(
            message="Ingestion job submitted to distributed queue.",
            job_id=job_id, repo_id=request.repo_id, status="queued")
    except redis.ConnectionError:
        logger.error("Redis connection failed during job submission.")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            "Job queue is temporarily unavailable.")
    except Exception as e:
        logger.error(f"Failed to queue ingestion job: {e}")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR,
                            "Failed to enqueue repository ingestion.")


@router.get("/status/{job_id}")
async def job_status(job_id: str):
    """Poll job state: queued → processing → completed | failed."""
    try:
        raw = redis_client.get(STATUS_KEY.format(job_id=job_id))
    except redis.ConnectionError:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Status store unavailable.")
    if raw is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Unknown or expired job id.")
    return json.loads(raw)
