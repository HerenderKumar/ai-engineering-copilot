import logging
from typing import List, Dict, Any

from app.services.embeddings import generate_embeddings
from app.services.vector_store import vector_metadata_store
from app.services.reranker import reranker_service
from app.core.config import settings

logger = logging.getLogger(__name__)

class RetrievalService:
    """
    Production-grade Hybrid Retrieval Service.
    Combines Dense (FAISS), Sparse (FTS5/BM25), and Cross-Encoder Re-ranking.
    """
    
    @staticmethod
    def retrieve_context(repo_id: str, query: str, top_k: int = settings.DEFAULT_TOP_K) -> List[Dict[str, Any]]:
        logger.info(f"Executing Hybrid Retrieval for repo: {repo_id} | Query: '{query}'")
        
        if not query.strip():
            return []

        try:
            # We fetch more candidates than requested to give the re-ranker a good pool (e.g., 2x top_k)
            candidate_pool_size = top_k * 2

            # 1. Dense Retrieval (Semantic Meaning)
            query_embedding = generate_embeddings([query])[0]
            dense_results = vector_metadata_store.search_dense(
                repo_id=repo_id, 
                query_embedding=query_embedding, 
                top_k=candidate_pool_size
            )
            
            # 2. Sparse Retrieval (Exact Keyword Matching via BM25)
            sparse_results = vector_metadata_store.search_sparse(
                repo_id=repo_id, 
                query=query, 
                top_k=candidate_pool_size
            )

            # 3. Fuse & Deduplicate Results
            # We use faiss_id as the unique identifier for deduplication
            fused_candidates = {}
            for doc in dense_results + sparse_results:
                faiss_id = doc["faiss_id"]
                if faiss_id not in fused_candidates:
                    fused_candidates[faiss_id] = doc

            unique_candidates = list(fused_candidates.values())
            logger.debug(f"Hybrid search yielded {len(unique_candidates)} unique candidate chunks.")

            # 4. Smart Re-ranking Layer (Cross-Encoder)
            # If we don't have enough candidates, just return them
            if len(unique_candidates) <= top_k and len(unique_candidates) > 0:
                logger.debug("Skipping re-ranker, pool size <= top_k.")
                return unique_candidates

            final_ranked_results = reranker_service.rerank(
                query=query, 
                documents=unique_candidates, 
                top_k=top_k
            )
            
            logger.info(f"Successfully retrieved and re-ranked Top-{len(final_ranked_results)} chunks.")
            return final_ranked_results
            
        except Exception as e:
            logger.error(f"Retrieval pipeline failed for repo {repo_id}: {e}", exc_info=True)
            raise RuntimeError(f"Failed to retrieve context: {str(e)}")

retrieve_context = RetrievalService.retrieve_context