# Concepts — Every Term Explained From Zero

Read this once; everything else in the project becomes readable.

## Embeddings — "meaning as numbers"

An **embedding model** is a neural network that converts text into a list of numbers (a *vector*), e.g. 1024 numbers. The magic property: texts with **similar meaning get similar vectors**. "How do we check for changed files?" and `def _compute_sha256(file_path):` end up close together in vector space even though they share almost no words.

Distance between vectors is measured with **cosine similarity** (angle between vectors). We L2-normalize vectors so cosine similarity equals a simple dot product — that's why `embeddings.py` normalizes and the FAISS index uses "inner product".

We use a **code-trained** model (BGE-M3 by default) because generic English models are noticeably worse at code. This project stamps every stored vector with a `model_id` — vectors from different models are mathematically incompatible, so changing the model triggers a full, controlled re-index.

## Vector store / FAISS — "find similar vectors fast"

**FAISS** (Facebook AI Similarity Search) is a library that stores millions of vectors and finds the nearest ones to a query vector in milliseconds. We keep **one FAISS index per repository** (isolation), wrapped in an `IndexIDMap` so individual vectors can be *deleted by id* — that's what makes incremental updates possible.

## BM25 / sparse search — "old-school keyword search, still essential"

**BM25** is the classic ranking formula behind search engines: score documents by how often query words appear, weighted by how rare each word is. It's called **sparse** search (vectors of word counts are mostly zeros) vs the embedding **dense** search. Why keep both? Embeddings understand *meaning* but can fumble exact identifiers; BM25 nails `IndexIDMap` typed verbatim but knows nothing about synonyms. SQLite's **FTS5** extension gives us BM25 for free inside the database we already use.

## Chunking — "cut files into pieces models can eat"

Models have size limits, so files are split into **chunks**. Naive chunking cuts functions in half. **AST chunking** uses the syntax tree to cut at *logical boundaries* — whole functions/classes. This project also prepends a **context header** to every chunk (path, language, class, imports, outgoing calls) so the embedding captures *where the code lives in the architecture*, not just its text. Rule: headers contain only **stable** facts — never "who calls me" (that changes when OTHER files change, silently rotting the vector).

## AST / tree-sitter — "the syntax tree"

An **AST** (abstract syntax tree) is code parsed into a tree: a file contains a class, the class contains methods, a method contains calls. **tree-sitter** is a fast parser library with grammars for ~160 languages. It gives us both smart chunk boundaries and the raw facts (definitions, imports, call sites) the knowledge graph is built from.

## RAG — "retrieve, then generate"

**Retrieval-Augmented Generation**: instead of hoping the LLM memorized your codebase, you (1) *retrieve* the most relevant chunks and (2) paste them into the prompt so the LLM *generates* an answer grounded in real code. The retrieval quality ceiling IS the answer quality ceiling — which is why most of this project is retrieval machinery.

## RRF — "combine multiple rankings fairly"

Dense and sparse search return ranked lists with incomparable scores. **Reciprocal Rank Fusion** merges them using positions only: each list gives a document `1/(60+rank)` points; sum across lists. A document ranked well in several lists beats one ranked well in a single list. Simple, robust, no tuning.

## Cross-encoder / reranking — "the precision pass"

The embedding model scores query and document *separately* (fast, coarse). A **cross-encoder** reads query+document *together* through one transformer (accurate, slow). Recipe: retrieve ~50 candidates cheaply, rerank them with the cross-encoder, keep the top 10.

## Knowledge graph — "structural truth"

A **graph** of nodes (files, classes, functions, methods) and edges (relationships):

- `CONTAINS` — file contains class, class contains method
- `CALLS` — `orders.create` calls `Billing.charge`
- `IMPORTS` — file imports file
- `INHERITS` — `Billing` inherits from `Base`

Embeddings answer "what code *means* like this?"; the graph answers "what code *touches* this?" — deterministically. "What breaks if I change X?" is a graph question; no amount of embedding similarity can answer it reliably. Every edge carries a **confidence** score because call resolution is genuinely hard (dynamic dispatch, aliases); uncertain edges are *marked, never hidden*.

## GraphRAG — "the fusion"

Plain RAG retrieves chunks by similarity. **GraphRAG** additionally maps retrieved chunks to graph nodes, walks 1–2 hops to their callers/callees, and pulls those chunks in too — surfacing code that is *connected* to your answer but shares no words with your question. Then the prompt tells the LLM the relationships explicitly ("`charge` is called by `orders.create`"), so it reasons over dependencies instead of a bag of snippets.

## Louvain communities — "auto-discovered modules"

A graph algorithm that finds **clusters** of nodes more connected to each other than to the rest. On a code graph, clusters ≈ subsystems. The 3D view colors nodes by cluster, giving an instant architecture map.

## OKF — "knowledge as a folder of Markdown"

**Open Knowledge Format**: knowledge packaged as Markdown files with a small YAML header (type, title, description, tags). No runtime, no SDK — the point is that humans read it on GitHub and AI agents read it as files. We generate a `.knowledge/` bundle per repo from the analysis layer, and ALSO embed those docs so "why"-questions retrieve curated intent, not just code.

## The infrastructure vocabulary

- **Redis queue** — ingestion takes minutes; the API can't block. The API pushes a job onto a Redis list; a separate **worker** process pops and executes it. Scale = run more workers.
- **Incremental ingestion** — we store a SHA-256 **hash** per file; on re-ingest, only files whose hash changed are re-processed. Unchanged repo = no-op.
- **SSE (Server-Sent Events)** — the streaming protocol the chat uses: the server sends `data: {...}` frames as the LLM produces tokens, so text appears word by word.
- **Structured logging** — logs as JSON objects (`{"stage": "retrieval.dense", "duration_ms": 42, "repo_id": ...}`) so dashboards can aggregate them; plain-text logs can't be queried.
- **Graceful degradation** — every stage has a documented fallback: embedder down → keyword-only search; graph down → plain RAG; reranker down → RRF order; Redis down → in-process cache. The system gets worse, never dead.
- **Recall@k / MRR / nDCG** — retrieval report cards: "was the right file in the top k?", "how high was the first right answer?", "how good is the whole ordering?". Defined precisely in `10_EVAL_AND_TESTS.md`.
