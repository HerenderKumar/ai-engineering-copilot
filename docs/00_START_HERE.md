# Start Here — What This Project Is and How to Read These Docs

Welcome! This documentation explains the **AI Engineering Copilot** from zero, assuming you are a beginner who wants to understand every file and eventually **rebuild the whole thing yourself**.

## What the project does, in one paragraph

You give it a Git repository URL. It reads all the code, cuts it into meaningful pieces ("chunks"), turns each piece into numbers that capture its meaning ("embeddings"), and simultaneously builds a **map of the code's structure** — which function calls which, which file imports which, which class inherits from which (the "knowledge graph"). When you then ask a question in plain English ("how does login work?"), it finds the most relevant code pieces using three kinds of search at once, walks the graph to pull in connected code you didn't know to ask about, and hands everything to an LLM (Gemini) which writes an answer with file citations. There is also a 3D visualization of the code graph, and an auto-generated "knowledge folder" (OKF) that explains the repo to humans and AI agents.

This design is called **hybrid, multi-source GraphRAG**. The two strategy documents that define it are `GraphRAG_Hybrid_Strategy.md` (the *why*) and `AI_Copilot_Build_Handoff.md` (the *what to build*). Everything implemented here follows the phased plan in the handoff: Phase 0 (stabilize) → Phase 1 (sharper RAG) → Phase 2 (the graph) → Phase 3 (fuse + see). Phase 4 (MCP server + scale hardening) is designed but not yet built.

## The four layers (memorize this picture)

```
┌────────────────────────────────────────────────────────┐
│ 4. DELIVERY      web app (chat + analysis + 3D graph)  │
├────────────────────────────────────────────────────────┤
│ 3. CURATED KNOWLEDGE   OKF bundle (.knowledge/*.md)    │
├────────────────────────────────────────────────────────┤
│ 2. SEMANTIC      embeddings + BM25 + RRF + rerank      │
├────────────────────────────────────────────────────────┤
│ 1. STRUCTURAL    code knowledge graph (the backbone)   │
└────────────────────────────────────────────────────────┘
```

## Reading order

| Doc | What it covers |
|---|---|
| `01_CONCEPTS.md` | Every term you'll meet (RAG, embedding, FAISS, BM25, RRF, knowledge graph…) explained simply |
| `02_SETUP_AND_RUN.md` | Install, configure, run all three processes, ingest your first repo |
| `03_BACKEND_CORE.md` | `config.py`, `logging.py`, `main.py` — the app skeleton |
| `04_INGESTION_PIPELINE.md` | How a repo URL becomes chunks: API → queue → worker → chunker |
| `05_EMBEDDINGS_AND_STORAGE.md` | Embeddings, caching, FAISS + SQLite storage |
| `06_RETRIEVAL_PIPELINE.md` | The full question-answering pipeline, stage by stage |
| `07_KNOWLEDGE_GRAPH.md` | The graph: store, builder, layout, API — the heart of the project |
| `08_ANALYSIS_AND_OKF.md` | The repo-explainer layer and the OKF knowledge bundle |
| `09_FRONTEND.md` | The React app: chat, analysis, 3D graph views |
| `10_EVAL_AND_TESTS.md` | How we measure quality and prove the graph is correct |
| `11_REBUILD_CHECKLIST.md` | **The rebuild guide**: every file, in build order, with checkpoints |

If you only want to rebuild the project, read `01`, `02`, then jump to `11` and use the other docs as references for each file as you write it.

## Complete file map (what exists and why)

```
backend/
  app/
    main.py                     FastAPI app factory; mounts all routers
    schemas.py                  (legacy, unused — superseded by per-router models)
    core/
      config.py                 every setting/knob, loaded from .env
      logging.py                structured JSON logging + stage timers
    api/
      ingest.py                 POST /ingest → Redis queue; GET /ingest/status
      query.py                  POST /query and /query/stream (SSE) — Q&A
      analysis.py               GET /analysis/{repo}; POST .../okf
      graph.py                  GET /graph/{repo}/subgraph|stats|node — 3D UI feed
    workers/
      ingestion_worker.py       queue consumer process (runs separately)
    services/
      parsing.py                tree-sitter helpers: defs/imports/calls per language
      chunking.py               CodeChunker: AST-boundary chunks + context headers
      embeddings.py             code-trained embedder, versioned, cached, lazy
      cache.py                  Redis cache with in-memory fallback
      vector_store.py           FAISS per repo + SQLite (chunks, FTS5, hashes, meta)
      fusion.py                 Reciprocal Rank Fusion
      query_rewrite.py          question → 2-3 search variants
      retrieval.py              the whole retrieval pipeline (orchestrator)
      reranker.py               cross-encoder precision pass
      prompt_builder.py         graph-aware LLM prompt assembly
      ingestion.py              ingestion orchestrator (the write path)
      graph_store.py            SQLite persistence of nodes/edges + traversals
      graph_builder.py          two-pass extraction + call resolution
      graph_layout.py           3D positions + Louvain clusters, precomputed
      okf_emitter.py            .knowledge/ bundle generator + embedder
      llm/gemini.py             pluggable LLM client (lazy)
      analysis/                 heuristic repo explainers (single-purpose modules)
    eval/
      metrics.py                recall@k, MRR, nDCG (pure functions)
      questions.py              labeled question sets per repo
      run_eval.py               the eval harness CLI
  tests/
    conftest.py                 test bootstrap (env isolation)
    fixtures/sample_repo/       hand-labeled mini repo (ground truth)
    test_*.py                   unit tests + THE graph validation harness
  requirements.txt
  .env.example
frontend/
  src/
    api.js                      all backend calls in one place
    App.jsx                     shell: ingest bar + Chat/Analysis/Graph tabs
    GraphView.jsx               3D force-graph renderer (precomputed layout)
    App.css / index.css         self-contained dark theme
  package.json
docs/                           you are here
```

## What was deliberately NOT built yet (and why)

These are Phase 4 / open-decision items from the handoff — documented so you know they're missing on purpose:

- **MCP server** (typed tools like `semantic_search`, `trace_path` for coding agents) — Phase 4.
- **LSP/Kythe precision tier** for call resolution — waiting on the "top 4-5 languages" product decision; the tree-sitter tier (~80% accuracy, confidence-scored) works everywhere today.
- **Product wedge features** (e.g. onboarding flows) — waiting on the product-owner wedge decision; the analysis layer is the foundation for it.
- **FAISS HNSW, Redis consumer groups + DLQ, raw-code second embedding space, embedder fine-tuning** — scale work, gated on eval evidence.
