# Backend Core — `config.py`, `logging.py`, `main.py`

These three files are the skeleton every other file hangs on. Build them first when rebuilding.

---

## `app/core/config.py` — every knob in one place

**What it is.** A single `Settings` class (pydantic-settings) holding every tunable: model names, dimensions, Redis URL, chunk sizes, feature flags. Values come from environment variables / a `.env` file — code never hardcodes behavior (the "12-factor" rule that makes the app container-ready).

**How it works, line by line in spirit:**

- Each field has a type and default: `RRF_K: int = Field(60, ...)`. Pydantic validates types at startup — a typo in `.env` fails fast with a clear error instead of exploding mid-request.
- `GEMINI_API_KEY: SecretStr = Field(SecretStr(""))` — a `SecretStr` never prints its value in logs/tracebacks. It defaults to *empty* on purpose: only answer generation needs the key; ingestion/graph/eval must run without it.
- `model_config = SettingsConfigDict(env_file=".env", ...)` — tells pydantic where to read overrides.
- Bottom: `settings = get_settings()` builds ONE instance at import time; every module does `from app.core.config import settings`.

**Key fields to understand:**

| Field | Why it exists |
|---|---|
| `EMBEDDING_MODEL_NAME` / `EMBEDDING_DIM` | The versioned embedder (change → auto re-index) |
| `EMBEDDING_NORMALIZE` | normalized vectors → cosine similarity via inner product |
| `REPOS_DIR` | ingestion persists a working copy here so analysis/OKF can read source later |
| `RRF_K` | the RRF smoothing constant (60 = literature standard) |
| `QUERY_REWRITE_ENABLED`, `GRAPH_EXPANSION_ENABLED`, `GRAPH_EXPANSION_HOPS`, `OKF_ON_INGEST` | feature flags — every pipeline stage can be switched off in config, which is also how you A/B test stages in the eval |
| `RERANK_CANDIDATE_CAP` | latency guard: the cross-encoder sees at most this many candidates |

**Rebuild-it-yourself steps:** create the class with app/API/LLM/embedding/Redis/cache/RAG/graph/OKF sections → add `SettingsConfigDict` → module-level singleton → `.env.example` documenting each var.

---

## `app/core/logging.py` — structured JSON logs + stage timers

**The problem it solves.** `print("done")` tells a dashboard nothing. Infrastructure needs logs it can *query*: which repo? which job? how long did the dense stage take? how many candidates survived fusion?

**The three exports:**

1. `configure_logging()` — installs ONE stdout handler on the root logger with a `JsonFormatter`. Idempotent (safe to call in API, worker, tests). Containers expect stdout; the platform ships the logs.
2. `log_event(logger, "ingest.done", repo_id=..., chunks=42)` — one JSON object per event. The formatter merges any keyword fields into the payload by reading non-reserved attributes off the `LogRecord` (that's what the `_RESERVED` set is for).
3. `stage_timer(logger, "retrieval.dense", repo_id=...)` — a context manager that measures wall time and logs `{"message": "retrieval.dense", "duration_ms": 41.3, "ok": true, ...}` on exit, even on exceptions. It yields a dict — anything you stuff into it gets logged too (candidate counts, cache hits). These lines are the raw material for p50/p95 dashboards.

**Example output line:**
```json
{"ts": "2026-07-06T10:12:03+00:00", "level": "INFO", "logger": "app.services.retrieval",
 "message": "retrieval.done", "repo_id": "flask", "candidates": 38, "returned": 10,
 "fallbacks": null}
```

**Rebuild steps:** write `JsonFormatter.format()` (timestamp/level/logger/message + extra fields + exception text) → `configure_logging()` that swaps root handlers → `log_event` helper → `stage_timer` contextmanager with `time.perf_counter()`.

---

## `app/main.py` — the application factory

**What it does.** Builds the FastAPI app: configures logging FIRST (so every import logs structured), adds CORS middleware (the browser frontend runs on a different port — without CORS the browser blocks the calls), mounts the four routers under `/api/v1`, and exposes `GET /health` for container orchestration liveness probes.

**The one subtle ordering rule:** `configure_logging()` runs *before* the router imports, because importing routers imports services, and their startup log lines should already be JSON.

**The bug that was fixed here:** the analysis router existed but was never mounted — a whole feature that was unreachable. The graph router is new (Phase 3).

**Rebuild steps:** `create_app()` factory → CORS middleware → `include_router` × 4 with `prefix=settings.API_V1_STR` → `/health` route → module-level `app = create_app()` (what uvicorn imports) → `if __name__ == "__main__"` uvicorn runner.

---

## `app/schemas.py` — a note

Legacy file with two unused models (`IngestRequest` with different fields than the real one, `QueryRequest`). Each router defines its own request/response models next to its endpoints, which is the pattern to follow. Kept only to avoid breaking stray imports; don't extend it.
