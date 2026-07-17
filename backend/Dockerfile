# syntax=docker/dockerfile:1
#
# Backend image — ONE image, run as two services:
#   - API    : uvicorn app.main:app        (default CMD)
#   - worker : python -m app.workers.ingestion_worker   (command override in compose)
#
# Build context is the REPO ROOT (requirements.txt lives there; app code in backend/).
#   docker build -f backend/Dockerfile -t ai-copilot-backend .

FROM python:3.11-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HF_HOME=/opt/hf-cache

# System deps:
#   git             — ingestion shells out to `git clone`
#   ca-certificates — HTTPS clones + model/API downloads
RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# --- Python deps (own layer for cache reuse) ---
# Install CPU-only torch from the PyTorch CPU index first so we don't pull the
# ~2 GB CUDA build we can't use. The pinned torch>=2.2 in requirements is then
# already satisfied. (If this line ever fails on your arch, delete it — the
# requirements install below will fetch torch from PyPI instead.)
COPY requirements.txt ./
RUN pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# --- App code ---
COPY backend/ ./backend/

# Non-root user. Pre-create + own the data/model dirs so the *named volumes*
# mounted there inherit writable ownership (Docker seeds an empty named volume
# from the image dir's perms).
RUN useradd -m -u 10001 appuser \
    && mkdir -p /app/backend/data /opt/hf-cache \
    && chown -R appuser:appuser /app /opt/hf-cache
USER appuser

# app.main:app is imported from here (so `app` package resolves)
WORKDIR /app/backend
EXPOSE 8000

# Liveness for compose/K8s (no curl in slim → use urllib)
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')" || exit 1

# API process. The worker service overrides this in docker-compose.yml.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
