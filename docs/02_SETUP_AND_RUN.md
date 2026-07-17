# Setup and Run — From Clone to First Answer

## Prerequisites

- **Python 3.10+** (3.11 recommended)
- **Node 18+** (frontend)
- **Redis** — the job queue. Easiest: `docker run -d -p 6379:6379 redis:7`
- **Git** on PATH (ingestion shells out to `git clone`)
- A **Gemini API key** (free tier works) — only needed for chat answers; ingestion, graph, analysis and eval run without it

## 1. Backend

```bash
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then edit .env
```

Notes on heavy dependencies:

- `torch` + `sentence-transformers` are large (~2 GB). They load **lazily** — the API boots without touching them.
- The default embedder `BAAI/bge-m3` downloads ~2.3 GB on first use. On a laptop, set in `.env`:
  ```
  EMBEDDING_MODEL_NAME=all-MiniLM-L6-v2
  EMBEDDING_DIM=384
  ```
  (Quality drops; the locked production choice is BGE-M3. Switching later is safe — the model-versioning system detects the change and re-indexes automatically.)
- `tree-sitter` is pinned `==0.21.3` because `tree-sitter-languages` ships prebuilt grammar wheels compiled against that API.

## 2. Run the two backend processes

Terminal A — the API:
```bash
cd backend && source .venv/bin/activate
uvicorn app.main:app --reload
# → http://127.0.0.1:8000  (interactive docs at /docs)
```

Terminal B — the ingestion worker (this is what actually processes repos):
```bash
cd backend && source .venv/bin/activate
python -m app.workers.ingestion_worker
```

Both processes emit JSON log lines. `"message": "ingest.done"` with counts = success.

## 3. Frontend

```bash
cd frontend
npm install
npm run dev
# → http://localhost:5173
```

## 4. First ingestion (two ways)

**Via the UI:** paste a Git URL (start small, e.g. a repo with < 500 files), press *Index repo*, watch the status badge: `queued → processing → completed`. Tabs unlock when done.

**Offline / no GitHub access?** Paste a **local folder path** instead of a URL (e.g. `/Users/you/projects/my-app`) — the worker copies it directly, no git or network needed. The folder doesn't have to be a git repo.

**Via curl:**
```bash
curl -X POST http://127.0.0.1:8000/api/v1/ingest/ \
  -H "Content-Type: application/json" \
  -d '{"repo_id": "my-first-repo", "repo_url": "https://github.com/pallets/flask.git"}'
# → {"job_id": "job-..."}
curl http://127.0.0.1:8000/api/v1/ingest/status/job-...
```

First ingestion downloads the embedding model — expect several minutes. Re-ingesting the same unchanged repo returns `"action": "noop"` in seconds (that's the incremental hashing working).

## 5. Ask questions / explore

- **Chat tab** — streaming answers; source-file chips appear before the text.
- **Analysis tab** — entry points, reading order, folder responsibilities.
- **Graph tab** — the 3D code map. Colors = auto-discovered clusters; red thin edges = low-confidence resolutions; click a node to re-center on its neighborhood.

Or the API directly:
```bash
curl -X POST http://127.0.0.1:8000/api/v1/query/ \
  -H "Content-Type: application/json" \
  -d '{"repo_id": "my-first-repo", "query": "How does routing work?"}'

curl "http://127.0.0.1:8000/api/v1/graph/my-first-repo/stats"
curl "http://127.0.0.1:8000/api/v1/graph/my-first-repo/subgraph?depth=2&limit=200"
```

## 6. Run the tests and the eval

```bash
cd backend
pytest tests/ -v                       # unit tests + graph validation harness
python -m app.eval.run_eval ai-copilot-self   # after ingesting this repo itself
```

## Where data lives

```
backend/data/
  vectors/<repo_id>.index      FAISS index per repo
  metadata/metadata.db         chunks + FTS5 + file hashes + embedding meta
  metadata/graph.db            knowledge-graph nodes + edges
  repos/<repo_id>/             persisted working copy (analysis + OKF read this)
  okf/<repo_id>/.knowledge/    the generated OKF bundle
```

Delete `backend/data/` to reset everything.

## Common problems

| Symptom | Cause / fix |
|---|---|
| API starts but ingestion never runs | Worker not running, or Redis down (`docker ps`) |
| `Job queue is temporarily unavailable` | Redis unreachable at `REDIS_URL` |
| First query very slow | Embedding + reranker models loading (lazy, one-time per process) |
| Chat says GEMINI_API_KEY not set | Add it to `backend/.env`; only chat needs it |
| Graph tab empty | Repo had no supported-language files, or ingestion's graph stage logged `ingest.graph_degraded` — check worker logs |
| `pip` fails on tree-sitter | Ensure the exact pins: `tree-sitter==0.21.3`, `tree-sitter-languages==1.10.2` |
