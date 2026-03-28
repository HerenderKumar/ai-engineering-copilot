import logging
import numpy as np
from typing import List
from sentence_transformers import SentenceTransformer
from app.core.config import settings

logger = logging.getLogger(__name__)

class EmbeddingService:
    """
    Production-grade Embedding Service.
    Uses local HuggingFace SentenceTransformers (MiniLM) for fast, stable vectorization.
    Maintains the model in memory as a singleton to ensure low-latency API responses.
    """
    def __init__(self):
        model_name = settings.EMBEDDING_MODEL_NAME
        logger.info(f"Loading embedding model: {model_name}...")
        
        try:
            # This will download the weights on the very first run and cache them locally in ~/.cache/huggingface
            self.model = SentenceTransformer(model_name)
            logger.info("Embedding model loaded successfully.")
        except Exception as e:
            logger.critical(f"Failed to load embedding model '{model_name}': {e}")
            raise RuntimeError(f"Model initialization failed: {e}")

    def generate_embeddings(self, texts: List[str]) -> np.ndarray:
        """
        Generates dense vector embeddings for a list of text strings.
        
        Args:
            texts (List[str]): The code chunks or user query to embed.
            
        Returns:
            np.ndarray: A numpy array of shape (len(texts), embedding_dim).
        """
        if not texts:
            return np.array([])
            
        try:
            logger.debug(f"Generating embeddings for {len(texts)} text inputs...")
            embeddings = self.model.encode(
                texts, 
                convert_to_numpy=True, 
                show_progress_bar=False,
                batch_size=32  
            )
            return embeddings
        except Exception as e:
            logger.error(f"Failed to generate embeddings: {e}")
            raise RuntimeError(f"Embedding generation failed: {e}")

# Instantiate the singleton service
embedding_service = EmbeddingService()

# Expose the direct function interface expected by retrieval.py and ingestion.py
def generate_embeddings(texts: List[str]) -> np.ndarray:
    return embedding_service.generate_embeddings(texts)