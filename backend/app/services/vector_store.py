"""
Vector + metadata store (Phases 0-1) — FAISS dense index per repo, SQLite for
chunk text / BM25 / state.

One class owns three storage concerns, all keyed by repo_id (per-repo
isolation — one customer's repo can never bleed into another's results):

  1. FAISS index per repo   — dense vectors; IndexIDMap wrapper so vectors can
     be REMOVED by id (surgical incremental updates).
  2. SQLite chunk_metadata  — the chunk text + file/line provenance; also an
     FTS5 virtual table (chunk_fts) giving BM25 keyword search for free.
  3. SQLite state tables    — file_hashes (incremental ingestion state) and
     embedding_meta (which embedding model each repo's vectors were made by).

Phase 1 upgrades:
  * model_id versioning     — vectors from different embedding models must
    never share an index; `needs_reindex()` + `wipe_repo()` give ingestion a
    controlled re-index path when the configured model changes.
  * spaces                  — each chunk is tagged 'code' or 'okf' so
    retrieval can run dense search per space (multi-source GraphRAG).
  * line spans              — start/end line per chunk → file:line citations
    and the chunk ↔ graph-node join.
  * cosine similarity       — new indices use inner product on normalized
    vectors (IndexFlatIP); existing L2 index files keep working as-is.
  * LRU cap on open indices — the old dict grew unboundedly; now bounded.
  * rank-order fix          — the old _fetch_metadata returned rows in SQL
    order, silently scrambling FAISS's ranking; results now preserve rank.
"""

import logging
import os
import re
import sqlite3
from collections import OrderedDict
from typing import Any, Dict, List, Optional

import faiss
import numpy as np

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger(__name__)

MAX_OPEN_INDICES = 16  # LRU bound on cached FAISS handles


class VectorMetadataStore:
    def __init__(self, base_dir: str = settings.DATA_DIR):
        self.base_dir = base_dir
        self.vector_dir = os.path.join(base_dir, "vectors")
        self.metadata_dir = os.path.join(base_dir, "metadata")
        os.makedirs(self.vector_dir, exist_ok=True)
        os.makedirs(self.metadata_dir, exist_ok=True)

        self.db_path = os.path.join(self.metadata_dir, "metadata.db")
        self._init_db()

        self.indices: "OrderedDict[str, faiss.Index]" = OrderedDict()
        self.embedding_dim = settings.EMBEDDING_DIM

    # -- schema + migrations ----------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")   # readers don't block writers
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def _init_db(self):
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS chunk_metadata (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        faiss_id INTEGER NOT NULL,
                        repo_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        chunk_index INTEGER NOT NULL,
                        content TEXT NOT NULL,
                        UNIQUE(repo_id, faiss_id)
                    )
                """)
                cursor.execute("CREATE INDEX IF NOT EXISTS idx_repo ON chunk_metadata(repo_id)")
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                        content, file_path, repo_id UNINDEXED, faiss_id UNINDEXED
                    )
                """)
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS file_hashes (
                        repo_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        file_hash TEXT NOT NULL,
                        PRIMARY KEY (repo_id, file_path)
                    )
                """)
                # Phase 1: which embedding model produced this repo's vectors
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS embedding_meta (
                        repo_id TEXT PRIMARY KEY,
                        model_id TEXT NOT NULL,
                        dim INTEGER NOT NULL
                    )
                """)
                # Phase 1 migrations: additive columns on existing installs.
                self._add_column_if_missing(cursor, "chunk_metadata", "model_id", "TEXT")
                self._add_column_if_missing(cursor, "chunk_metadata", "space", "TEXT DEFAULT 'code'")
                self._add_column_if_missing(cursor, "chunk_metadata", "start_line", "INTEGER")
                self._add_column_if_missing(cursor, "chunk_metadata", "end_line", "INTEGER")
                conn.commit()
        except sqlite3.Error as e:
            logger.critical(f"Database initialization failed: {e}")
            raise RuntimeError(f"Could not initialize metadata DB: {e}")

    @staticmethod
    def _add_column_if_missing(cursor, table: str, column: str, decl: str) -> None:
        cols = {row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in cols:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
            logger.info(f"migration: added {table}.{column}")

    # -- index handles (LRU) -----------------------------------------------------

    def _get_index_path(self, repo_id: str) -> str:
        return os.path.join(self.vector_dir, f"{repo_id}.index")

    def _load_or_create_index(self, repo_id: str) -> faiss.Index:
        if repo_id in self.indices:
            self.indices.move_to_end(repo_id)  # mark most-recently-used
            return self.indices[repo_id]

        index_path = self._get_index_path(repo_id)
        if os.path.exists(index_path):
            index = faiss.read_index(index_path)
        else:
            # Inner product on normalized vectors == cosine similarity.
            base = (faiss.IndexFlatIP(self.embedding_dim)
                    if settings.EMBEDDING_NORMALIZE
                    else faiss.IndexFlatL2(self.embedding_dim))
            # IndexIDMap lets us remove vectors by id for incremental updates.
            index = faiss.IndexIDMap(base)

        self.indices[repo_id] = index
        if len(self.indices) > MAX_OPEN_INDICES:  # evict least-recently-used
            evicted, _ = self.indices.popitem(last=False)
            log_event(logger, "vector_store.index_evicted", repo_id=evicted)
        return index

    # -- embedding-model versioning (Phase 1) -------------------------------------

    def get_repo_model_id(self, repo_id: str) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute("SELECT model_id FROM embedding_meta WHERE repo_id = ?",
                               (repo_id,)).fetchone()
            return row[0] if row else None

    def needs_reindex(self, repo_id: str, current_model_id: str) -> bool:
        """True when this repo's stored vectors came from a different model."""
        stored = self.get_repo_model_id(repo_id)
        return stored is not None and stored != current_model_id

    def wipe_repo(self, repo_id: str) -> None:
        """Controlled re-index path: drop every trace of a repo's vectors +
        state so the next ingestion rebuilds from scratch with the new model."""
        with self._connect() as conn:
            for table in ("chunk_metadata", "chunk_fts", "file_hashes", "embedding_meta"):
                conn.execute(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
            conn.commit()
        self.indices.pop(repo_id, None)
        index_path = self._get_index_path(repo_id)
        if os.path.exists(index_path):
            os.remove(index_path)
        log_event(logger, "vector_store.repo_wiped", repo_id=repo_id)

    # -- incremental ingestion state ----------------------------------------------

    def get_file_hashes(self, repo_id: str) -> Dict[str, str]:
        hashes: Dict[str, str] = {}
        try:
            with self._connect() as conn:
                for path, h in conn.execute(
                        "SELECT file_path, file_hash FROM file_hashes WHERE repo_id = ?",
                        (repo_id,)).fetchall():
                    hashes[path] = h
        except sqlite3.Error as e:
            logger.error(f"Failed to fetch file hashes for {repo_id}: {e}")
        return hashes

    def remove_files(self, repo_id: str, file_paths: List[str]) -> int:
        """Surgically remove all vectors/metadata belonging to specific files."""
        if not file_paths:
            return 0
        index = self._load_or_create_index(repo_id)
        removed_count = 0
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                faiss_ids: List[int] = []
                # SQLite caps bound variables (~999) — batch large file lists.
                for i in range(0, len(file_paths), 500):
                    batch = file_paths[i:i + 500]
                    placeholders = ",".join("?" * len(batch))
                    cursor.execute(
                        f"SELECT faiss_id FROM chunk_metadata WHERE repo_id = ? AND file_path IN ({placeholders})",
                        [repo_id] + batch)
                    faiss_ids.extend(row[0] for row in cursor.fetchall())

                if faiss_ids:
                    index.remove_ids(np.array(faiss_ids, dtype=np.int64))
                    faiss.write_index(index, self._get_index_path(repo_id))
                    for i in range(0, len(faiss_ids), 500):
                        batch = faiss_ids[i:i + 500]
                        ph = ",".join("?" * len(batch))
                        cursor.execute(f"DELETE FROM chunk_metadata WHERE repo_id = ? AND faiss_id IN ({ph})",
                                       [repo_id] + batch)
                        cursor.execute(f"DELETE FROM chunk_fts WHERE repo_id = ? AND faiss_id IN ({ph})",
                                       [repo_id] + batch)
                    removed_count = len(faiss_ids)

                for i in range(0, len(file_paths), 500):
                    batch = file_paths[i:i + 500]
                    ph = ",".join("?" * len(batch))
                    cursor.execute(f"DELETE FROM file_hashes WHERE repo_id = ? AND file_path IN ({ph})",
                                   [repo_id] + batch)
                conn.commit()
            log_event(logger, "vector_store.purged", repo_id=repo_id,
                      chunks=removed_count, files=len(file_paths))
            return removed_count
        except Exception as e:
            logger.error(f"Failed to remove files for {repo_id}: {e}")
            raise RuntimeError(f"Incremental deletion failed: {e}")

    # -- writes ---------------------------------------------------------------------

    def store_chunks(self, repo_id: str, chunks: List[Dict[str, Any]],
                     embeddings: Optional[np.ndarray], updated_hashes: Dict[str, str],
                     model_id: str = "", space: str = "code") -> List[int]:
        """
        Store chunks (+ vectors when available); returns the assigned
        faiss_ids (aligned with `chunks`) — ingestion uses them to join graph
        nodes to chunks.

        `embeddings=None` = degraded sparse-only mode: chunk text, BM25 and
        line spans are stored so keyword search and the graph work; dense
        search simply has nothing for these chunks until a re-index.
        """
        if embeddings is not None and len(chunks) != embeddings.shape[0]:
            raise ValueError("Mismatched chunks and embeddings.")
        index = self._load_or_create_index(repo_id)
        try:
            with self._connect() as conn:
                cursor = conn.cursor()
                max_id = cursor.execute(
                    "SELECT MAX(faiss_id) FROM chunk_metadata WHERE repo_id = ?",
                    (repo_id,)).fetchone()[0]
                start_id = 0 if max_id is None else max_id + 1
                faiss_ids = np.arange(start_id, start_id + len(chunks), dtype=np.int64)

                if len(chunks) > 0 and embeddings is not None:
                    index.add_with_ids(np.ascontiguousarray(embeddings, dtype=np.float32), faiss_ids)
                    faiss.write_index(index, self._get_index_path(repo_id))

                    cursor.executemany(
                        """INSERT INTO chunk_metadata
                           (faiss_id, repo_id, file_path, chunk_index, content,
                            model_id, space, start_line, end_line)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        [(int(f_id), repo_id, c["file_path"], c.get("chunk_index", i),
                          c["content"], model_id, space,
                          c.get("start_line"), c.get("end_line"))
                         for i, (f_id, c) in enumerate(zip(faiss_ids, chunks))])
                    cursor.executemany(
                        "INSERT INTO chunk_fts (content, file_path, repo_id, faiss_id) VALUES (?, ?, ?, ?)",
                        [(c["content"], c["file_path"], repo_id, int(f_id))
                         for f_id, c in zip(faiss_ids, chunks)])

                if updated_hashes:
                    cursor.executemany(
                        "INSERT OR REPLACE INTO file_hashes (repo_id, file_path, file_hash) VALUES (?, ?, ?)",
                        [(repo_id, p, h) for p, h in updated_hashes.items()])
                if model_id:
                    cursor.execute(
                        "INSERT OR REPLACE INTO embedding_meta (repo_id, model_id, dim) VALUES (?, ?, ?)",
                        (repo_id, model_id, self.embedding_dim))
                conn.commit()
            return [int(i) for i in faiss_ids]
        except Exception as e:
            logger.error(f"Failed to store incremental chunks: {e}")
            raise RuntimeError(f"Ingestion failed: {e}")

    # -- reads ------------------------------------------------------------------------

    def search_dense(self, repo_id: str, query_embedding: np.ndarray,
                     top_k: int = 15, space: Optional[str] = None) -> List[Dict[str, Any]]:
        """Vector similarity search; optionally restricted to one embedding
        space ('code' | 'okf'). Results preserve FAISS rank order."""
        index_path = self._get_index_path(repo_id)
        if not os.path.exists(index_path):
            return []
        index = self._load_or_create_index(repo_id)
        if index.ntotal == 0:
            return []
        if len(query_embedding.shape) == 1:
            query_embedding = np.expand_dims(query_embedding, axis=0)
        # Over-fetch when filtering by space (filter happens post-search).
        fetch_k = top_k * 3 if space else top_k
        _, indices = index.search(
            np.ascontiguousarray(query_embedding, dtype=np.float32),
            min(fetch_k, index.ntotal))
        valid = [int(idx) for idx in indices[0] if idx != -1]
        if not valid:
            return []
        results = self.fetch_chunks_by_ids(repo_id, valid)
        if space:
            results = [r for r in results if r.get("space", "code") == space]
        return results[:top_k]

    def search_sparse(self, repo_id: str, query: str, top_k: int = 15) -> List[Dict[str, Any]]:
        """BM25 keyword search via FTS5. Query is reduced to OR'd word tokens —
        raw user text (quotes, hyphens, '?') breaks FTS MATCH syntax."""
        tokens = re.findall(r"[A-Za-z0-9_]+", query)
        if not tokens:
            return []
        fts_query = " OR ".join(dict.fromkeys(tokens))  # dedup, keep order
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT faiss_id FROM chunk_fts WHERE repo_id = ? AND chunk_fts MATCH ? "
                    "ORDER BY bm25(chunk_fts) LIMIT ?",
                    (repo_id, fts_query, top_k)).fetchall()
                ids = [row["faiss_id"] for row in rows]
                return self.fetch_chunks_by_ids(repo_id, ids) if ids else []
        except sqlite3.Error as e:
            logger.warning(f"Sparse search failed: {e}")
            return []

    def fetch_chunks_by_ids(self, repo_id: str, faiss_ids: List[int]) -> List[Dict[str, Any]]:
        """Fetch chunk metadata, PRESERVING the order of `faiss_ids` (rank order).
        Public because graph expansion resolves node.chunk_id → chunk through it."""
        if not faiss_ids:
            return []
        by_id: Dict[int, Dict[str, Any]] = {}
        try:
            with self._connect() as conn:
                conn.row_factory = sqlite3.Row
                for i in range(0, len(faiss_ids), 500):
                    batch = faiss_ids[i:i + 500]
                    ph = ",".join("?" * len(batch))
                    for row in conn.execute(
                            f"""SELECT faiss_id, file_path, chunk_index, content,
                                       space, start_line, end_line
                                FROM chunk_metadata
                                WHERE repo_id = ? AND faiss_id IN ({ph})""",
                            [repo_id] + batch).fetchall():
                        by_id[row["faiss_id"]] = {
                            "faiss_id": row["faiss_id"],
                            "file_path": row["file_path"],
                            "chunk_index": row["chunk_index"],
                            "content": row["content"],
                            "space": row["space"] or "code",
                            "start_line": row["start_line"],
                            "end_line": row["end_line"],
                        }
        except sqlite3.Error as e:
            logger.error(f"Metadata retrieval failed: {e}")
        return [by_id[fid] for fid in faiss_ids if fid in by_id]


vector_metadata_store = VectorMetadataStore()
