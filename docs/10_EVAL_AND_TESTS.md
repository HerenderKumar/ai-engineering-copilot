# Eval and Tests — How We Know It Works

Two different safety nets: the **eval** measures retrieval *quality* (a number to improve); the **tests** verify *correctness* (pass/fail gates). The project rule: every phase is gated on these — "it feels better" doesn't merge.

## The eval harness (`app/eval/`)

### `metrics.py` — the rulers

Pure functions, no dependencies (verify them by hand before trusting any number):

- **recall@k** — of the labeled relevant files, what fraction appeared in the top k? The ceiling metric: the LLM can only cite what retrieval surfaced.
- **MRR** — 1/rank of the FIRST relevant result, averaged. Position sensitivity: 1.0 = always first.
- **nDCG@k** — quality of the whole ordering with logarithmic position discounting (early hits worth more), normalized against the ideal ordering.

Relevance is binary at FILE level — file labels are cheap to write and are the standard first rung for code-RAG evals.

### `questions.py` — the ground truth

`QUESTION_SETS = {repo_id: [{question, relevant_files}, ...]}`. Twelve labeled questions ship for this repo itself (`ai-copilot-self`), so the eval runs out of the box. Labeling rules: mix phrasings WITH and WITHOUT exact identifiers (identifier-only questions make BM25 look perfect and teach you nothing); list every file that genuinely contains the answer; 15-30 questions is plenty.

### `run_eval.py` — the harness

(The original imported modules that never existed — it could not run at all.) Now: for each labeled question → run the REAL `retrieve_context()` (the whole pipeline: rewrite → dense+sparse → RRF → graph expand → rerank) → reduce ranked chunks to ranked unique files → score → aggregate → print + save a timestamped JSON report under `eval_results/` (reports are diffable across phases — that's how you prove Phase 1 beat Phase 0).

```bash
python -m app.eval.run_eval ai-copilot-self
```

Deliberately stops BEFORE the LLM: retrieval quality is what phases gate on, and skipping generation keeps the eval fast, free, deterministic. (Answer-faithfulness eval is the documented next rung.)

## The test suite (`tests/`)

### `conftest.py`
Sets env vars (temp `DATA_DIR`, empty API key, cache off) **before any `app.` import** — the config singleton materializes at import time, so ordering is the whole trick. Fixtures: `fixture_repo` (copies the sample repo so tests may mutate it), `graph_env` (isolated `GraphStore` per test).

### `fixtures/sample_repo/` — the hand-labeled mini repo
Three Python files engineered so every resolution rule fires at least once: `users.py` (a base class + a free function), `billing.py` (inherits `Base`, calls an imported symbol, calls `self.save()`, calls external `stripe.PaymentIntent.create()`), `orders.py` (constructs `Billing()`, calls `b.charge()`, and — the trap — defines its own `create()` that shares a name with the stripe call).

### `test_graph_builder.py` — **THE validation harness** (Phase 2 exit gate)
`EXPECTED_CALLS / INHERITS / IMPORTS / CONTAINS` are written down as ground truth, and the test asserts the built graph matches **exactly** — precision AND recall = 1.0 on the labeled subset. Plus: the stripe false-positive guard (`billing → orders.create` must NOT exist), confidence-tier spot checks (0.90 imported-symbol, 0.60 unique-global), **determinism** (build twice → byte-identical node/edge sets), and **reconciliation** (rename `fetch_user_tier` in the fixture → rebuild only `users.py` → the stale edge is gone, `billing.py` was auto-re-resolved, and zero dangling edge endpoints remain). Gate CI on this file.

### The rest
`test_graph_store.py` — stable-id determinism, idempotent upserts, cycle-safe bounded BFS, directed `trace_path`, and `delete_files` correctly reporting which files must re-resolve. `test_chunking.py` — the unified contract (the Phase 0 bug can never regress), context-header content, 1-based line spans, overlap in the line-split fallback, vendored-path filtering. `test_fusion.py` / `test_metrics.py` / `test_query_rewrite.py` — hand-computed expected values.

Heavy-dependency tests (`tree-sitter`, chunking) auto-skip via `pytest.importorskip` when grammars aren't installed, so the suite is runnable everywhere and strict where it matters.

```bash
cd backend && pytest tests/ -v
```

## The ops metrics (the third leg)

Quality (eval) + correctness (tests) + **operations**: the structured logs emit per-stage `duration_ms`, candidate counts, cache hits, fallback events (`retrieval.fallback`, `ingest.graph_degraded`), and graph `resolution_rate` — chart p50/p95 per stage, alert on fallback rate. Handoff §9 defines the full gate list.
