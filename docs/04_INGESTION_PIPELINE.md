# Ingestion Pipeline — From Git URL to Indexed Repo

Ingestion is the **write path**: everything that happens between "here's a repo URL" and "you can now ask questions". Five files cooperate:

```
POST /api/v1/ingest  (api/ingest.py)          the front door
        │  pushes JSON job onto Redis list
        ▼
ingestion_worker.py  (separate process)        the consumer
        │  calls
        ▼
services/ingestion.py                          the orchestrator
        │  uses
        ├── services/parsing.py                tree-sitter facts
        └── services/chunking.py               CodeChunker
```

---

## `app/api/ingest.py` — the front door

**Pattern: accept fast, work later.** Cloning + embedding can take minutes; an HTTP handler must answer in milliseconds. So the endpoint only (1) validates the request (`repo_id` + a real `HttpUrl`), (2) `RPUSH`es a JSON payload onto the Redis list `repo_ingestion_queue`, (3) writes an initial `queued` status, (4) returns **202 Accepted** with a `job_id`.

`GET /ingest/status/{job_id}` reads the status key the worker maintains (`queued → processing → completed | failed`) — this is what the frontend polls every 2 s.

**Error contract:** Redis down → **503** ("queue temporarily unavailable"), not a 500 — callers can retry 503s.

---

## `app/workers/ingestion_worker.py` — the consumer process

Runs as its own process: `python -m app.workers.ingestion_worker`. An infinite loop around `BLPOP` (blocking pop — sleeps until a job arrives, and Redis guarantees each job goes to exactly ONE worker, so you can run five workers for 5× throughput with zero code changes).

Per job: parse JSON → validate fields → write `processing` status → call `ingest_repository(...)` → write `completed`/`failed` status + push the full result onto a results queue. Malformed JSON, Redis blips and unexpected exceptions are each caught separately — the worker never dies from one bad job.

**Known limitation (deliberate, Phase 4):** with bare `BLPOP`, a job dies if its worker crashes mid-run. The upgrade path is Redis consumer groups + visibility timeouts + a dead-letter queue.

---

## `app/services/ingestion.py` — the orchestrator

The heart of the write path. Steps, in order:

1. **Shallow clone** (`git clone --depth 1`) into a temp dir — full history isn't needed.
2. **Model version gate.** Ask the vector store which embedding model this repo's existing vectors were made with. If it differs from the configured model → `wipe_repo()` (vectors + graph) and re-index everything. Mixing vectors from two models in one index produces silently-wrong similarity scores — the worst kind of bug.
3. **Hash diff (the incremental core).** SHA-256 every processable file; compare with the stored `file_hashes` table:
   - hash unchanged → skip entirely (the 95% case on re-ingest)
   - new/changed → process list
   - in the store but not on disk → delete list
   If both lists are empty → return `{"action": "noop"}`.
4. **Persist a working copy** to `REPOS_DIR/<repo_id>` — the analysis layer and OKF emitter need the source *after* the temp clone is deleted. (This fixes the old bug where `/analysis/{repo_id}` always 404'd.)
5. **Purge stale vectors** for changed+deleted files (surgical FAISS `remove_ids` + SQL deletes).
6. **Parse ONCE → chunk → embed → store.** Each changed file is parsed with tree-sitter a single time; the same tree feeds the chunker now and the graph builder next (the "parse once, use twice" rule). Chunks carry line spans; `store_chunks` returns the assigned `faiss_ids`, from which we build `chunk_spans = {file: [(start_line, end_line, faiss_id)]}` — the join data the graph needs.
7. **Graph build + reconciliation** (`GraphBuilder.build_or_update`) and **3D layout recompute** — wrapped in try/except: a graph failure logs `ingest.graph_degraded` but never fails the job (retrieval then works as plain hybrid RAG).
8. **OKF bundle** emit + embed — same graceful contract.

Every step logs through `stage_timer` with `repo_id` and `job_id`, so a slow ingestion can be diagnosed stage by stage.

---

## `app/services/parsing.py` — the shared tree-sitter toolbox

One module owns everything tree-sitter so no other file needs to know its API. Exposes:

- `LANGUAGE_MAP` (`.py → python`, `.ts → typescript`, …), `IGNORED_DIRS` (node_modules, .git, dist…), `is_processable_file()`
- `parse(text, lang)` → tree or **None on any failure** (per-file isolation: one weird file can't poison a pipeline)
- `extract_definitions(tree, text, lang)` → `[{kind: class|function|method, name, parent, bases, signature, start_line, end_line, ...}]` — implemented by *walking* the tree: at a `class_definition` node grab the name field, recurse into the body marking `parent_class`, etc.
- `extract_imports(...)` → `[{module, names: [(name, alias)], alias, line}]` covering `import x`, `import x as y`, `from x import a as b`, JS `import {a as b} from 'x'`, Go/Rust forms
- `extract_calls(...)` → `[{name, receiver, line}]`: `foo()` → name=foo; `svc.charge()` → name=charge, receiver=svc

**Language tiers (honest, documented):** python/JS/TS get full extraction (defs+imports+calls+inheritance); go/rust/cpp get defs+imports; everything else falls back to plain line chunking. This is the plan's tree-sitter "breadth tier" (~80%); an LSP "precision tier" can plug in later without changing any caller.

---

## `app/services/chunking.py` — the unified `CodeChunker`

**THE original bug lived here.** `ingestion.py` imported `CodeChunker` and called `.chunk_text(...)`, but the file only defined `ASTChunker` with `.get_chunks()` — `ImportError` at startup; ingestion could never run. The fix (locked decision #11): one class, `CodeChunker(max_chunk_size, overlap_size)` exposing `chunk_text(text, file_path, tree=None)`, with `ASTChunker = CodeChunker` and `get_chunks = chunk_text` kept as aliases so neither name can ever break again. A regression test locks the contract.

**How chunking works:**

- Walk the file's **top-level AST nodes** (functions, classes, statements). Pack consecutive small nodes into one chunk until `max_chunk_size` would be exceeded, then start a new chunk — chunks therefore break at *logical boundaries*, never mid-function.
- A single oversized node (a 3000-char function) is split by lines **with `overlap_size` characters carried between pieces**, so a statement at a cut boundary appears in both pieces (overlap only matters here; AST boundaries don't need it).
- Unparseable/unknown files → plain line chunking with the same overlap.

**Context headers (Phase 1, the recall booster):** every chunk's stored+embedded text is prefixed with

```
[Path: src/checkout/billing.py] [Lang: python] [Class: Billing]
[Defs: charge] [Imports: stripe, users] [Calls: fetch_user_tier, save]
```

Now the vector encodes *topology + semantics* — "user tier lookups during checkout" finds `billing.py` even with zero shared words. Two hard rules from the strategy doc: **only stable facts** (path, class, imports, *outgoing* calls — never incoming callers, which change when other files change and would silently stale this vector), and **keep it short** (caps: 5 defs, 6 imports, 6 calls — over-stuffed headers make all chunks look alike and *drop* recall).

**Every chunk dict:** `{file_path, chunk_index, content, start_line, end_line, language}`. The 1-based line spans power file:line citations and the chunk↔graph-node join.

**Rebuild order for this pipeline:** parsing.py → chunking.py (test on one file) → vector store (next doc) → ingestion.py → api/ingest.py → worker. Checkpoint: ingest a tiny repo twice; second run must log `noop`.
