# The Rebuild Checklist — Implement It Yourself, File by File

This is the ordered path to rebuild the entire project from an empty folder. Each step names the file(s), what must exist when you're done, and a **checkpoint** — don't move on until it passes. Deep explanations live in the doc referenced per step.

**Ground rules that make everything else work** (violate these and later steps get mysterious):
1. Config is a singleton read from `.env` — no behavior hardcoded.
2. Heavy models load lazily — importing a module must be free.
3. Every stage degrades gracefully and logs its fallback.
4. Determinism everywhere — same input, same output (ids, orderings, layouts).
5. Version every vector; stable ids for every node.
6. Measure before you celebrate — eval + tests gate each phase.

---

## Stage A — Skeleton (docs: 03)

**A1. `app/core/config.py`** — the `Settings` class, all sections, `.env` support, singleton.
**A2. `app/core/logging.py`** — `JsonFormatter`, `configure_logging()`, `log_event()`, `stage_timer()`.
**A3. `app/main.py`** — app factory + CORS + `/health` (mount routers as they appear).
**A4.** `requirements.txt`, `.env.example`.
✅ *Checkpoint:* `uvicorn app.main:app` boots WITHOUT any API key; `curl /health` returns JSON; log lines are JSON objects.

## Stage B — Parse + chunk (docs: 04)

**B1. `app/services/parsing.py`** — LANGUAGE_MAP, `is_processable_file`, `parse()` (returns None on failure), `extract_definitions`, `extract_imports`, `extract_calls`. Python first; JS/TS second; defs-only tier for go/rust/cpp.
**B2. `app/services/chunking.py`** — `CodeChunker(max_chunk_size, overlap_size).chunk_text(text, path, tree=None)`: AST-boundary packing, oversized-node line split WITH overlap, context headers (stable facts only, capped), line spans, `ASTChunker`/`get_chunks` aliases.
✅ *Checkpoint:* chunk a real `.py` file in a REPL — headers show `[Path] [Lang] [Imports] [Calls]`, spans are 1-based, nothing crashes on a binary/garbage file.

## Stage C — Store + embed (docs: 05)

**C1. `app/services/cache.py`** — Redis-or-local-dict, never throws.
**C2. `app/services/embeddings.py`** — lazy `SentenceTransformer`, `model_id`, content-hash cache, L2-normalize, `EmbeddingUnavailable`.
**C3. `app/services/vector_store.py`** — the big one: schema + migrations (`model_id`, `space`, line spans, `embedding_meta`), `IndexIDMap(IndexFlatIP)`, LRU on open indices, `store_chunks` (returns faiss_ids), `remove_files`, `search_dense` (space filter, rank order preserved), `search_sparse` (tokenized OR query), `fetch_chunks_by_ids` (input-order!), `needs_reindex`, `wipe_repo`.
✅ *Checkpoint:* store 3 hand-made chunks with fake vectors → dense and sparse search both return them ranked; `needs_reindex` flips when you change the configured model name.

## Stage D — Ingestion (docs: 04)

**D1. `app/services/ingestion.py`** — clone → model gate → hash diff → persist working copy → purge → parse-once/chunk/embed/store → build `chunk_spans`. (Graph + OKF hooks come later — leave the try/except stubs.)
**D2. `app/api/ingest.py`** — 202 + Redis enqueue + status key + `GET /status/{job_id}`.
**D3. `app/workers/ingestion_worker.py`** — BLPOP loop, per-job error isolation, status mirroring.
✅ *Checkpoint:* ingest a small public repo via curl; status walks queued→processing→completed; **re-ingest immediately → `"action": "noop"`** (the incremental proof); break a file hash in SQLite and re-ingest → only that file re-processes.

## Stage E — Retrieval (docs: 06)

**E1. `app/services/fusion.py`** — `rrf_fuse` + scored variant, deterministic ties.
**E2. `app/services/query_rewrite.py`** — original + identifier expansion + keyword skeleton, ≤3, deduped.
**E3. `app/services/retrieval.py`** — cache → rewrite → dense×spaces → sparse → RRF → (graph slot) → rerank cap → assemble; every stage timed, every failure a logged fallback.
**E4. `app/services/reranker.py`** — lazy CrossEncoder, fallback = input order.
**E5. `app/services/prompt_builder.py`** — system rules + (preamble slot) + file-grouped snippets with line ranges.
**E6. `app/services/llm/gemini.py`** — lazy client, `generate_response`, `generate_stream`, sync `ask_gemini`.
**E7. `app/api/query.py`** — plain + SSE endpoints.
✅ *Checkpoint:* with NO Gemini key: `retrieve_context()` returns ranked chunks (and works with the embedder uninstalled — sparse-only fallback logs). With a key: `/query/stream` streams sources-then-tokens in curl.

## Stage F — Eval baseline (docs: 10) — do this BEFORE the graph

**F1. `app/eval/metrics.py`** — recall@k, MRR, nDCG, aggregate.
**F2. `app/eval/questions.py`** — label 12+ questions for a repo you ingested.
**F3. `app/eval/run_eval.py`** — harness + timestamped JSON reports.
✅ *Checkpoint:* a baseline number exists. Write it down — Stage G must beat it.

## Stage G — The knowledge graph (docs: 07)

**G1. `app/services/graph_store.py`** — schema (+layout cols), `stable_node_id` (sha256→63-bit), upserts (idempotent), `delete_files` (returns affected files!), `delete_out_edges`, `neighbors` (visited set + depth + node cap), `trace_path`, `subgraph`, `update_layout`, `stats`, `wipe_repo`.
**G2. Tests first:** `tests/test_graph_store.py` — ids, idempotency, cycle safety, directed paths, affected-file reporting.
**G3. `app/services/graph_builder.py`** — pass 1 (register nodes + CONTAINS + collect facts), pass 2 (the confidence ladder 0.95/0.90/0.85/0.60/0.30, the **known-provenance guard**: imported-but-unmatched names NEVER fall back to global), reconciliation (delete → rebuild → re-resolve affected+importers), per-file isolation, `resolution_rate` logging, `chunk_spans` join.
**G4. The validation harness:** `tests/fixtures/sample_repo/` (3 files exercising every rule + the external-namesake trap) + `tests/test_graph_builder.py` (exact ground-truth match, false-positive guard, confidence tiers, determinism ×2, reconciliation heals + zero dangling).
**G5. `app/services/graph_layout.py`** — igraph 3D FR (optional) → networkx spring fallback (seeded), Louvain (seeded), scale, persist.
**G6.** Wire graph + layout into `ingestion.py` (inside try/except → `ingest.graph_degraded`).
✅ *Checkpoint:* `pytest tests/test_graph_builder.py -v` all green — this IS the Phase 2 exit gate. Re-ingest your repo; `GET /graph/<repo>/stats` shows nodes + edges by kind.

## Stage H — Fusion of the two worlds (docs: 06, 07)

**H1.** `retrieval.py::_graph_expand` — top chunks → nodes (chunk_id join) → bounded neighbors → neighbor chunks as `via_graph` candidates + `graph_context` lines on seeds.
**H2.** `prompt_builder.py` — the STRUCTURAL RELATIONSHIPS preamble + `via_graph` snippet markers.
**H3. `app/api/graph.py`** — `/subgraph` (center/file/kinds/depth/limit; ids as strings!), `/stats`, `/node/{id}`.
✅ *Checkpoint:* re-run the eval — hybrid should beat your Stage F baseline; ask a "what calls X?" question and see the call chain named in the answer.

## Stage I — Analysis + OKF (docs: 08)

**I1.** `app/services/analysis/*` — the heuristic explainers + aggregator (LLM optional with fallback).
**I2.** `app/api/analysis.py` — mounted, reading `REPOS_DIR`.
**I3.** `app/services/okf_emitter.py` — frontmatter writer, 7 docs + README index, embed under `space='okf'`, never raises; wire into ingestion.
✅ *Checkpoint:* `.knowledge/` exists with 8 files; a "why"-question cites an OKF doc.

## Stage J — Frontend (docs: 09)

**J1.** `src/api.js` (incl. the SSE reader) → **J2.** `App.jsx` shell + polling → **J3.** ChatTab → **J4.** AnalysisTab → **J5.** GraphView (pinned coords, cooldownTicks=0, cluster colors, red low-confidence edges, click-to-center) → **J6.** CSS.
✅ *Checkpoint:* full loop in the browser: index → chat with streaming + source chips → analysis panels → rotate the 3D graph, click a node.

## Stage K — What's deliberately left (Phase 4 — the handoff's roadmap)

MCP server (`semantic_search`, `trace_path`, `impact_of_change`, `read_node_code`, `explain_feature` as typed tools); webhook-triggered ingestion; Redis consumer groups + DLQ; FAISS HNSW past ~100k vectors; LSP/Kythe precision tier; raw-code second embedding space; embedder fine-tuning on graph pairs — each gated on the eval, per the plan.

---

### The dependency graph of the build (why this order)

```
config ─ logging ──► parsing ──► chunking ──► vector_store ──► ingestion ──► worker/API
                                    │              │
                                    ▼              ▼
                             graph_builder ◄── chunk_spans
                                    │
graph_store ────────────────────────┘──► layout ──► /subgraph ──► 3D UI
fusion + rewrite ──► retrieval ──► reranker ──► prompt ──► LLM ──► /query ──► Chat UI
metrics ──► eval (baseline BEFORE graph, re-run AFTER)
analysis ──► OKF ──► okf space in retrieval
```

Anything upstream broken = everything downstream mysterious. Build in order, checkpoint every stage.
