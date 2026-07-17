"""
Ingestion orchestrator — the write path of the whole platform.

One job = one repo brought fully up to date, incrementally:

  1. clone (shallow) into a temp dir
  2. DIFF against stored per-file SHA-256 hashes → only changed/new files
     are processed, deleted files are purged (this is why re-ingesting an
     unchanged repo is a no-op)
  3. persist a working copy under REPOS_DIR (the analysis layer + OKF emitter
     need the source AFTER the temp clone is gone — this fixes the old 404
     on /analysis/{repo_id})
  4. embedding-model VERSION CHECK: if this repo's vectors came from a
     different model, wipe → full re-index (mixing vector spaces silently
     breaks similarity math)
  5. parse ONCE per file (tree-sitter) → chunk with context headers → embed
     → store vectors (returns faiss_ids)
  6. GRAPH: same parse trees → nodes + edges, with incremental edge
     reconciliation for changed/deleted files; then recompute the 3D layout
  7. OKF: emit + embed the curated-knowledge bundle

Failure containment: the graph, layout and OKF stages each degrade
gracefully (logged, ingestion still succeeds) — retrieval works as plain
hybrid RAG until the next successful graph build. Vector storage failures
DO fail the job (they're the core product).
"""

import hashlib
import logging
import os
import shutil
import stat
import subprocess
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from app.core.config import settings
from app.core.logging import log_event, stage_timer
from app.services import parsing
from app.services.chunking import CodeChunker
from app.services.embeddings import current_model_id, generate_embeddings
from app.services.vector_store import vector_metadata_store

logger = logging.getLogger(__name__)


def readonly_handler(func, path, execinfo):
    """shutil.rmtree helper: Windows marks .git files read-only; unlock + retry."""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception as e:
        logger.warning(f"Failed to force delete {path}: {e}")


class RepositoryIngestor:

    CLONE_TIMEOUT_S = 600  # kill runaway clones; connection failures die in seconds

    @staticmethod
    def _clone_repository(repo_url: str, target_dir: str) -> Optional[str]:
        """
        Materialize the source into target_dir. Two modes:
          * local directory  → straight copy (works fully OFFLINE; the path
            doesn't even need to be a git repo)
          * git URL          → shallow clone
        Returns None on success, else a human-readable error that flows into
        the job status (the UI must show WHY, not just that it failed).
        """
        expanded = os.path.abspath(os.path.expanduser(repo_url))
        if os.path.isdir(expanded):
            try:
                logger.info(f"Copying local folder {expanded} into {target_dir}...")
                shutil.copytree(expanded, target_dir, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(".git", "node_modules",
                                                              ".venv", "venv", "__pycache__"))
                return None
            except Exception as e:
                logger.error(f"Local copy failed: {e}")
                return f"local copy failed: {e}"
        try:
            logger.info(f"Cloning {repo_url} into {target_dir}...")
            # GIT_TERMINAL_PROMPT=0: a private/nonexistent repo must fail fast,
            # not hang the worker forever on a credential prompt.
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, target_dir],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                timeout=RepositoryIngestor.CLONE_TIMEOUT_S,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
            )
            return None
        except subprocess.TimeoutExpired:
            logger.error(f"Git clone timed out after {RepositoryIngestor.CLONE_TIMEOUT_S}s")
            return f"git clone timed out after {RepositoryIngestor.CLONE_TIMEOUT_S}s"
        except subprocess.CalledProcessError as e:
            logger.error(f"Git clone failed: {e.stderr}")
            # Last stderr lines carry git's 'fatal: ...' reason (network down,
            # repo not found, auth required, ...).
            tail = " | ".join((e.stderr or "").strip().splitlines()[-3:])
            return tail or "git clone failed (no error output)"

    @staticmethod
    def _compute_sha256(file_path: str) -> str:
        hasher = hashlib.sha256()
        with open(file_path, "rb") as f:
            for block in iter(lambda: f.read(4096), b""):
                hasher.update(block)
        return hasher.hexdigest()

    @staticmethod
    def _persist_working_copy(temp_dir: str, repo_id: str) -> Optional[str]:
        """Keep the source around for the analysis layer / OKF emitter."""
        dest = os.path.join(settings.REPOS_DIR, repo_id)
        try:
            if os.path.exists(dest):
                shutil.rmtree(dest, onerror=readonly_handler)
            shutil.copytree(temp_dir, dest,
                            ignore=shutil.ignore_patterns(".git"))
            return dest
        except Exception as e:
            log_event(logger, "ingest.persist_copy_failed", level=logging.WARNING,
                      repo_id=repo_id, error=str(e))
            return None

    @classmethod
    def ingest_repository(cls, repo_id: str, repo_url: str,
                          job_id: str = "adhoc") -> Dict[str, Any]:
        log_event(logger, "ingest.start", repo_id=repo_id, job_id=job_id, repo_url=repo_url)
        temp_dir = tempfile.mkdtemp(prefix=f"rag_ingest_{repo_id}_")
        try:
            clone_error = cls._clone_repository(repo_url, temp_dir)
            if clone_error:
                raise RuntimeError(f"Failed to fetch {repo_url} — {clone_error}")

            # --- Embedding-model version gate (Phase 1) -----------------------
            model_id = current_model_id()
            if vector_metadata_store.needs_reindex(repo_id, model_id):
                log_event(logger, "ingest.reindex_triggered", repo_id=repo_id,
                          job_id=job_id, old_model=vector_metadata_store.get_repo_model_id(repo_id),
                          new_model=model_id)
                vector_metadata_store.wipe_repo(repo_id)
                try:
                    from app.services.graph_store import graph_store
                    graph_store.wipe_repo(repo_id)
                except Exception:
                    pass

            # --- 1. State reconciliation (hash diff) ---------------------------
            chunker = CodeChunker(max_chunk_size=settings.MAX_CHUNK_SIZE,
                                  overlap_size=settings.CHUNK_OVERLAP)
            existing_hashes = vector_metadata_store.get_file_hashes(repo_id)
            current_hashes: Dict[str, str] = {}
            files_to_process: List[str] = []
            files_to_delete: List[str] = []

            with stage_timer(logger, "ingest.diff", repo_id=repo_id, job_id=job_id) as ctx:
                for root, _, files in os.walk(temp_dir):
                    for file in files:
                        full_path = os.path.join(root, file)
                        rel_path = os.path.relpath(full_path, temp_dir).replace("\\", "/")
                        if not chunker.is_processable_file(rel_path):
                            continue
                        try:
                            file_hash = cls._compute_sha256(full_path)
                            current_hashes[rel_path] = file_hash
                            if existing_hashes.get(rel_path) != file_hash:
                                files_to_process.append(rel_path)
                        except Exception as e:
                            logger.warning(f"Could not hash file {rel_path}: {e}")
                # Anything indexed before but absent now was deleted upstream —
                # skip OKF docs, which live in the index but not in the repo.
                files_to_delete = [p for p in existing_hashes
                                   if p not in current_hashes]
                files_to_process.sort()
                files_to_delete.sort()
                ctx.update(to_process=len(files_to_process), to_delete=len(files_to_delete))

            persisted = cls._persist_working_copy(temp_dir, repo_id)

            if not files_to_process and not files_to_delete:
                log_event(logger, "ingest.noop", repo_id=repo_id, job_id=job_id)
                return {"status": "success", "repo_id": repo_id, "action": "noop", "processed": 0}

            # --- 2. Surgical deletion of stale vectors --------------------------
            files_to_purge = files_to_delete + files_to_process
            if files_to_purge:
                vector_metadata_store.remove_files(repo_id, files_to_purge)

            # --- 3. Parse once → chunk → embed → store --------------------------
            all_chunks: List[Dict[str, Any]] = []
            updated_hashes: Dict[str, str] = {}
            trees: Dict[str, Any] = {}  # rel_path -> parse tree (reused by graph)

            with stage_timer(logger, "ingest.chunk", repo_id=repo_id, job_id=job_id) as ctx:
                for rel_path in files_to_process:
                    full_path = os.path.join(temp_dir, rel_path)
                    try:
                        with open(full_path, "r", encoding="utf-8",
                                  errors="replace") as f:
                            content = f.read()
                        lang = parsing.language_for(rel_path)
                        tree = parsing.parse(content, lang) if lang else None
                        trees[rel_path] = tree
                        all_chunks.extend(chunker.chunk_text(content, rel_path, tree=tree))
                        updated_hashes[rel_path] = current_hashes[rel_path]
                    except Exception as e:
                        logger.error(f"Failed to process {rel_path}: {e}")
                ctx["chunks"] = len(all_chunks)

            chunk_spans: Dict[str, List[Tuple[int, int, int]]] = {}
            degraded: List[str] = []
            if all_chunks:
                # Graceful degradation: if the embedding model can't load
                # (offline, blocked network, missing weights), store chunks
                # WITHOUT vectors — BM25 keyword search, the knowledge graph,
                # analysis and OKF all still work. Dense search lights up on
                # the next successful re-index.
                embeddings = None
                try:
                    with stage_timer(logger, "ingest.embed", repo_id=repo_id, job_id=job_id) as ctx:
                        embeddings = generate_embeddings([c["content"] for c in all_chunks])
                        ctx["vectors"] = int(embeddings.shape[0])
                except Exception as e:
                    degraded.append("embeddings")
                    log_event(logger, "ingest.embed_degraded", level=logging.WARNING,
                              repo_id=repo_id, job_id=job_id, error=str(e),
                              mode="sparse-only")
                with stage_timer(logger, "ingest.store", repo_id=repo_id, job_id=job_id):
                    # Degraded mode: skip the hash write, so the NEXT ingest
                    # re-processes these files and backfills their vectors.
                    faiss_ids = vector_metadata_store.store_chunks(
                        repo_id, all_chunks, embeddings,
                        updated_hashes if embeddings is not None else {},
                        model_id=model_id if embeddings is not None else "",
                        space="code")
                # chunk ↔ node join data for the graph builder.
                for chunk, faiss_id in zip(all_chunks, faiss_ids):
                    if chunk.get("start_line") is not None:
                        chunk_spans.setdefault(chunk["file_path"], []).append(
                            (chunk["start_line"], chunk.get("end_line") or chunk["start_line"],
                             faiss_id))

            # --- 4. Knowledge graph (Phase 2) — graceful, never fails the job ---
            graph_stats: Dict[str, Any] = {}
            if settings.GRAPH_ENABLED:
                try:
                    from app.services.graph_builder import GraphBuilder
                    builder = GraphBuilder(repo_id, temp_dir)
                    graph_stats = builder.build_or_update(
                        files_to_process, files_to_delete, chunk_spans)
                    if settings.GRAPH_LAYOUT_ON_INGEST:
                        from app.services.graph_layout import compute_layout
                        compute_layout(repo_id)
                except Exception as e:
                    log_event(logger, "ingest.graph_degraded", level=logging.WARNING,
                              repo_id=repo_id, job_id=job_id, error=str(e))

            # --- 5. OKF curated-knowledge bundle (Phase 3) — also graceful ------
            okf_result: Dict[str, Any] = {}
            if settings.OKF_ON_INGEST and persisted:
                from app.services.okf_emitter import emit_okf_bundle
                okf_result = emit_okf_bundle(repo_id, persisted)

            result = {
                "status": "success",
                "repo_id": repo_id,
                "action": "incremental_update",
                "files_added_or_modified": len(files_to_process),
                "files_deleted": len(files_to_delete),
                "chunks_embedded": len(all_chunks),
                "embedding_model": model_id,
                "degraded": degraded or None,
                "graph": graph_stats,
                "okf": {k: okf_result.get(k) for k in ("status", "docs")} if okf_result else {},
            }
            log_event(logger, "ingest.done", job_id=job_id, **{
                k: v for k, v in result.items() if k != "status"})
            return result

        except Exception as e:
            log_event(logger, "ingest.failed", level=logging.ERROR,
                      repo_id=repo_id, job_id=job_id, error=str(e))
            logger.error(f"Ingestion job failed for {repo_id}: {e}", exc_info=True)
            return {"status": "failed", "repo_id": repo_id, "error": str(e)}
        finally:
            try:
                shutil.rmtree(temp_dir, onerror=readonly_handler)
            except Exception as cleanup_error:
                logger.error(f"Failed to clean up temp dir {temp_dir}: {cleanup_error}")


ingest_repository = RepositoryIngestor.ingest_repository
