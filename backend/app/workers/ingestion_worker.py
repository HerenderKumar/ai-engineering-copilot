"""
Ingestion worker — the async consumer half of the distributed queue.

Runs as its OWN process (`python -m app.workers.ingestion_worker`), separate
from the API: ingestion is CPU/IO heavy and must never block request
handling. Scale by running more worker processes — Redis BLPOP gives each
job to exactly one worker.

Job lifecycle it maintains (visible to the frontend via /ingest/status):
    queued → processing → completed | failed
State is mirrored to (a) a per-job Redis hash the API polls, and (b) the
results queue for any downstream consumer.

Phase-4 note (deliberately not built yet): bare BLPOP means a job dies with
a crashed worker. The upgrade path is Redis consumer groups + visibility
timeout + a dead-letter queue.
"""

import json
import logging
import time
from typing import Any, Dict

import redis

from app.core.config import settings
from app.core.logging import configure_logging, log_event

configure_logging(level=logging.DEBUG if settings.DEBUG else logging.INFO,
                  json_logs=settings.LOG_JSON)
logger = logging.getLogger("ingestion_worker")

STATUS_KEY = "ingest:status:{job_id}"
STATUS_TTL = 24 * 3600


class DistributedIngestionWorker:
    def __init__(self):
        try:
            self.redis_client = redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_keepalive=True,       # detect dead peers on a long idle BLPOP
                health_check_interval=30,    # periodic PING keeps the connection fresh
                retry_on_timeout=True,       # transparently retry a timed-out read
            )
            self.redis_client.ping()
            log_event(logger, "worker.connected", redis_url=settings.REDIS_URL)
        except redis.ConnectionError as e:
            logger.critical(f"Failed to connect to Redis. Worker cannot start. Error: {e}")
            raise

    def process_job(self, job_data: Dict[str, Any]):
        job_id = job_data.get("job_id", "unknown_job")
        repo_id = job_data.get("repo_id")
        repo_url = job_data.get("repo_url")

        if not repo_id or not repo_url:
            log_event(logger, "worker.job_invalid", level=logging.ERROR, job_id=job_id)
            self._report_status(job_id, "failed", error="Missing repo_id or repo_url in payload")
            return

        log_event(logger, "worker.job_start", job_id=job_id, repo_id=repo_id)
        self._report_status(job_id, "processing", repo_id=repo_id)

        # Import here so the worker fails per-job (with a reported status),
        # not at boot, if a heavy dependency is missing.
        from app.services.ingestion import ingest_repository
        result = ingest_repository(repo_id=repo_id, repo_url=repo_url, job_id=job_id)

        if result.get("status") == "success":
            log_event(logger, "worker.job_done", job_id=job_id, repo_id=repo_id,
                      action=result.get("action"))
            self._report_status(job_id, "completed", repo_id=repo_id, result=result)
        else:
            error_msg = result.get("error", "Unknown ingestion error")
            log_event(logger, "worker.job_failed", level=logging.ERROR,
                      job_id=job_id, repo_id=repo_id, error=error_msg)
            self._report_status(job_id, "failed", repo_id=repo_id, error=error_msg)

    def _report_status(self, job_id: str, status: str, repo_id: str = None,
                       result: Dict = None, error: str = None):
        """Mirror job state to the status key (API polling) + results queue."""
        payload: Dict[str, Any] = {"job_id": job_id, "status": status,
                                   "repo_id": repo_id, "timestamp": time.time()}
        if result:
            payload["result"] = result
        if error:
            payload["error"] = error
        try:
            self.redis_client.setex(STATUS_KEY.format(job_id=job_id),
                                    STATUS_TTL, json.dumps(payload))
            self.redis_client.rpush(settings.REDIS_RESULTS_QUEUE, json.dumps(payload))
        except Exception as e:
            logger.error(f"Failed to report status for {job_id}: {e}")

    def start_listening(self):
        log_event(logger, "worker.listening", queue=settings.REDIS_INGEST_QUEUE)
        while True:
            try:
                # Finite block: returns (queue, payload) when a job arrives, or
                # None when the window elapses with the queue still empty. A
                # finite timeout also lets the loop wake periodically for a
                # clean shutdown instead of blocking forever.
                item = self.redis_client.blpop(settings.REDIS_INGEST_QUEUE, timeout=5)
                if item is None:
                    continue
                _queue, raw_data = item
                self.process_job(json.loads(raw_data))
            except redis.exceptions.TimeoutError:
                # Idle blocking-read timeout on an empty queue — expected, NOT an
                # error. TimeoutError is a sibling of ConnectionError, so it used
                # to fall through to the generic handler and spam ERROR logs
                # every few seconds while the worker was simply waiting for work.
                continue
            except json.JSONDecodeError:
                log_event(logger, "worker.malformed_payload", level=logging.ERROR,
                          queue=settings.REDIS_INGEST_QUEUE)
            except redis.ConnectionError as e:
                logger.error(f"Redis connection lost. Retrying in 5 seconds... ({e})")
                time.sleep(5)
            except Exception as e:
                logger.error(f"Unexpected worker error: {e}", exc_info=True)
                time.sleep(1)


if __name__ == "__main__":
    DistributedIngestionWorker().start_listening()
