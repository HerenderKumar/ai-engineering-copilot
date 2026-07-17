"""
LLM fallback router — Gemini first, local Ollama second.

This is the module the rest of the app imports (`llm_client` for async
callers, `ask_llm` for sync ones); nothing outside services/llm/ should
import a concrete provider directly. Fallback fires on ANY provider failure:
missing/invalid GEMINI_API_KEY, network black-hole, rate limit, model error.

Streaming falls back only while the failing provider has produced no tokens
yet. After the first token we finish with the legacy inline
"[System Error: ...]" tail instead of restarting on the next provider —
restarting would duplicate the partial answer already on the user's screen.
The stream never raises (same contract the SSE endpoint always relied on);
the non-streaming methods raise RuntimeError when every provider failed.
"""

import logging
from typing import AsyncGenerator, Callable, List, Optional, Sequence, Tuple

from app.core.config import settings
from app.core.logging import log_event
from app.services.llm.gemini import ask_gemini, gemini_client
from app.services.llm.ollama import ollama_client

logger = logging.getLogger(__name__)


class LLMRouter:
    def __init__(self, providers: Optional[Sequence] = None):
        # Explicit providers are for tests; by default the chain is built
        # per-call so settings changes (e.g. OLLAMA_ENABLED) apply live.
        self._explicit = providers

    def _providers(self) -> List:
        if self._explicit is not None:
            return list(self._explicit)
        providers = [gemini_client]
        if settings.OLLAMA_ENABLED:
            providers.append(ollama_client)
        return providers

    async def generate_response(self, prompt: str) -> str:
        """Standard generation — used by POST /query/."""
        errors = []
        for provider in self._providers():
            try:
                return await provider.generate_response(prompt)
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                log_event(logger, "llm.fallback", failed_provider=provider.name,
                          error=str(e)[:300])
        raise RuntimeError("All LLM providers failed — " + " | ".join(errors))

    async def generate_stream(self, prompt: str) -> AsyncGenerator[str, None]:
        """Token streaming — used by POST /query/stream (SSE). Never raises;
        terminal failures surface as an inline error chunk."""
        errors = []
        for provider in self._providers():
            yielded = False
            try:
                async for chunk in provider.generate_stream(prompt):
                    yielded = True
                    yield chunk
                return
            except Exception as e:
                if yielded:
                    # Mid-answer failure: can't switch provider without
                    # duplicating the partial answer — close out inline.
                    log_event(logger, "llm.stream_failed_midway",
                              failed_provider=provider.name, error=str(e)[:300])
                    yield f"\n\n[System Error: LLM Generation failed - {e}]"
                    return
                errors.append(f"{provider.name}: {e}")
                log_event(logger, "llm.fallback", failed_provider=provider.name,
                          error=str(e)[:300])
        yield ("\n\n[System Error: LLM Generation failed - all providers "
               "failed: " + " | ".join(errors) + "]")


def _sync_providers() -> List[Tuple[str, Callable[[str], str]]]:
    providers: List[Tuple[str, Callable[[str], str]]] = [("gemini", ask_gemini)]
    if settings.OLLAMA_ENABLED:
        providers.append(("ollama", ollama_client.generate_sync))
    return providers


def ask_llm(prompt: str) -> str:
    """
    SYNCHRONOUS one-shot with the same fallback chain — for the analysis
    layer / OKF emitter. Raises RuntimeError when every provider failed;
    callers keep their existing catch-and-use-heuristics behavior.
    """
    errors = []
    for name, generate in _sync_providers():
        try:
            return generate(prompt)
        except Exception as e:
            errors.append(f"{name}: {e}")
            log_event(logger, "llm.fallback", failed_provider=name,
                      error=str(e)[:300])
    raise RuntimeError("All LLM providers failed — " + " | ".join(errors))


# Singleton — what the API layer imports.
llm_client = LLMRouter()
