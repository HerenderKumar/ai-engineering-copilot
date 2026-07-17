"""
LLM reasoning layer — Gemini today, PLUGGABLE by design (locked decision).

Changes vs the original: the client now initializes LAZILY. The old version
created `GeminiClient()` at import time and raised if GEMINI_API_KEY was
missing — meaning ingestion, retrieval, the graph and eval could not even
start without an LLM key, although none of them need one. Now only the
answer-generation step requires the key; everything else runs key-less
(required for the air-gapped / BYO-LLM path in the strategy).

Swapping the LLM = implementing these two methods (generate_response,
generate_stream) against another SDK behind the same interface — see
ollama.py for the local fallback and router.py for the fallback chain the
app actually imports (`llm_client` / `ask_llm`).
"""

import logging
from typing import AsyncGenerator, Optional

from app.core.config import settings
from app.core.logging import log_event

logger = logging.getLogger(__name__)


class GeminiClient:
    name = "gemini"

    def __init__(self):
        self._client = None  # created on first use
        self.model_name = settings.GEMINI_MODEL

    def _get_client(self):
        if self._client is None:
            api_key = settings.GEMINI_API_KEY.get_secret_value() \
                if hasattr(settings.GEMINI_API_KEY, "get_secret_value") \
                else settings.GEMINI_API_KEY
            if not api_key:
                raise RuntimeError(
                    "GEMINI_API_KEY is not set. Retrieval/graph/eval work without "
                    "it, but answer generation needs it — add it to backend/.env")
            from google import genai  # modern `google-genai` SDK
            self._client = genai.Client(api_key=api_key)
            log_event(logger, "llm.client_ready", model=self.model_name)
        return self._client

    async def generate_response(self, prompt: str) -> str:
        """Standard (non-streaming) generation — used by POST /query/."""
        try:
            client = self._get_client()
            response = await client.aio.models.generate_content(
                model=self.model_name, contents=prompt)
            if not response.text:
                logger.warning("Received an empty response from Gemini.")
                return ("The reasoning engine returned an empty response. "
                        "Please try refining your query.")
            return response.text
        except Exception as e:
            logger.error(f"Communication with Gemini API failed: {e}", exc_info=True)
            raise RuntimeError(f"LLM Generation failed: {e}")

    async def generate_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Token streaming — used by POST /query/stream (SSE).

        Raises on failure (it used to yield inline error text) so the router
        can fall back to the next provider; the router owns the user-facing
        error formatting now.
        """
        try:
            client = self._get_client()
            response_stream = await client.aio.models.generate_content_stream(
                model=self.model_name, contents=prompt)
            async for chunk in response_stream:
                if chunk.text:
                    yield chunk.text
            logger.debug("Successfully completed LLM stream.")
        except Exception as e:
            logger.error(f"Streaming from Gemini API failed: {e}", exc_info=True)
            raise RuntimeError(f"LLM Generation failed: {e}")


# Singleton — safe now that construction is side-effect free.
gemini_client = GeminiClient()


def ask_gemini(prompt: str) -> str:
    """
    SYNCHRONOUS one-shot helper for non-async callers (the analysis layer /
    OKF emitter run in plain functions). Raises RuntimeError when the key is
    missing — callers are expected to catch and fall back to heuristics.
    """
    client = gemini_client._get_client()
    response = client.models.generate_content(
        model=gemini_client.model_name, contents=prompt)
    return response.text or ""
