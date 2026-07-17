# The Knowledge Graph — The Backbone

The structural layer (locked decision #3): a property graph of code entities, built with tree-sitter facts, persisted in SQLite, traversed for retrieval, rendered in 3D. Four files.

## The data model

**Nodes** — `file`, `class`, `function`, `method`. Each: deterministic `id`, `name`, `qualified_name` (`app.billing.Billing.charge`), `file_path`, line span, `signature`, `language`, `chunk_id` (→ the vector store join), and `x/y/z/cluster` (precomputed layout).

**Edges** — `CONTAINS` (file→class→method), `CALLS`, `IMPORTS` (file→file), `INHERITS`. Every edge has a **confidence** ∈ (0, 1]. Composite PK `(repo_id, src_id, dst_id, kind)` = re-inserting is idempotent. Two covering indices (`src` and `dst`) make both "what does X call" and "who calls X" instant.

## `app/services/graph_store.py` — persistence + traversal

The six correctness requirements from the handoff (§7) map to code here:

1. **Stable identity** — `stable_node_id(repo, qualified_name, file, signature)` = first 8 bytes of a SHA-256, masked to 63 bits (fits SQLite INTEGER). Content-derived → the same function gets the same id on every re-index. NEVER line numbers or array positions (they shift on every edit and would corrupt incremental updates). *Foundation: bad ids corrupt every re-index.*
2. **Edge accuracy** — confidences persisted; low-confidence *marked, not hidden* (`stats()` counts them; the UI draws them red).
3. **Incremental reconciliation** — `delete_files()` removes the files' nodes + BOTH edge directions, and returns which *other* files had edges pointing in — the builder must re-resolve those (no dangling edges, no phantom knowledge). `delete_out_edges()` supports re-resolving a file without rebuilding its nodes.
4. **Cycle-safe bounded traversal** — `neighbors(seeds, kinds, depth≤4, direction, max_nodes)` is a BFS with a visited set + node cap; recursion and cyclic imports are normal in code and must not hang a query. `trace_path(src, dst)` = bounded BFS shortest path (the Phase 2 exit-criterion function).
5. **Determinism** — every query is `ORDER BY`-ed; same graph in, same rows out (diffable, testable).
6. **Validation harness** — lives in `tests/test_graph_builder.py` (see doc 10).

Also: `subgraph()` (center-node / file / overview modes, node-capped — never a full dump), `update_layout()`, `stats()`, `wipe_repo()`. SQLite pragmas: WAL + busy_timeout.

## `app/services/graph_builder.py` — extraction + resolution

**Why two passes:** you can't resolve `fetch_user_tier()` while reading `billing.py` — the definition lives in `users.py`. So: **Pass 1** registers every definition of every changed file as nodes (+ CONTAINS edges) and collects raw facts (imports, call sites, base names). **Pass 2**, with the full symbol table (fresh facts + already-stored nodes), resolves each call/import/inheritance to a target node.

**The resolution ladder** (confidence encodes honesty about certainty):

| Conf | Rule | Example |
|---|---|---|
| 0.95 | same-file definition | helper defined above |
| 0.90 | directly imported symbol | `from users import fetch_user_tier` |
| 0.85 | via imported module/class alias | `billing.charge()`, `Billing.charge()` |
| 0.60 | unique global name | only one `save` in the repo |
| 0.30 | ambiguous → edge to ≤3 candidates, **marked** | three `render()`s |

**The false-positive guard** (a real bug caught by the test harness while building this): `stripe.PaymentIntent.create()` — `stripe` is imported but external, and the repo has its own `orders.create`. Naive fallback-to-global would fabricate `billing → orders.create`. Rule: once a name's provenance is KNOWN (it came from an import), never fall through to global guessing. Missing edges are recoverable (the semantic layer covers them); wrong edges poison trust.

**Incremental flow** (`build_or_update(changed, deleted, chunk_spans)`):
1. `delete_files(changed+deleted)` → learn `affected` files (had edges into the deleted nodes).
2. Pass 1 over changed files (with per-file parse isolation — a broken file logs and is skipped).
3. `affected ∪ importers-of-changed` get "facts-only" re-extraction + their out-edges dropped.
4. Pass 2 re-resolves everything in scope; logs `resolution_rate` (resolved calls / seen calls).

The `chunk_spans` parameter (`{file: [(start, end, faiss_id)]}`) sets each node's `chunk_id` = smallest chunk containing the node's start line — the join that powers graph expansion in retrieval.

## `app/services/graph_layout.py` — precompute the picture

Force-directed layout over thousands of nodes at 60 fps would melt the browser. So at INDEX time: build the graph in memory → **3D Fruchterman-Reingold** via python-igraph (C-speed) when installed, else seeded `networkx.spring_layout(dim=3)` → scale to scene units → **Louvain communities** (networkx built-in, seeded) → write `x/y/z/cluster` onto every node row. The browser then renders *fixed* positions with physics disabled. Layout failure only logs — it's cosmetic, never fatal.

## `app/api/graph.py` — the feed for the 3D UI

- `GET /graph/{repo}/subgraph?center=&file=&kinds=&depth=&limit=` → `{nodes: [...x,y,z,cluster...], edges: [...confidence...]}` — exactly what the renderer consumes.
- `GET /graph/{repo}/stats` — node/edge counts by kind + low-confidence count (the QA number).
- `GET /graph/{repo}/node/{id}` — one node + immediate neighborhood.

One JS-specific detail: node ids are serialized as **strings** because JavaScript numbers lose precision past 2^53 and our ids are 63-bit.

**Rebuild order:** graph_store (+ its tests — pure SQLite, no heavy deps) → graph_builder pass 1 → pass 2 ladder → reconciliation → the validation harness fixture → graph_layout → api/graph.py → wire into ingestion. Checkpoint: `pytest tests/test_graph_builder.py` — the hand-labeled ground truth must match exactly (that's the Phase 2 exit gate).
