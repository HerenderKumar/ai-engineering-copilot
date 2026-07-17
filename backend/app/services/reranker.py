"""
Cross-encoder reranker — the precision pass at the end of retrieval.

Bi-encoders (our embedding model) score query and document INDEPENDENTLY —
fast, indexable, but coarse. A cross-encoder reads query+document TOGETHER
through one transformer and outputs a single relevance score — far more
accurate, far too slow to run over a whole corpus. So the standard recipe:
recall broadly with cheap search, then rerank the small fused pool (≤50)
with the expensive model.

Changes vs the original: the model now loads LAZILY (first rerank call, not
import) so the API boots instantly and, if the model can't load, retrieval
degrades to RRF order instead of the whole app failing to start.
"""

import logging
from typing import Any, Dict, List

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger(__name__)


class ReRankerService:
    def __init__(self, model_name: str = settings.RERANKER_MODEL_NAME):
        self.model_name = model_name
        self._model = None  # lazy

    def _load(self):
        if self._model is None:
            from sentence_transformers import CrossEncoder  # heavy import, kept lazy
            logger.info(f"Loading CrossEncoder model: {self.model_name}...")
            self._model = CrossEncoder(self.model_name, max_length=512)
            log_event(logger, "reranker.model_loaded", model=self.model_name)
        return self._model

    def rerank(self, query: str, documents: List[Dict[str, Any]],
               top_k: int = 10) -> List[Dict[str, Any]]:
        """Score each (query, chunk) pair; return top_k sorted by score.
        On any failure returns the input order truncated (fallback contract)."""
        if not documents:
            return []
        try:
            model = self._load()
            pairs = [[query, doc.get("content", "")] for doc in documents]
            scores = model.predict(pairs)
            for i, doc in enumerate(documents):
                doc["rerank_score"] = float(scores[i])
            ranked = sorted(documents, key=lambda x: x["rerank_score"], reverse=True)
            return ranked[:top_k]
        except Exception as e:
            log_event(logger, "reranker.fallback", level=logging.WARNING, reason=str(e))
            return documents[:top_k]


reranker_service = ReRankerService()
