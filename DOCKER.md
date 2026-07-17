# Running with Docker

The whole stack — API, ingestion worker, Redis, and the frontend — starts with one command.

## Prerequisites

- Docker Desktop (or Docker Engine) with the Compose plugin (`docker compose version`).
- ~5 GB free disk for images + the BGE-M3 embedder that downloads on first ingest.

## Quick start

```bash
# 1. Configure environment (chat needs a Gemini key; everything else runs without one)
cp backend/.env.example backend/.env
#    then edit backend/.env → set GEMINI_API_KEY (optional but needed for the Chat tab)

# 2. Build and start everything
docker compose up --build
```

Then open:

| Service  | URL                         | Notes                                    |
|----------|-----------------------------|------------------------------------------|
| Frontend | http://localhost:5173       | React app (Chat / Analysis / 3D Graph)   |
| API docs | http://localhost:8000/docs  | Interactive Swagger                      |
| Health   | http://localhost:8000/health| Liveness probe                           |

Redis is internal to the compose network (no host port unless you uncomment it).

> **Do not** override `REDIS_URL` in `backend/.env` — compose sets it to `redis://redis:6379/0` for the container network automatically.

## What runs where

- **backend** and **worker** are the *same image* (built once). The API serves requests; the worker consumes the ingestion queue. They share the `copilot-data` and `hf-cache` volumes.
- **frontend** is the production Vite build served by nginx. The browser calls the API directly at `localhost:8000` (published), so no proxy is needed locally.

## First ingestion

```bash
curl -X POST http://localhost:8000/api/v1/ingest/ \
  -H "Content-Type: application/json" \
  -d '{"repo_id":"my-first-repo","repo_url":"https://github.com/pallets/flask.git"}'
```

The **first** ingest downloads the embedder (~2.3 GB) into the `hf-cache` volume — several minutes, once. Re-ingesting an unchanged repo returns `"action":"noop"` in seconds.

Laptop-friendly alternative (smaller, lower quality) — set in `backend/.env` before starting:

```
EMBEDDING_MODEL_NAME=all-MiniLM-L6-v2
EMBEDDING_DIM=384
```

## Common operations

```bash
docker compose up --build -d          # start detached
docker compose logs -f worker         # follow ingestion logs (JSON lines)
docker compose up --scale worker=3    # more ingestion throughput
docker compose down                   # stop (keeps volumes/data)
docker compose down -v                # stop AND wipe all data (fresh reset)
```

## Data & persistence

All state lives in the `copilot-data` named volume, mirroring the app's layout:
`data/vectors/*.index`, `data/metadata/metadata.db`, `data/metadata/graph.db`,
`data/repos/<id>/`, `data/okf/<id>/.knowledge/`. It survives `down`; only
`down -v` removes it. Model weights persist separately in `hf-cache`.

## Notes / gotchas

- **Apple Silicon / ARM:** images build natively for arm64. Torch is pulled CPU-only.
- **CUDA/GPU** is intentionally not used (CPU torch keeps the image lean). For GPU, swap the torch install line in `backend/Dockerfile` for a CUDA wheel and add a GPU runtime.
- If the `torch --index-url .../cpu` line ever fails to resolve on your platform, delete that one line in `backend/Dockerfile`; the normal requirements install will fetch torch from PyPI.
- **Production single-origin:** to serve the app and API on one origin, point `frontend/src/api.js` at a relative `/api/v1` base and uncomment the proxy block in `frontend/nginx.conf`.
