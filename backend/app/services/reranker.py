import logging
from typing import List, Dict, Any
from sentence_transformers import CrossEncoder

logger = logging.getLogger(__name__)

class ReRankerService:
    """
    Production Cross-Encoder Re-ranking Service.
    Takes a candidate pool of retrieved chunks and strictly scores their relevance to the query.
    """
    def __init__(self, model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"):
        logger.info(f"Loading CrossEncoder model: {model_name}...")
        # ms-marco-MiniLM-L-6 is a standard, fast, and lightweight re-ranker optimized for RAG
        self.model = CrossEncoder(model_name, max_length=512)

    def rerank(self, query: str, documents: List[Dict[str, Any]], top_k: int = 10) -> List[Dict[str, Any]]:
        """
        Scores and sorts the documents based on query relevance.
        """
        if not documents:
            return []

        # CrossEncoder expects a list of pairs: [[query, doc1], [query, doc2], ...]
        pairs = [[query, doc.get("content", "")] for doc in documents]
        
        try:
            scores = self.model.predict(pairs)
            
            # Attach scores to the documents
            for i, doc in enumerate(documents):
                doc["rerank_score"] = float(scores[i])
                
            # Sort descending by the cross-encoder score
            ranked_docs = sorted(documents, key=lambda x: x["rerank_score"], reverse=True)
            
            # Return the refined Top-K
            return ranked_docs[:top_k]
            
        except Exception as e:
            logger.error(f"Re-ranking failed: {e}")
            # Fallback: return the original list truncated if the model fails
            return documents[:top_k]

# Singleton instance
reranker_service = ReRankerService()