import os
import sqlite3
import numpy as np
import faiss
from typing import List, Dict, Any, Tuple
from app.core.config import settings
import logging

logger = logging.getLogger(__name__)

class VectorMetadataStore:
    """
    Production-grade Vector and Metadata Store.
    Handles Hybrid Search (Dense FAISS + Sparse FTS5) and Stateful Incremental Indexing.
    """
    def __init__(self, base_dir: str = settings.DATA_DIR):
        self.base_dir = base_dir
        self.vector_dir = os.path.join(base_dir, "vectors")
        self.metadata_dir = os.path.join(base_dir, "metadata")
        
        os.makedirs(self.vector_dir, exist_ok=True)
        os.makedirs(self.metadata_dir, exist_ok=True)
        
        self.db_path = os.path.join(self.metadata_dir, "metadata.db")
        self._init_db()
        
        self.indices: Dict[str, faiss.Index] = {}
        self.embedding_dim = settings.EMBEDDING_DIM

    def _init_db(self):
        """Initializes the SQLite DB with standard metadata, FTS5, and file hashing state."""
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                # Standard metadata
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
                
                # FTS5 Virtual Table for BM25 Sparse Search
                cursor.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
                        content, file_path, repo_id UNINDEXED, faiss_id UNINDEXED
                    )
                """)

                # NEW: File hashing state for incremental ingestion
                cursor.execute("""
                    CREATE TABLE IF NOT EXISTS file_hashes (
                        repo_id TEXT NOT NULL,
                        file_path TEXT NOT NULL,
                        file_hash TEXT NOT NULL,
                        PRIMARY KEY (repo_id, file_path)
                    )
                """)
                conn.commit()
        except sqlite3.Error as e:
            logger.critical(f"Database initialization failed: {e}")
            raise RuntimeError(f"Could not initialize metadata DB: {e}")

    def _get_index_path(self, repo_id: str) -> str:
        return os.path.join(self.vector_dir, f"{repo_id}.index")

    def _load_or_create_index(self, repo_id: str) -> faiss.Index:
        if repo_id in self.indices:
            return self.indices[repo_id]

        index_path = self._get_index_path(repo_id)
        if os.path.exists(index_path):
            index = faiss.read_index(index_path)
        else:
            # IndexIDMap allows us to surgically remove vectors by ID later
            index = faiss.IndexIDMap(faiss.IndexFlatL2(self.embedding_dim))
            
        self.indices[repo_id] = index
        return index

    def get_file_hashes(self, repo_id: str) -> Dict[str, str]:
        """Retrieves the current state of file hashes for a repository."""
        hashes = {}
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT file_path, file_hash FROM file_hashes WHERE repo_id = ?", (repo_id,))
                for row in cursor.fetchall():
                    hashes[row[0]] = row[1]
        except sqlite3.Error as e:
            logger.error(f"Failed to fetch file hashes for {repo_id}: {e}")
        return hashes

    def remove_files(self, repo_id: str, file_paths: List[str]) -> int:
        """
        Surgically removes all vectors and metadata associated with specific files.
        Crucial for processing modified or deleted files.
        """
        if not file_paths:
            return 0

        index = self._load_or_create_index(repo_id)
        removed_count = 0

        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                # Max variable limit in SQLite is 999; chunk the file paths if needed. 
                # Keeping simple for standard commits, but in massive repos, chunk the list.
                placeholders = ",".join("?" * len(file_paths))
                
                # 1. Get FAISS IDs associated with these files
                query = f"SELECT faiss_id FROM chunk_metadata WHERE repo_id = ? AND file_path IN ({placeholders})"
                cursor.execute(query, [repo_id] + file_paths)
                faiss_ids = [row[0] for row in cursor.fetchall()]
                
                if faiss_ids:
                    # 2. Remove from FAISS index
                    ids_array = np.array(faiss_ids, dtype=np.int64)
                    index.remove_ids(ids_array)
                    
                    if repo_id in self.indices:
                        faiss.write_index(self.indices[repo_id], self._get_index_path(repo_id))
                    
                    # 3. Remove from SQLite (Metadata & FTS5)
                    id_placeholders = ",".join("?" * len(faiss_ids))
                    cursor.execute(f"DELETE FROM chunk_metadata WHERE repo_id = ? AND faiss_id IN ({id_placeholders})", [repo_id] + faiss_ids)
                    cursor.execute(f"DELETE FROM chunk_fts WHERE repo_id = ? AND faiss_id IN ({id_placeholders})", [repo_id] + faiss_ids)
                    
                    removed_count = len(faiss_ids)

                # 4. Remove the file hash states
                cursor.execute(f"DELETE FROM file_hashes WHERE repo_id = ? AND file_path IN ({placeholders})", [repo_id] + file_paths)
                conn.commit()
                
                logger.info(f"Purged {removed_count} stale chunks across {len(file_paths)} files for {repo_id}.")
                return removed_count

        except Exception as e:
            logger.error(f"Failed to remove files for {repo_id}: {e}")
            raise RuntimeError(f"Incremental deletion failed: {e}")

    def store_chunks(self, repo_id: str, chunks: List[Dict[str, Any]], embeddings: np.ndarray, updated_hashes: Dict[str, str]) -> bool:
        """Stores new chunks, embeddings, and updates the file hash states."""
        if len(chunks) != embeddings.shape[0]:
            raise ValueError("Mismatched chunks and embeddings.")

        index = self._load_or_create_index(repo_id)
        
        try:
            with sqlite3.connect(self.db_path) as conn:
                cursor = conn.cursor()
                
                cursor.execute("SELECT MAX(faiss_id) FROM chunk_metadata WHERE repo_id = ?", (repo_id,))
                max_id = cursor.fetchone()[0]
                start_id = 0 if max_id is None else max_id + 1
                
                faiss_ids = np.arange(start_id, start_id + len(chunks), dtype=np.int64)
                
                # 1. FAISS Insert
                if len(chunks) > 0:
                    index.add_with_ids(embeddings, faiss_ids)
                    if repo_id in self.indices:
                        faiss.write_index(self.indices[repo_id], self._get_index_path(repo_id))
                
                # 2. SQLite Inserts (Metadata + FTS5)
                if len(chunks) > 0:
                    metadata_records = [(int(f_id), repo_id, c["file_path"], c.get("chunk_index", i), c["content"]) for i, (f_id, c) in enumerate(zip(faiss_ids, chunks))]
                    cursor.executemany("INSERT INTO chunk_metadata (faiss_id, repo_id, file_path, chunk_index, content) VALUES (?, ?, ?, ?, ?)", metadata_records)
                    
                    fts_records = [(c["content"], c["file_path"], repo_id, int(f_id)) for i, (f_id, c) in enumerate(zip(faiss_ids, chunks))]
                    cursor.executemany("INSERT INTO chunk_fts (content, file_path, repo_id, faiss_id) VALUES (?, ?, ?, ?)", fts_records)
                
                # 3. Update File Hashes
                if updated_hashes:
                    hash_records = [(repo_id, path, f_hash) for path, f_hash in updated_hashes.items()]
                    cursor.executemany("INSERT OR REPLACE INTO file_hashes (repo_id, file_path, file_hash) VALUES (?, ?, ?)", hash_records)
                
                conn.commit()
                return True
                
        except Exception as e:
            logger.error(f"Failed to store incremental chunks: {e}")
            raise RuntimeError(f"Ingestion failed: {e}")

    # ... keep search_dense, search_sparse, and _fetch_metadata exactly as they were in the previous iteration ...
    def search_dense(self, repo_id: str, query_embedding: np.ndarray, top_k: int = 15) -> List[Dict[str, Any]]:
        index_path = self._get_index_path(repo_id)
        if not os.path.exists(index_path): return []
        index = self._load_or_create_index(repo_id)
        if len(query_embedding.shape) == 1: query_embedding = np.expand_dims(query_embedding, axis=0)
        _, indices = index.search(query_embedding, top_k)
        valid_indices = [int(idx) for idx in indices[0] if idx != -1]
        if not valid_indices: return []
        return self._fetch_metadata(repo_id, valid_indices)

    def search_sparse(self, repo_id: str, query: str, top_k: int = 15) -> List[Dict[str, Any]]:
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                clean_query = query.replace('"', '').replace("'", "")
                fts_query = "SELECT faiss_id FROM chunk_fts WHERE repo_id = ? AND chunk_fts MATCH ? ORDER BY bm25(chunk_fts) LIMIT ?"
                cursor.execute(fts_query, (repo_id, clean_query, top_k))
                valid_indices = [row["faiss_id"] for row in cursor.fetchall()]
                if not valid_indices: return []
                return self._fetch_metadata(repo_id, valid_indices)
        except sqlite3.Error as e:
            logger.warning(f"Sparse search failed: {e}")
            return []

    def _fetch_metadata(self, repo_id: str, faiss_ids: List[int]) -> List[Dict[str, Any]]:
        results = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                placeholders = ",".join("?" * len(faiss_ids))
                query = f"SELECT faiss_id, file_path, chunk_index, content FROM chunk_metadata WHERE repo_id = ? AND faiss_id IN ({placeholders})"
                cursor.execute(query, [repo_id] + faiss_ids)
                for row in cursor.fetchall():
                    results.append({"faiss_id": row["faiss_id"], "file_path": row["file_path"], "chunk_index": row["chunk_index"], "content": row["content"]})
        except sqlite3.Error as e:
            logger.error(f"Metadata retrieval failed: {e}")
        return results

vector_metadata_store = VectorMetadataStore()