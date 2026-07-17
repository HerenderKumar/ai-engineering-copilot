"""
Embedding service (Phase 1) — code-trained model, versioned vectors, cached.

Changes vs the original:
  * Model is CONFIG-DRIVEN (locked decision #7): default BGE-M3 (code-trained,
    1024-dim) instead of generic English MiniLM. Set EMBEDDING_MODEL_NAME /
    EMBEDDING_DIM in .env to swap — e.g. MiniLM for a laptop demo.
  * VERSIONING: every stored vector carries `model_id`. Mixing vectors from
    different models in one index silently ruins similarity math; the vector
    store compares its recorded model_id against this one and triggers a
    controlled re-index on mismatch.
  * LAZY LOADING: the multi-GB model loads on first use, not import — so the
    API process, tests, and the graph pipeline never pay for it and the
    platform can degrade to sparse-only search when the model is unavailable.
  * CACHING: content-hash → vector via the cache layer (a re-ingested repo
    re-embeds only genuinely new text).
  * NORMALIZATION: vectors are L2-normalized so inner-product FAISS search
    equals cosine similarity (the standard for embedding retrieval).
"""

import hashlib
import logging
from typing import List, Optional

import numpy as np

from app.core.config import settings
from app.core.logging import log_event
from app.services.cache import cache

logger = logging.getLogger(__name__)


class EmbeddingUnavailable(RuntimeError):
    """Raised when the embedding model can't load/run — callers degrade to sparse."""


class EmbeddingService:
    def __init__(self):
        self._model = None  # loaded lazily

    @property
    def model_id(self) -> str:
        """Version tag stored with every vector, e.g. 'BAAI/bge-m3#1024'."""
        return f"{settings.EMBEDDING_MODEL_NAME}#{settings.EMBEDDING_DIM}"

    def _load(self):
        if self._model is not None:
            return self._model
        try:
            from sentence_transformers import SentenceTransformer  # heavy import, kept lazy
            logger.info(f"Loading embedding model: {settings.EMBEDDING_MODEL_NAME}...")
            self._model = SentenceTransformer(settings.EMBEDDING_MODEL_NAME)
            log_event(logger, "embeddings.model_loaded", model_id=self.model_id)
            return self._model
        except Exception as e:
            log_event(logger, "embeddings.unavailable", level=logging.ERROR, error=str(e))
            raise EmbeddingUnavailable(f"Embedding model failed to load: {e}") from e

    @staticmethod
    def _content_key(text: str, model_id: str) -> str:
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
        return f"emb:{model_id}:{h}"

    def generate_embeddings(self, texts: List[str], use_cache: bool = True) -> np.ndarray:
        """
        Embed a list of texts → float32 array (n, dim), L2-normalized if
        configured. Cache-aside: look up each text by content hash, embed only
        the misses, write those back.
        """
        if not texts:
            return np.zeros((0, settings.EMBEDDING_DIM), dtype=np.float32)

        model_id = self.model_id
        vectors: List[Optional[np.ndarray]] = [None] * len(texts)
        miss_idx: List[int] = []

        if use_cache:
            for i, text in enumerate(texts):
                hit = cache.get_json(self._content_key(text, model_id))
                if hit is not None:
                    vectors[i] = np.asarray(hit, dtype=np.float32)
                else:
                    miss_idx.append(i)
        else:
            miss_idx = list(range(len(texts)))

        if miss_idx:
            model = self._load()
            try:
                fresh = model.encode(
                    [texts[i] for i in miss_idx],
                    convert_to_numpy=True,
                    show_progress_bar=False,
                    batch_size=32,
                )
            except Exception as e:
                raise EmbeddingUnavailable(f"Embedding generation failed: {e}") from e
            fresh = np.asarray(fresh, dtype=np.float32)
            if settings.EMBEDDING_NORMALIZE:
                norms = np.linalg.norm(fresh, axis=1, keepdims=True)
                norms[norms == 0] = 1.0
                fresh = fresh / norms
            for j, i in enumerate(miss_idx):
                vectors[i] = fresh[j]
                if use_cache:
                    cache.set_json(self._content_key(texts[i], model_id),
                                   fresh[j].tolist(), settings.EMBEDDING_CACHE_TTL)

        log_event(logger, "embeddings.batch", n=len(texts),
                  cache_hits=len(texts) - len(miss_idx), model_id=model_id)
        return np.stack(vectors).astype(np.float32)


# Singleton + function interface expected by retrieval.py and ingestion.py
embedding_service = EmbeddingService()


def generate_embeddings(texts: List[str]) -> np.ndarray:
    return embedding_service.generate_embeddings(texts)


def current_model_id() -> str:
    return embedding_service.model_id
