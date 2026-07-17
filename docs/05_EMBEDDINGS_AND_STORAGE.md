# Embeddings, Cache, and the Vector Store

Three files own "text → numbers → searchable storage".

---

## `app/services/embeddings.py` — meaning as vectors, versioned

**What it does.** Wraps a SentenceTransformer model behind one function: `generate_embeddings(texts) → float32 array (n, dim)`.

**The four design decisions (each fixes a real failure mode):**

1. **Config-driven, code-trained model.** Default `BAAI/bge-m3` (1024-dim) per the locked decision — generic English MiniLM measurably under-performs on code. Swap via `.env` (e.g. MiniLM on a laptop); nothing else changes.
2. **`model_id` versioning.** `model_id = f"{name}#{dim}"` is stamped on every stored vector and recorded per repo. Vectors from different models are mathematically incomparable — without versioning, changing the model quietly breaks all similarity search. The vector store's `needs_reindex()` compares stored vs current and ingestion wipes + rebuilds when they differ. **Rule: version every vector.**
3. **Lazy loading.** The multi-GB model loads on FIRST USE, not import. The API boots instantly; tests and the graph pipeline never pay for it; and if the model can't load, `EmbeddingUnavailable` is raised — retrieval catches it and degrades to keyword-only search instead of the app dying at import.
4. **Content-hash caching.** Before encoding, each text is looked up by `sha256(text)` in the cache; only misses hit the GPU/CPU; results are written back with a 7-day TTL. Re-ingesting a repo re-embeds only genuinely new text.

Also here: **L2 normalization** (when `EMBEDDING_NORMALIZE=true`) so inner-product search ≡ cosine similarity — pair this with the store's `IndexFlatIP`.

---

## `app/services/cache.py` — Redis with a safety net

A tiny cache client with one resilience contract: **a cache must never take the service down.**

- On construction it pings Redis (1 s timeout). Reachable → Redis backend. Not → logs ONE `cache.fallback` warning and uses a bounded in-process dict (10 000 entries, oldest-half eviction, TTLs respected).
- If Redis dies mid-flight, the next error flips it to the local dict permanently (until restart). Every method swallows backend errors.
- API: `get_json(key)` / `set_json(key, value, ttl)`. Users: embeddings (`emb:<model>:<hash>` → vector) and retrieval (`qry:<repo>:<k>:<normalized query>` → results).

Local-dict caveat (documented): it's per-process — fine for one replica, Redis makes it shared across replicas.

---

## `app/services/vector_store.py` — FAISS + SQLite, one class, three concerns

### The storage model

| Store | Holds | Why |
|---|---|---|
| `data/vectors/<repo>.index` | FAISS index (dense vectors) | one per repo = hard isolation |
| SQLite `chunk_metadata` | chunk text, file, chunk_index, **model_id, space, start_line, end_line** | vectors are anonymous; this is who they are |
| SQLite `chunk_fts` (FTS5) | same text, BM25-indexed | keyword search for free |
| SQLite `file_hashes` | SHA-256 per file per repo | incremental ingestion state |
| SQLite `embedding_meta` | model_id per repo | the re-index trigger |

The **`faiss_id`** is the join key everywhere: FAISS returns ids → SQLite rows → (later) graph nodes via `nodes.chunk_id`.

### Why `IndexIDMap`

Plain FAISS indices only append. Wrapping in `IndexIDMap` lets us `remove_ids([...])` — the surgical deletion that makes incremental updates possible (changed file → remove its old vectors → add new ones). New indices use `IndexFlatIP` (cosine on normalized vectors); old L2 index files keep working because FAISS stores the type in the file.

### The Phase 1 upgrades (each is a lesson)

- **Migrations:** `_add_column_if_missing()` (PRAGMA table_info → ALTER TABLE) upgrades existing databases in place — schema changes must never require "delete your data".
- **Spaces:** every chunk is tagged `'code'` or `'okf'`. `search_dense(..., space=...)` over-fetches 3× then filters — that's how one physical index serves the multi-source retrieval design.
- **Rank-order bug fix:** the old metadata fetch used `WHERE faiss_id IN (...)`, which returns rows in *SQL* order — silently scrambling FAISS's carefully ranked results before rerank. `fetch_chunks_by_ids()` now re-orders to match the input id list. Subtle, invisible, quality-destroying — the kind of bug evals catch.
- **LRU cap on open indices:** the old `self.indices` dict grew forever (a memory leak across many repos). Now an `OrderedDict`, `move_to_end` on access, evict-oldest past 16 open handles.
- **`store_chunks` returns the assigned faiss_ids** so ingestion can hand the graph builder its chunk↔node join data.
- **SQLite hygiene:** WAL mode (readers don't block the writer), `busy_timeout`, and every `IN (...)` batched ≤ 500 ids (SQLite caps bound variables at ~999).
- **FTS5 query sanitizing:** raw user text (`"how does re-embedding work?"`) is invalid FTS MATCH syntax; queries are reduced to word tokens joined with `OR`.

### Public API (what other files call)

`get_file_hashes`, `remove_files`, `store_chunks`, `search_dense`, `search_sparse`, `fetch_chunks_by_ids`, `needs_reindex`, `wipe_repo`.

**Rebuild checkpoint:** after this file, you can store and search by hand:
```python
ids = store.store_chunks("r", chunks, embeddings, {}, model_id="m#384")
store.search_dense("r", query_vec, top_k=5)      # ranked chunks back
store.search_sparse("r", "compute sha256", 5)    # BM25 results back
```
