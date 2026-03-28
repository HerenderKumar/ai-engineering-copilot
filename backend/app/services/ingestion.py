import os
import shutil
import stat
import logging
import tempfile
import subprocess
import hashlib
from typing import Dict, Any

from app.services.chunking import CodeChunker
from app.services.embeddings import generate_embeddings
from app.services.vector_store import vector_metadata_store
from app.core.config import settings

logger = logging.getLogger(__name__)

def readonly_handler(func, path, execinfo):
    """
    Error handler for shutil.rmtree.
    If the error is due to an access error (read only file),
    it attempts to add write permission and then retries.
    Crucial for Windows environments handling .git folders.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except Exception as e:
        logger.warning(f"Failed to force delete {path}: {e}")

class RepositoryIngestor:
    """
    Production Incremental Ingestion Orchestrator.
    Calculates file diffs to save compute, processing only modified/new files.
    """
    
    @staticmethod
    def _clone_repository(repo_url: str, target_dir: str) -> bool:
        try:
            logger.info(f"Cloning {repo_url} into {target_dir}...")
            subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, target_dir],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Git clone failed: {e.stderr}")
            return False

    @staticmethod
    def _compute_sha256(file_path: str) -> str:
        """Generates a SHA-256 hash for file state tracking."""
        hasher = hashlib.sha256()
        with open(file_path, 'rb') as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hasher.update(chunk)
        return hasher.hexdigest()

    @classmethod
    def ingest_repository(cls, repo_id: str, repo_url: str) -> Dict[str, Any]:
        logger.info(f"Starting Incremental Ingestion job for repo_id: {repo_id}")
        temp_dir = tempfile.mkdtemp(prefix=f"rag_ingest_{repo_id}_")
        
        try:
            if not cls._clone_repository(repo_url, temp_dir):
                raise RuntimeError(f"Failed to clone repository: {repo_url}")

            # 1. State Reconciliation
            existing_hashes = vector_metadata_store.get_file_hashes(repo_id)
            current_hashes = {}
            files_to_process = []
            files_to_delete = []
            
            chunker = CodeChunker(max_chunk_size=settings.MAX_CHUNK_SIZE, overlap_size=settings.CHUNK_OVERLAP)
            
            for root, _, files in os.walk(temp_dir):
                for file in files:
                    full_path = os.path.join(root, file)
                    rel_path = os.path.relpath(full_path, temp_dir)
                    
                    if not chunker.is_processable_file(rel_path):
                        continue
                        
                    try:
                        file_hash = cls._compute_sha256(full_path)
                        current_hashes[rel_path] = file_hash
                        
                        if rel_path not in existing_hashes or existing_hashes[rel_path] != file_hash:
                            files_to_process.append(rel_path)
                    except Exception as e:
                        logger.warning(f"Could not hash file {rel_path}: {e}")

            for rel_path in existing_hashes:
                if rel_path not in current_hashes:
                    files_to_delete.append(rel_path)

            logger.info(f"Delta calculation complete. To process: {len(files_to_process)}, To delete: {len(files_to_delete)}")

            if not files_to_process and not files_to_delete:
                logger.info(f"Repository {repo_id} is already up to date. No changes required.")
                return {"status": "success", "repo_id": repo_id, "action": "noop", "processed": 0}

            # 2. Surgical Deletion
            files_to_purge = files_to_delete + files_to_process
            if files_to_purge:
                logger.info(f"Purging {len(files_to_purge)} files from vector store...")
                vector_metadata_store.remove_files(repo_id, files_to_purge)

            # 3. Processing Delta
            all_chunks = []
            updated_hashes = {}
            
            if files_to_process:
                logger.info("Chunking modified/new files...")
                for rel_path in files_to_process:
                    full_path = os.path.join(temp_dir, rel_path)
                    try:
                        with open(full_path, 'r', encoding='utf-8') as f:
                            content = f.read()
                        file_chunks = chunker.chunk_text(content, rel_path)
                        all_chunks.extend(file_chunks)
                        updated_hashes[rel_path] = current_hashes[rel_path]
                    except Exception as e:
                        logger.error(f"Failed to process {rel_path}: {e}")

                if all_chunks:
                    logger.info(f"Generating embeddings for {len(all_chunks)} new chunks...")
                    embeddings = generate_embeddings([chunk["content"] for chunk in all_chunks])
                    
                    logger.info("Committing to Vector Store...")
                    vector_metadata_store.store_chunks(repo_id, all_chunks, embeddings, updated_hashes)

            return {
                "status": "success",
                "repo_id": repo_id,
                "action": "incremental_update",
                "files_added_or_modified": len(files_to_process),
                "files_deleted": len(files_to_delete),
                "chunks_embedded": len(all_chunks)
            }

        except Exception as e:
            logger.error(f"Ingestion job failed for {repo_id}: {str(e)}", exc_info=True)
            return {"status": "failed", "repo_id": repo_id, "error": str(e)}
            
        finally:
            logger.debug(f"Cleaning up temporary directory: {temp_dir}")
            try:
                # The onerror handler forces Windows to bypass the Read-Only lock
                shutil.rmtree(temp_dir, onerror=readonly_handler)
            except Exception as cleanup_error:
                logger.error(f"Failed to clean up temp directory {temp_dir}: {cleanup_error}")

ingest_repository = RepositoryIngestor.ingest_repository