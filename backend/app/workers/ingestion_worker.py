import json
import time
import logging
import redis
from typing import Dict, Any

from app.services.ingestion import ingest_repository
from app.core.config import settings

logging.basicConfig(
    level=logging.DEBUG if settings.DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] WORKER: %(message)s"
)
logger = logging.getLogger("ingestion_worker")

class DistributedIngestionWorker:
    """
    Production-grade Python worker that bridges to the Node.js/Redis job queue.
    Listens for repository ingestion payloads, processes them, and reports state.
    """
    def __init__(self):
        try:
            self.redis_client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            self.redis_client.ping()
            logger.info(f"Successfully connected to Redis at {settings.REDIS_URL}")
        except redis.ConnectionError as e:
            logger.critical(f"Failed to connect to Redis. Worker cannot start. Error: {e}")
            raise

    def process_job(self, job_data: Dict[str, Any]):
        """Executes the ingestion pipeline and handles state reporting."""
        job_id = job_data.get("job_id", "unknown_job")
        repo_id = job_data.get("repo_id")
        repo_url = job_data.get("repo_url")

        if not repo_id or not repo_url:
            logger.error(f"Job {job_id} missing required fields (repo_id, repo_url). Dropping.")
            self._report_status(job_id, "failed", error="Missing repo_id or repo_url in payload")
            return

        logger.info(f"[{job_id}] Starting processing for {repo_id} ({repo_url})")
        self._report_status(job_id, "processing")

        # Execute our synchronous orchestrator
        result = ingest_repository(repo_id=repo_id, repo_url=repo_url)

        if result.get("status") == "success":
            logger.info(f"[{job_id}] Successfully ingested {repo_id}")
            self._report_status(job_id, "completed", result=result)
        else:
            error_msg = result.get("error", "Unknown ingestion error")
            logger.error(f"[{job_id}] Failed to ingest {repo_id}: {error_msg}")
            self._report_status(job_id, "failed", error=error_msg)

    def _report_status(self, job_id: str, status: str, result: Dict = None, error: str = None):
        """Pushes job state updates back to Redis."""
        payload = {
            "job_id": job_id,
            "status": status,
            "timestamp": time.time()
        }
        if result:
            payload["result"] = result
        if error:
            payload["error"] = error

        try:
            self.redis_client.rpush(settings.REDIS_RESULTS_QUEUE, json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to report status for {job_id}: {e}")

    def start_listening(self):
        """Blocking loop that acts as the consumer for the Redis queue."""
        logger.info(f"Worker started. Listening on Redis queue: '{settings.REDIS_INGEST_QUEUE}'...")
        
        while True:
            try:
                queue_name, raw_data = self.redis_client.blpop(settings.REDIS_INGEST_QUEUE, timeout=0)
                job_data = json.loads(raw_data)
                logger.debug(f"Received raw job payload: {job_data}")
                
                self.process_job(job_data)
                
            except json.JSONDecodeError:
                logger.error(f"Received malformed JSON payload from {settings.REDIS_INGEST_QUEUE}")
            except redis.ConnectionError as e:
                logger.error(f"Redis connection lost. Retrying in 5 seconds... ({e})")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected worker error: {e}", exc_info=True)
                time.sleep(1)

if __name__ == "__main__":
    worker = DistributedIngestionWorker()
    worker.start_listening()