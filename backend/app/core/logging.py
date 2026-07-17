"""
Structured JSON logging for the whole platform (Phase 0).

Why JSON logs?
--------------
Plain text logs ("Ingested repo foo") are fine for humans but useless for
machines. Structured logs are dictionaries — every line carries queryable
fields (repo_id, job_id, stage, duration_ms, candidate counts) so an
observability stack (Datadog / Loki / OpenSearch / OpenTelemetry collector)
can filter, aggregate and alert on them without regex parsing.

Usage:
    from app.core.logging import configure_logging, log_event, stage_timer

    configure_logging()                       # once, at process start
    logger = logging.getLogger(__name__)

    log_event(logger, "ingest.start", repo_id="repo_x", job_id="job-1")

    with stage_timer(logger, "retrieval.dense", repo_id="repo_x") as ctx:
        results = do_search()
        ctx["candidates"] = len(results)      # extra fields added to the exit log
"""

import json
import logging
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict

# Attributes that exist on every LogRecord — everything else was passed via
# `extra=` and should be emitted as a structured field.
_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "taskName", "asctime",
}


class JsonFormatter(logging.Formatter):
    """Formats every log record as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: Dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        # Merge structured fields passed through `extra=`.
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: int = logging.INFO, json_logs: bool = True) -> None:
    """
    Install a single stdout handler on the root logger. Idempotent — safe to
    call from the API process, the worker, tests and CLI scripts.
    Containers expect logs on stdout; the platform (Docker/K8s) ships them.
    """
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.StreamHandler(sys.stdout)
    if json_logs:
        handler.setFormatter(JsonFormatter())
    else:  # human-readable fallback for local debugging
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
    root.addHandler(handler)
    # Quiet noisy third-party loggers; our own events stay at INFO.
    for noisy in ("urllib3", "httpx", "sentence_transformers", "faiss"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    """Emit one structured event, e.g. log_event(log, 'ingest.done', repo_id=r, chunks=42)."""
    logger.log(level, event, extra=fields)


@contextmanager
def stage_timer(logger: logging.Logger, stage: str, **fields: Any):
    """
    Context manager that logs `<stage>` with duration_ms on exit — the
    building block for per-stage latency metrics (p50/p95 in your APM).
    Yields a dict; anything you put in it is added to the exit log line.
    """
    ctx: Dict[str, Any] = {}
    started = time.perf_counter()
    try:
        yield ctx
        ok = True
    except Exception:
        ok = False
        raise
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        log_event(logger, stage, duration_ms=duration_ms, ok=ok, **fields, **ctx)
