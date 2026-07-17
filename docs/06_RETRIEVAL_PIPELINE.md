# The Retrieval Pipeline — How a Question Becomes an Answer

The read path. Seven files, one flow:

```
question
  → query_rewrite.py     1 question → 2-3 search variants
  → vector_store         dense (per variant × per space) + sparse BM25
  → fusion.py            RRF: many ranked lists → one
  → graph expansion      (inside retrieval.py, using graph_store)
  → reranker.py          cross-encoder precision pass
  → prompt_builder.py    graph-aware prompt
  → llm/gemini.py        the answer
  → api/query.py         serves it (plain + SSE streaming)
```

`retrieval.py` orchestrates the middle; read it last.

---

## `app/services/query_rewrite.py`

One phrasing rarely hits every index well: dense search likes sentences, BM25 likes identifiers. So each question becomes up to three variants: the **original** (always first — never make things worse), an **identifier expansion** (`getUserTier` → "get user tier"; `file_hashes` → "file hashes") via camelCase/snake_case splitting, and a **keyword skeleton** (stopwords stripped). Deliberately heuristic: deterministic, free, zero-latency, unit-testable. An LLM rewriter can replace it behind the same function — but only after the eval proves it's worth a model call per query (the phase-gating rule).

## `app/services/fusion.py`

RRF as two pure functions. `rrf_fuse(ranked_lists, k=60)`: each document earns `1/(k+rank)` per list containing it; sum; sort. Rank-only → immune to incomparable score scales; `k=60` damps the head so one list can't dominate. Ties break by id — determinism is a project-wide invariant. All lists go in at once: dense×(variants × spaces) + sparse×variants, typically 6-9 lists.

## `app/services/retrieval.py` — the orchestrator

Stage by stage (each wrapped in `stage_timer`, each with a fallback):

0. **Query cache** — normalized query hit → return instantly (5-min TTL).
1. **Rewrite** (flag: `QUERY_REWRITE_ENABLED`).
2. **Dense** — embed all variants in one batch; search each variant against both spaces (`code`, `okf`); collect ranked id lists + a `docs_by_id` map. `EmbeddingUnavailable` → log `retrieval.fallback stage=dense`, continue **sparse-only**.
3. **Sparse** — BM25 per variant; more ranked lists.
4. **RRF fuse** all lists → one ranked candidate list.
5. **Graph expansion** (Phase 3, flag-gated) — see below. Graph errors → plain hybrid RAG.
6. **Rerank** the top `RERANK_CANDIDATE_CAP` (50) candidates; failure → keep RRF order.
7. Log `retrieval.done` with candidate counts + which fallbacks fired; cache; return top-k.

**Graph expansion (`_graph_expand`)** is the GraphRAG move:
1. Top fused chunks → graph nodes via the `chunk_id` join.
2. `graph_store.neighbors(...)` walks 1–2 hops over CALLS/IMPORTS/INHERITS/CONTAINS (bounded, cycle-safe).
3. Neighbor nodes' `chunk_id`s → fetch those chunks → **new candidates** tagged `via_graph: true` (code that shares *no words* with the question but is *wired to* the answer — the thing pure RAG structurally cannot find).
4. Edges become human-readable lines ("`billing.Billing.charge` calls `users.fetch_user_tier`") attached to seed chunks as `graph_context` — the prompt builder's raw material.

## `app/services/reranker.py`

The precision pass. A cross-encoder (`ms-marco-MiniLM-L-6-v2`) reads query+chunk *together* and outputs one relevance score — far more accurate than bi-encoder cosine, far too slow for a whole corpus; that's why it only sees the fused top-50. Loads lazily; any failure returns the input order truncated (never crashes a query).

## `app/services/prompt_builder.py`

Assembles the final prompt: system rules (answer only from context; cite file paths; use the relationships section; don't invent) → **STRUCTURAL RELATIONSHIPS preamble** (the deduped `graph_context` lines — the LLM now *knows* who calls what instead of guessing) → code grouped by file, each snippet labeled with its line range and a `[included via code-graph relationship]` marker when applicable → the question. Without the preamble the model gets a bag of snippets; with it, dependency questions get answered with real call chains.

## `app/services/llm/gemini.py`

The pluggable reasoning layer. `GeminiClient` with `generate_response(prompt)` and `generate_stream(prompt)` (async generator of tokens), plus sync `ask_gemini()` for the analysis layer. **Lazy client creation** — the fix that made the whole app bootable without an API key (only answer generation needs it; swapping to another LLM = reimplementing two methods).

## `app/api/query.py`

Two endpoints. `POST /query/` — retrieve → prompt → answer, returns `{answer, sources}`. `POST /query/stream` — the same, but as **Server-Sent Events**: first frame `{"type": "sources", "data": [...]}` (the UI shows file chips while the LLM thinks — perceived latency win), then many `{"type": "chunk"}` token frames, finally `{"type": "done"}`. Implemented as an async generator handed to `StreamingResponse(media_type="text/event-stream")`.

---

**Worked example** (from the strategy doc): *"How do we avoid re-embedding an unchanged repo?"* — no function names in the question. Rewrite adds "avoid re embedding unchanged repo" keywords; dense surfaces the `_compute_sha256` chunk; RRF ranks it #1; graph expansion pulls its callers/callees (`get_file_hashes`, `remove_files`, `process_job` — none lexically similar to the question); rerank trims; the prompt lists the call chain; the answer explains hash-diff ingestion AND names the chain. Pure RAG misses the neighbors; pure graph can't even start from a name-free question. That's the hybrid thesis in one query.

**Rebuild order:** fusion → query_rewrite (both pure + testable) → retrieval steps 0-4 → reranker → steps 6-7 → prompt_builder → gemini → api/query.py → add graph expansion last (it needs Phase 2). Checkpoint: ingest this repo, run the eval, expect recall@10 ≈ 0.8+ on the self-labeled set.
