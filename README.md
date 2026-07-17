# AI Engineering Copilot — Hybrid Multi-Source GraphRAG

Give it a Git URL → it builds a **code knowledge graph** (who calls what, deterministically) + a **semantic index** (embeddings + BM25, RRF-fused, cross-encoder reranked) + a **curated OKF knowledge bundle** — then answers questions with an LLM that reasons over real relationships, streams tokens, and cites files & lines. Includes a **3D graph UI** with precomputed layout and a confidence overlay.

```
question → rewrite → dense ×(variants×spaces) + BM25 → RRF → graph expand → rerank → LLM → answer + citations
ingest   → clone → hash-diff → parse once → chunks+headers → embed (versioned) → graph build+reconcile → 3D layout → OKF
```

## Quick start

```bash
# 0) Redis
docker run -d -p 6379:6379 redis:7

# 1) API          (terminal A)
cd backend && python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt && cp .env.example .env   # add GEMINI_API_KEY for chat — optional: without it (or when Gemini is down) chat falls back to local Ollama (`ollama serve` + a pulled model)
uvicorn app.main:app --reload

# 2) Worker       (terminal B)
cd backend && source .venv/bin/activate
python -m app.workers.ingestion_worker

# 3) Frontend     (terminal C)
cd frontend && npm install && npm run dev                  # → localhost:5173
```

Paste a repo URL, press *Index repo*, then chat / analyze / explore the 3D graph.

```bash
cd backend && pytest tests/ -v                # unit tests + graph validation harness
python -m app.eval.run_eval ai-copilot-self   # retrieval quality report
```

## Documentation

**Beginner-oriented, file-by-file docs live in [`docs/`](docs/00_START_HERE.md)** — including a complete [rebuild checklist](docs/11_REBUILD_CHECKLIST.md) to reimplement the project from scratch. Strategy & plan: `GraphRAG_Hybrid_Strategy.md` (why) and `AI_Copilot_Build_Handoff.md` (what).

## Status vs the phased plan

| Phase | Scope | Status |
|---|---|---|
| 0 Stabilize | chunker fix, JSON logging, eval harness | ✅ built |
| 1 Sharper RAG | code embedder + versioning, RRF, context headers, rewrite, caches | ✅ built |
| 2 The graph | store + two-pass builder, confidence edges, reconciliation, validation harness | ✅ built |
| 3 Fuse + see | graph-expanded retrieval, graph-aware prompts, 3D UI, OKF bundle | ✅ built |
| 4 Ship + scale | MCP server, webhook, HNSW, consumer groups/DLQ, precision tier | ▢ designed, not built |
